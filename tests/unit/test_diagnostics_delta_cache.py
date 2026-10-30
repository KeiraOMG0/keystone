"""Diagnostics bandwidth work: per-connection delta pushes (server/live/hub.py),
the bot-side TTL caches + clear_caches() (services/diagnostics_service.py) and
the "Clear cached data" routes (internal + dashboard)."""

from __future__ import annotations

import datetime

import asyncio
import copy
import json
from types import SimpleNamespace

import discord
import httpx
import pytest
from discord.ext import commands
from fastapi.testclient import TestClient

import config as bot_config
from internal_api.app import create_app
from server.app import app as dashboard_app
from server.auth.dependencies import require_session
from server.db.models import Session
from server.live import hub as hub_module
from server.live.hub import Connection, DashboardHub, diagnostics_delta
from server.services import internal_client
from server.services.internal_client import InternalAPIError
from services import diagnostics_service as ds

# async tests run via `-o asyncio_mode=auto` (the documented test command)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def apply(base: dict, delta: dict) -> dict:
    """Python mirror of applyDelta in server/frontend/src/api/diagnostics.ts."""
    out = copy.deepcopy(base)
    for path, value in delta["set"]:
        node = out
        for k in path[:-1]:
            if not isinstance(node.get(k), dict):
                node[k] = {}
            node = node[k]
        node[path[-1]] = value
    for path in delta["unset"]:
        node = out
        for k in path[:-1]:
            node = node.get(k, {})
        node.pop(path[-1], None)
    return out


def dump(cpu=1.0, db_rows=10, reachable=True, gen="t1") -> dict:
    bot = {"reachable": reachable}
    if reachable:
        bot.update(
            generated_at=gen,
            process={"pid": 1, "cpu_percent": cpu, "asyncio": {"loop_lag_ms": 0.1}},
            system={"cpu": {"system_percent": 3.0}, "net_io": {"bytes_sent": 100}},
            database={"tables": {"audit_log": db_rows}, "as_of": "x", "ttl_seconds": 60},
            config={"env": {"A": "1"}, "versions": {"discord.py": "2.4"}},
        )
    else:
        bot["error"] = "internal_api_unavailable"
    return {"generated_at": gen, "viewer": {"user_id": "1", "username": "u"}, "bot": bot,
            "dashboard": {"process": {"cpu_percent": 0.5}, "config": {"X": 1}}}


def drain(conn):
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get_nowait())
    return out


def staff_conn(hub: DashboardHub) -> Connection:
    conn = Connection("s1", 1, "u", {"diagnostics", "bot", "stats"})
    hub.add_connection(conn)
    conn.topics.add("diagnostics")
    return conn


# --------------------------------------------------------------------------
# diagnostics_delta (pure)
# --------------------------------------------------------------------------

async def test_delta_unchanged_is_empty():
    assert diagnostics_delta(dump(), dump()) == ([], [])


async def test_delta_only_changed_paths_and_roundtrip():
    old, new = dump(cpu=1.0, gen="t1"), dump(cpu=2.0, gen="t2")
    sets, unsets = diagnostics_delta(old, new)
    paths = sorted(tuple(p) for p, _ in sets)
    assert paths == [("bot", "generated_at"), ("bot", "process", "cpu_percent"), ("generated_at",)]
    assert unsets == []
    assert apply(old, {"set": sets, "unset": unsets}) == new


async def test_delta_depth_limit_sends_deeper_dicts_whole():
    old, new = dump(), dump()
    new["bot"]["system"]["cpu"]["system_percent"] = 9.0  # 4 keys deep
    sets, _ = diagnostics_delta(old, new)
    assert sets == [[["bot", "system", "cpu"], {"system_percent": 9.0}]]


async def test_delta_removed_keys_and_type_changes():
    old, new = dump(), dump(reachable=False)
    sets, unsets = diagnostics_delta(old, new)
    assert ["bot", "process"] in unsets and ["bot", "database"] in unsets
    assert apply(old, {"set": sets, "unset": unsets}) == new
    a, b = {"x": 1}, {"x": True}  # 1 == True in Python, but not on the page
    assert diagnostics_delta(a, b) == ([[["x"], True]], [])


# --------------------------------------------------------------------------
# hub: per-connection delta pushes
# --------------------------------------------------------------------------

