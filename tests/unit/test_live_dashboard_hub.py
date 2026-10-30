"""Dashboard live hub: per-topic authorization matrix, filtering, refcounts, diagnostics loop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server import config
from server.live.hub import TOPIC_PERMISSIONS, Connection, DashboardHub, required_permissions
from server.services import internal_client
from server.services.internal_client import InternalAPIError

pytestmark = pytest.mark.asyncio

ALL = set(TOPIC_PERMISSIONS)


def sess(uid=1, member=True, roles=()):
    return SimpleNamespace(discord_user_id=uid, is_guild_member=member, cached_role_ids=list(roles), session_id=f"s{uid}")


class FakeUpstream:
    def __init__(self, connected=True):
        self.connected = connected
        self.rtt_ms = 1.5
        self.topics: set[str] = set()
        self.history: list[set[str]] = []

    def set_topics(self, topics):
        self.topics = set(topics)
        self.history.append(set(topics))


@pytest.fixture
def perms(monkeypatch):
    """Controllable permission oracle: {user_id: {"perms": set, "staff": bool}}, or bot down."""
    state = {"users": {}, "down": False, "calls": 0}

    async def bulk(guild_id, user_id, permissions):
        state["calls"] += 1
        if state["down"]:
            raise InternalAPIError("down")
        granted = state["users"].get(user_id, {}).get("perms", set())
        return {p: (p in granted) for p in permissions}

    async def is_staff(guild_id, user_id):
        if state["down"]:
            raise InternalAPIError("down")
        return bool(state["users"].get(user_id, {}).get("staff"))

    monkeypatch.setattr(internal_client, "check_permissions_bulk", bulk)
    monkeypatch.setattr(internal_client, "check_is_staff", is_staff)
    return state


def drain(conn):
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get_nowait())
    return out


async def connect(hub, session):
    conn = Connection(session.session_id, session.discord_user_id, "user", await hub.resolve_access(session))
    hub.add_connection(conn)
    return conn


@pytest.mark.parametrize(
    "grant,expected",
    [
        (set(), {"bot"}),
        ({"staff_base"}, {"bot", "stats"}),  # stats has the same gate as GET /api/dashboard/stats
        ({"moderate_members"}, {"bot", "honeypot", "moderation"}),
        ({"manage_tickets", "staff_base"}, {"bot", "stats", "tickets"}),
        ({"view_audit"}, {"bot", "audit"}),
        ({"kick_members", "ban_members", "verify_members"}, {"bot"}),  # unrelated grants
        # automation is any-of (manage_events / manage_schedules / manage_autorole / manage_templates)
        ({"manage_templates"}, {"bot", "automation"}),
        ({"manage_autorole"}, {"bot", "automation"}),
    ],
)
async def test_topic_authorization_matrix(perms, grant, expected):
    perms["users"][1] = {"perms": grant, "staff": False}
    assert await DashboardHub().resolve_access(sess(1)) == expected


async def test_staff_gets_diagnostics_and_everything_else(perms):
    # The real bulk check returns True for every permission when the user is staff.
    every_grant = {p for req in TOPIC_PERMISSIONS.values() for p in required_permissions(req)}
    perms["users"][1] = {"perms": every_grant, "staff": True}
    assert await DashboardHub().resolve_access(sess(1)) == ALL


async def test_non_member_gets_nothing(perms):
    assert await DashboardHub().resolve_access(sess(1, member=False)) == set()


async def test_permission_cache_ttl_and_force(perms, monkeypatch):
    hub = DashboardHub()
    perms["users"][1] = {"perms": {"manage_tickets"}, "staff": False}
    await hub.resolve_access(sess(1))
    calls = perms["calls"]
    await hub.resolve_access(sess(1))
    assert perms["calls"] == calls  # served from cache
    perms["users"][1] = {"perms": set(), "staff": False}
    assert "tickets" in await hub.resolve_access(sess(1))  # stale within TTL
    assert "tickets" not in await hub.resolve_access(sess(1), force=True)  # forced re-check sees the demotion


async def test_bot_down_fallbacks(perms, monkeypatch):
    hub = DashboardHub()
    perms["down"] = True
    monkeypatch.setenv("STAFF_ROLE_IDS", "555")
    monkeypatch.setenv("OWNER_ID", "0")
    # unknown user, bot down: member topics only; cached staff role keeps diagnostics
    assert await hub.resolve_access(sess(2, roles=[1])) == {"bot"}
    assert await hub.resolve_access(sess(3, roles=[555])) == {"bot", "diagnostics"}
    # a user resolved earlier keeps their last known grants while the bot is down
    perms["down"] = False
    perms["users"][4] = {"perms": {"view_audit"}, "staff": False}
    first = await hub.resolve_access(sess(4))
    perms["down"] = True
    assert await hub.resolve_access(sess(4), force=True) == first


async def test_forbidden_subscribe_is_refused_and_never_receives(perms):
    hub = DashboardHub()
    perms["users"][1] = {"perms": {"manage_tickets"}, "staff": False}
    conn = await connect(hub, sess(1))
    res = hub.subscribe(conn, ["tickets", "audit", "diagnostics", "nope"])
    assert res["accepted"] == ["tickets"] and set(res["denied"]) == {"audit", "diagnostics"} and res["unknown"] == ["nope"]
    codes = [(m["topic"], m["data"]["code"]) for m in drain(conn) if m["type"] == "error"]
    assert ("audit", "forbidden") in codes and ("diagnostics", "forbidden") in codes
    hub.broadcast("audit", "event", {"secret": 1})
    hub.broadcast("tickets", "event", {"kind": "opened"})
    got = [m["topic"] for m in drain(conn)]
    assert got == ["tickets"]  # even if a client "subscribed" it, filtering is server-side


async def test_client_side_topic_set_cannot_bypass_allowed(perms):
    """Defence in depth: even a topic wrongly present in conn.topics is filtered."""
    hub = DashboardHub()
    conn = await connect(hub, sess(1))
    conn.topics.add("audit")  # simulate a bug / tampering
    hub.broadcast("audit", "event", {"x": 1})
    assert drain(conn) == []


async def test_demotion_removes_topic_and_upstream_refcount(perms):
    hub = DashboardHub()
    up = hub.upstream = FakeUpstream()
    perms["users"][1] = {"perms": {"moderate_members", "staff_base"}, "staff": False}
    conn = await connect(hub, sess(1))
    hub.subscribe(conn, ["honeypot", "moderation", "stats"])
    assert up.topics == {"honeypot", "moderation", "stats"}
    drain(conn)

    perms["users"][1] = {"perms": {"staff_base"}, "staff": False}  # demoted
    revoked = hub.apply_access(conn, await hub.resolve_access(sess(1), force=True))
    assert revoked == ["honeypot", "moderation"]
    assert conn.topics == {"stats"} and up.topics == {"stats"}
    errs = [m for m in drain(conn) if m["type"] == "error"]
    assert {e["topic"] for e in errs} == {"honeypot", "moderation"}
    hub.broadcast("honeypot", "event", {"kind": "catch_created"})
    assert drain(conn) == []


async def test_upstream_topics_are_refcounted_across_connections(perms):
    hub = DashboardHub()
    up = hub.upstream = FakeUpstream()
    perms["users"][1] = perms["users"][2] = {"perms": {"staff_base"}, "staff": False}
    a, b = await connect(hub, sess(1)), await connect(hub, sess(2))
    hub.subscribe(a, ["stats"])
    hub.subscribe(b, ["stats", "bot"])
    assert up.topics == {"stats", "bot"}
    hub.unsubscribe(a, ["stats"])
    assert up.topics == {"stats", "bot"}  # b still watching
    hub.remove_connection(b)
    assert up.topics == set()


async def test_upstream_events_forwarded_with_dashboard_seq_and_cached_stats(perms):
    hub = DashboardHub()
    perms["users"][1] = {"perms": {"moderate_members", "staff_base"}, "staff": False}
    conn = await connect(hub, sess(1))
    hub.subscribe(conn, ["honeypot", "stats"])
    drain(conn)
    hub.on_upstream_message({"type": "event", "topic": "honeypot", "data": {"kind": "catch_created", "catch": {"id": 1}}})
    hub.on_upstream_message({"type": "snapshot", "topic": "stats", "data": {"total": 9}})
    hub.on_upstream_message({"type": "event", "topic": "tickets", "data": {"kind": "opened"}})  # not subscribed/allowed
    hub.on_upstream_message({"type": "event", "topic": "bogus", "data": {}})
    hub.on_upstream_message({"type": "event", "topic": "honeypot", "data": "not-a-dict"})
    msgs = drain(conn)
    assert [(m["topic"], m["type"]) for m in msgs] == [("honeypot", "event"), ("stats", "snapshot")]
    assert msgs[0]["v"] == 1 and msgs[0]["seq"] == 1 and isinstance(msgs[0]["ts"], int)
    # a later subscriber gets the cached stats snapshot immediately
    perms["users"][2] = {"perms": {"staff_base"}, "staff": False}
    late = await connect(hub, sess(2))
    hub.subscribe(late, ["stats"])
    (snap,) = [m for m in drain(late) if m["topic"] == "stats"]
    assert snap["type"] == "snapshot" and snap["data"]["total"] == 9


async def test_bot_state_broadcast_and_snapshot(perms):
    hub = DashboardHub()
    conn = await connect(hub, sess(1))
    hub.subscribe(conn, ["bot"])
    first = drain(conn)[0]
    assert first["type"] == "snapshot" and first["data"]["online"] is False
    hub.on_upstream_state(True)
    hub.on_upstream_message({"type": "snapshot", "topic": "bot", "data": {"online": True, "ready": True, "latency_ms": 41.0}})
    hub.on_upstream_state(False)
    flags = [(m["type"], m["data"]["online"]) for m in drain(conn)]
    assert flags == [("event", True), ("snapshot", True), ("event", False)]
    assert hub.bot_state["latency_ms"] is None


async def test_diagnostics_loop_runs_only_while_subscribed(perms, monkeypatch):
    hub = DashboardHub()
    hub.diagnostics_interval = 0.01
    perms["users"][1] = {"perms": set(), "staff": True}
    perms["users"][2] = {"perms": set(), "staff": False}
    builds = {"n": 0}

    async def fake_build(viewer=None):
        builds["n"] += 1
        return {"bot": {"reachable": False}, "dashboard": {}, "n": builds["n"]}

    monkeypatch.setattr(hub, "build_diagnostics", fake_build)
    staff, plain = await connect(hub, sess(1)), await connect(hub, sess(2))
    await asyncio.sleep(0.06)
    assert builds["n"] == 0 and not hub.diagnostics_running  # nobody subscribed

    hub.subscribe(plain, ["diagnostics"])  # refused: not staff
    await asyncio.sleep(0.03)
    assert not hub.diagnostics_running and builds["n"] == 0

    hub.subscribe(staff, ["diagnostics"])
    await asyncio.sleep(0.08)
    assert hub.diagnostics_running and builds["n"] >= 2
    msgs = [m for m in drain(staff) if m["topic"] == "diagnostics"]
    assert msgs and msgs[0]["data"]["viewer"]["username"] == "user"
    assert all(m["type"] == "error" for m in drain(plain))  # only the forbidden error, never data

    hub.unsubscribe(staff, ["diagnostics"])
    await asyncio.sleep(0.03)
    frozen = builds["n"]
    await asyncio.sleep(0.08)
    assert builds["n"] == frozen and not hub.diagnostics_running  # stopped with the last subscriber
    await hub.stop()


async def test_build_diagnostics_merges_scrubs_and_marks_bot_state(perms, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "planted-internal-secret-VALUE")
    hub = DashboardHub()
    hub.upstream = FakeUpstream(connected=True)
    hub.on_upstream_message({
        "type": "snapshot", "topic": "diagnostics",
        "data": {"process": {"pid": 1}, "leak": "x planted-internal-secret-VALUE y", "as_of_note": "heavy"},
    })
    out = await hub.build_diagnostics({"user_id": "1", "username": "mod"})
    assert out["bot"]["reachable"] is True and out["bot"]["process"]["pid"] == 1
    assert out["bot"]["round_trip_ms"] == 1.5
    assert out["dashboard"]["internal_api"]["transport"] == "websocket"
    assert "planted-internal-secret-VALUE" not in str(out)
    assert out["viewer"] == {"user_id": "1", "username": "mod"}
    hub.upstream.connected = False  # bot went away: unreachable, dashboard section still present
    down = await hub.build_diagnostics()
    assert down["bot"]["reachable"] is False and "sessions" in down["dashboard"]
    # dashboard_section() opened the real SQLAlchemy/aiosqlite engine; its
    # non-daemon worker thread keeps the pytest process alive forever unless
    # the pool is disposed on this test's event loop.
    from server.db.engine import engine
    await engine.dispose()


async def test_connection_queue_is_bounded_drop_oldest():
    conn = Connection("s", 1, "u", {"stats"})
    for i in range(400):
        conn.put({"n": i})
    assert conn.queue.qsize() == 256 and conn.dropped == 144
    assert conn.queue.get_nowait()["n"] == 144