async def test_first_push_is_full_then_deltas_then_nothing_when_unchanged():
    hub = DashboardHub()
    conn = staff_conn(hub)
    first = hub.push_diagnostics(conn, dump(gen="t1"))
    assert first["type"] == "snapshot" and first["rev"] == 1 and first["data"] == dump(gen="t1")

    d = hub.push_diagnostics(conn, dump(cpu=5.0, gen="t2"))
    assert d["type"] == "delta" and d["base_rev"] == 1 and d["rev"] == 2
    changed = {tuple(p) for p, _ in d["data"]["set"]}
    assert ("bot", "process", "cpu_percent") in changed
    assert not any(p[:2] in (("bot", "database"), ("bot", "config"), ("dashboard", "config")) for p in changed)
    assert d["data"]["unset"] == []
    # the unchanged big sections are not in the wire message at all
    assert "audit_log" not in json.dumps(d) and "discord.py" not in json.dumps(d)

    assert hub.push_diagnostics(conn, dump(cpu=5.0, gen="t2")) is None  # nothing changed: nothing sent
    msgs = drain(conn)
    assert [m["type"] for m in msgs] == ["snapshot", "delta"]
    assert apply(msgs[0]["data"], msgs[1]["data"]) == dump(cpu=5.0, gen="t2")


async def test_deltas_are_per_connection():
    hub = DashboardHub()
    a = staff_conn(hub)
    hub.push_diagnostics(a, dump(gen="t1"))
    b = staff_conn(hub)  # joins later: gets its own full snapshot
    msg_a = hub.push_diagnostics(a, dump(gen="t2"))
    msg_b = hub.push_diagnostics(b, dump(gen="t2"))
    assert msg_a["type"] == "delta" and msg_b["type"] == "snapshot" and msg_b["rev"] == 1


async def test_reset_resync_and_resubscribe_force_a_full_snapshot():
    hub = DashboardHub()
    conn = staff_conn(hub)
    hub.push_diagnostics(conn, dump(gen="t1"))

    hub.reset_diagnostics_state()  # the cache-clear button
    m = hub.push_diagnostics(conn, dump(gen="t2"))
    assert m["type"] == "snapshot" and m["rev"] == 2

    assert hub.resync(conn, ["diagnostics", "tickets", 5]) == ["diagnostics"]  # not subscribed/odd: ignored
    assert hub.push_diagnostics(conn, dump(gen="t3"))["type"] == "snapshot"

    hub.unsubscribe(conn, ["diagnostics"])
    hub.subscribe(conn, ["diagnostics"])
    assert hub.push_diagnostics(conn, dump(gen="t4"))["type"] == "snapshot"
    await hub.stop()


async def test_resync_requires_subscription_and_authorization():
    hub = DashboardHub()
    conn = Connection("s", 2, "plain", {"bot"})
    hub.add_connection(conn)
    conn.topics.add("diagnostics")  # even a tampered topic set is not enough
    assert hub.resync(conn, ["diagnostics"]) == []


async def test_loop_sends_snapshot_then_deltas_and_reset_wakes(monkeypatch):
    hub = DashboardHub()
    hub.diagnostics_interval = 0.02
    n = {"i": 0}

    async def fake_build(viewer=None):
        n["i"] += 1
        return dump(cpu=float(n["i"]), gen=f"t{n['i']}")

    monkeypatch.setattr(hub, "build_diagnostics", fake_build)

    async def allow(session, force=False):
        return {"bot", "stats", "diagnostics"}

    monkeypatch.setattr(hub, "resolve_access", allow)
    conn = Connection("s", 1, "user", {"bot", "stats", "diagnostics"})
    hub.add_connection(conn)
    hub.subscribe(conn, ["diagnostics"])
    await asyncio.sleep(0.09)
    msgs = [m for m in drain(conn) if m["topic"] == "diagnostics"]
    assert msgs[0]["type"] == "snapshot" and msgs[0]["data"]["viewer"]["username"] == "user"
    assert len(msgs) >= 2 and all(m["type"] == "delta" for m in msgs[1:])
    assert [m["base_rev"] for m in msgs[1:]] == [m["rev"] for m in msgs[:-1]]  # an unbroken chain
    state = msgs[0]["data"]
    for m in msgs[1:]:
        state = apply(state, m["data"])
    k = int(state["generated_at"][1:])
    expected = dump(cpu=float(k), gen=f"t{k}")
    expected["viewer"] = {"user_id": "1", "username": "user"}
    assert state == expected  # snapshot + deltas rebuild exactly the last pushed dump

    hub.reset_diagnostics_state()
    await asyncio.sleep(0.01)  # woken: no need to wait out the interval
    after = [m for m in drain(conn) if m["topic"] == "diagnostics"]
    assert after and after[0]["type"] == "snapshot"
    await hub.stop()


# --------------------------------------------------------------------------
# bot-side TTL caches + clear_caches()
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_cache():
    ds.reset_cache()
    yield
    ds.reset_cache()


async def test_config_cache_within_ttl_refresh_after_and_clear(monkeypatch):
    calls = {"n": 0}

    def fake_config():
        calls["n"] += 1
        return {"env": {"N": calls["n"]}, "versions": {}, "project_root": "/x"}

    monkeypatch.setattr(ds, "config_section", fake_config)
    a = await ds.config_section_cached()
    b = await ds.config_section_cached()
    assert calls["n"] == 1 and a == b and a["ttl_seconds"] == ds.TTL_CONFIG and a["as_of"]

    monkeypatch.setattr(ds, "TTL_CONFIG", 0)  # expired
    c = await ds.config_section_cached()
    assert calls["n"] == 2 and c["env"]["N"] == 2
    monkeypatch.setattr(ds, "TTL_CONFIG", 300)

    await ds.config_section_cached()
    assert calls["n"] == 2  # cached again
    log_calls = {"n": 0}
    monkeypatch.setattr(ds, "clear_log_cache", lambda: log_calls.__setitem__("n", log_calls["n"] + 1))
    res = ds.clear_caches()
    assert "config" in res["cleared"] and res["entries"] >= 1 and res["log_cache_cleared"] is True
    assert log_calls["n"] == 1
    d = await ds.config_section_cached()
    assert calls["n"] == 3 and d["env"]["N"] == 3


async def test_clear_caches_survives_missing_or_failing_log_cache(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(ds, "clear_log_cache", boom)
    assert ds.clear_caches()["log_cache_cleared"] is False
    monkeypatch.delattr(ds, "clear_log_cache")
    assert ds.clear_caches()["log_cache_cleared"] is False


async def test_discord_static_part_cached_live_part_fresh():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    bot.get_guild = lambda gid: None  # type: ignore[assignment]
    a = await ds.discord_section_cached(bot, 1)
    assert a["heavy_ttl_seconds"] == ds.TTL_DISCORD_STATIC and a["intents"] and "slash_commands" in a
    assert a["guild"]["error"]
    cogs_before = a["cogs"]

    class Extra(commands.Cog):
        pass

    await bot.add_cog(Extra())
    b = await ds.discord_section_cached(bot, 1)
    assert b["heavy_as_of"] == a["heavy_as_of"] and b["cogs"] == cogs_before  # registry served from cache
    ds.clear_caches()
    c = await ds.discord_section_cached(bot, 1)
    assert [x["name"] for x in c["cogs"]] == ["Extra"]  # recomputed after the clear
    # the uncached function still returns the same shape (used directly by tests/tools)
    assert set(ds.discord_section(bot, 1)) == set(c) - {"heavy_as_of", "heavy_ttl_seconds"}


async def test_children_and_stack_cached(monkeypatch):
    calls = {"stack": 0, "children": 0}

    def fake_stack(bot):
        calls["stack"] += 1
        return {"pids": {1: {"role": "bot", "origin": "self"}}, "listeners": [], "connections_by_state": {}, "connections_total": 0}

    def fake_children(stack):
        calls["children"] += 1
        return {"count": 1, "processes": [{"pid": 1}]}

    monkeypatch.setattr(ds, "build_stack", fake_stack)
    monkeypatch.setattr(ds, "children_section", fake_children)
    s1 = await ds.build_stack_cached(None)
    s2 = await ds.build_stack_cached(None)
    assert s1 == s2 and calls["stack"] == 1
    c1 = await ds.children_section_cached(s1)
    c2 = await ds.children_section_cached(s1)
    assert calls["children"] == 1 and c1 == c2 and c1["ttl_seconds"] == ds.TTL_CHILDREN


# --------------------------------------------------------------------------
# internal route POST /guilds/{id}/diagnostics/cache/clear
# --------------------------------------------------------------------------

SECRET = "test-internal-secret-value-2"


async def test_internal_clear_route_requires_secret_and_clears(monkeypatch):
    monkeypatch.setattr(bot_config, "INTERNAL_API_SECRET", SECRET)
    app = create_app()
    app.state.bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
    await ds.heavy("config", 300, lambda: {"x": 1})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/guilds/1/diagnostics/cache/clear")).status_code == 401
        r = await c.post(
            "/guilds/1/diagnostics/cache/clear",
            headers={"Authorization": f"Bearer {SECRET}"},
            json={"requested_by": "mod\r\n(1)"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["cleared"] == ["config"] and body["entries"] == 1 and "at" in body
    assert "config" not in ds._HEAVY


# --------------------------------------------------------------------------
# dashboard route POST /api/diagnostics/cache/clear
# --------------------------------------------------------------------------

def _session(roles=()):
    s = Session()
    s.session_id = "sid"
    s.discord_user_id = 424242
    s.discord_username = "mod"
    s.is_guild_member = True
    s.cached_role_ids = list(roles)
    s.created_at = datetime.datetime.now(datetime.timezone.utc)  # fresh login: cached roles trusted
    return s


@pytest.fixture
def dash(monkeypatch):
    st = {"staff": True, "bot_down": False, "calls": [], "resets": 0}

    async def check_is_staff(guild_id, user_id):
        if st["bot_down"]:
            raise InternalAPIError("down")
        return st["staff"]

    async def clear(guild_id, requested_by):
        if st["bot_down"]:
            raise InternalAPIError("down")
        st["calls"].append(requested_by)
        return {"cleared": ["config", "database"], "entries": 2, "log_cache_cleared": True, "at": "now"}

    def reset():
        st["resets"] += 1

    monkeypatch.setattr(internal_client, "check_is_staff", check_is_staff)
    monkeypatch.setattr(internal_client, "clear_diagnostics_cache", clear)
    monkeypatch.setattr(hub_module.hub, "reset_diagnostics_state", reset)
    dashboard_app.dependency_overrides[require_session] = lambda: _session()
    yield st
    dashboard_app.dependency_overrides.pop(require_session, None)


def test_dashboard_clear_staff_ok_resets_hub(dash):
    r = TestClient(dashboard_app).post("/api/diagnostics/cache/clear")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "cleared": ["config", "database"], "entries": 2, "log_cache_cleared": True, "at": "now"}
    assert dash["calls"] == ["mod (424242)"] and dash["resets"] == 1


def test_dashboard_clear_non_staff_403(dash):
    dash["staff"] = False
    r = TestClient(dashboard_app).post("/api/diagnostics/cache/clear")
    assert r.status_code == 403 and dash["calls"] == [] and dash["resets"] == 0


def test_dashboard_clear_unauthenticated_401():
    assert TestClient(dashboard_app).post("/api/diagnostics/cache/clear").status_code == 401


def test_dashboard_clear_bot_down_is_clear_502(dash):
    dash["bot_down"] = True
    r = TestClient(dashboard_app).post("/api/diagnostics/cache/clear")
    assert r.status_code == 502  # non-cached-staff: the guard's own 502
    assert dash["resets"] == 0


def test_dashboard_clear_bot_down_cached_staff_gets_message(dash, monkeypatch):
    dash["bot_down"] = True
    monkeypatch.setenv("STAFF_ROLE_IDS", "777")
    dashboard_app.dependency_overrides[require_session] = lambda: _session(roles=[777])
    r = TestClient(dashboard_app).post("/api/diagnostics/cache/clear")
    assert r.status_code == 502
    body = r.json()
    assert "internal_api_unavailable" in json.dumps(body) and "could not be cleared" in json.dumps(body)
    assert dash["resets"] == 0


def test_dashboard_clear_foreign_origin_blocked(dash):
    r = TestClient(dashboard_app).post("/api/diagnostics/cache/clear", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and dash["calls"] == []
