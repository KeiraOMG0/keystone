"""Dashboard backend security hardening: live-hub fallback only when the bot is
unreachable (and time-capped), `stats` topic gate, hidden owner-only routes
404 for every method/body, API docs off, cookie Secure auto mode, /api/me
is_staff + member-gone handling, /api/resolve dedupe/cap/concurrency, OAuth
tokens no longer stored (Alembic), log out everywhere. No network: every
internal-API call is stubbed."""

from __future__ import annotations

import asyncio
import datetime
import sqlite3
import time
from types import SimpleNamespace

import discord
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from server import config
from server.app import app
from server.auth import oauth
from server.auth.dependencies import get_current_session, require_session
from server.auth.session import create_session, delete_user_sessions
from server.db.base import Base
from server.db.models import Session
from server.live import hub as hub_module
from server.live.hub import TOPIC_PERMISSIONS, Connection, DashboardHub
from server.permissions import guards
from server.services import internal_client
from server.services.internal_client import InternalAPIError

OWNER = 111222333444555666
STAFF = 999888777666555444
ORIGIN = config.FRONTEND_BASE_URL

MEMBER_GONE = InternalAPIError("gone", status_code=404, detail="Member 5 not found in guild 1 (not cached and not in guild).")
UNREACHABLE = [
    InternalAPIError("down"),
    InternalAPIError("5xx", status_code=503, detail="Bot instance is not available yet."),
    InternalAPIError("guild", status_code=404, detail="Guild 1 not found (bot is not in that guild or hasn't cached it yet)."),
]
ANSWERED = [
    InternalAPIError("auth", status_code=401),
    InternalAPIError("bad", status_code=422),
    InternalAPIError("odd404", status_code=404, detail=None),
]


def sess(uid=1, member=True, roles=()):
    return SimpleNamespace(discord_user_id=uid, is_guild_member=member, cached_role_ids=list(roles), session_id=f"s{uid}")


def _session(user_id: int, member: bool = True, roles=()) -> Session:
    s = Session()
    s.session_id = "sid"
    s.discord_user_id = user_id
    s.discord_username = "u"
    s.discord_avatar = None
    s.is_guild_member = member
    s.cached_role_ids = list(roles)
    return s


@pytest.fixture
def oracle(monkeypatch):
    st = {"perms": set(), "staff": False, "error": None}

    async def bulk(guild_id, user_id, permissions):
        if st["error"] is not None:
            raise st["error"]
        return {p: p in st["perms"] for p in permissions}

    async def is_staff(guild_id, user_id):
        if st["error"] is not None:
            raise st["error"]
        return st["staff"]

    async def check_permission(guild_id, user_id, permission):
        if st["error"] is not None:
            raise st["error"]
        return permission in st["perms"]

    monkeypatch.setattr(internal_client, "check_permissions_bulk", bulk)
    monkeypatch.setattr(internal_client, "check_is_staff", is_staff)
    monkeypatch.setattr(internal_client, "check_permission", check_permission)
    return st


# ---------------------------------------------------------------- live hub (item 1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("err", UNREACHABLE)
async def test_hub_keeps_cached_grants_only_while_bot_unreachable(oracle, err):
    hub = DashboardHub()
    oracle["perms"] = {"view_audit", "staff_base"}
    first = await hub.resolve_access(sess(1))
    assert first == {"bot", "stats", "audit"}
    oracle["error"] = err
    assert await hub.resolve_access(sess(1), force=True) == first


@pytest.mark.asyncio
async def test_hub_cached_fallback_is_time_capped(oracle, monkeypatch):
    hub = DashboardHub()
    oracle["perms"] = {"view_audit", "staff_base"}
    await hub.resolve_access(sess(1))
    oracle["error"] = InternalAPIError("down")
    real = time.monotonic
    monkeypatch.setattr(hub_module.time, "monotonic", lambda: real() + hub_module.PERMISSION_FALLBACK_MAX_AGE + 1)
    assert await hub.resolve_access(sess(1), force=True) == {"bot"}


@pytest.mark.asyncio
async def test_hub_member_gone_drops_everything_and_the_cache(oracle):
    hub = DashboardHub()
    oracle["perms"], oracle["staff"] = {"view_audit", "staff_base"}, True
    await hub.resolve_access(sess(1))
    oracle["error"] = MEMBER_GONE
    assert await hub.resolve_access(sess(1), force=True) == set()
    # A later outage must not revive the grants of someone the bot said is gone.
    oracle["error"] = InternalAPIError("down")
    assert await hub.resolve_access(sess(1), force=True) == {"bot"}


@pytest.mark.asyncio
@pytest.mark.parametrize("err", ANSWERED)
async def test_hub_other_bot_answers_fail_closed(oracle, err, monkeypatch):
    monkeypatch.setenv("STAFF_ROLE_IDS", "555")
    hub = DashboardHub()
    oracle["perms"] = {"view_audit", "staff_base"}
    await hub.resolve_access(sess(1, roles=[555]))
    oracle["error"] = err
    # neither the cached grants nor the cached-role diagnostics fallback
    assert await hub.resolve_access(sess(1, roles=[555]), force=True) == {"bot"}


@pytest.mark.asyncio
async def test_hub_unreachable_cached_staff_still_gets_diagnostics(oracle, monkeypatch):
    monkeypatch.setenv("STAFF_ROLE_IDS", "555")
    monkeypatch.setenv("OWNER_ID", "0")
    oracle["error"] = InternalAPIError("down")
    assert await DashboardHub().resolve_access(sess(9, roles=[555])) == {"bot", "diagnostics"}


@pytest.mark.asyncio
async def test_stats_topic_needs_staff_base(oracle):
    assert TOPIC_PERMISSIONS["stats"] == "staff_base"
    hub = DashboardHub()
    conn = Connection("s1", 1, "u", await hub.resolve_access(sess(1)))
    res = hub.subscribe(conn, ["stats"])
    assert res["denied"] == ["stats"] and conn.topics == set()
    hub.on_upstream_message({"type": "snapshot", "topic": "stats", "data": {"total": 1}})
    assert all(m["topic"] != "stats" or m["type"] == "error" for m in list(conn.queue._queue))

    oracle["perms"] = {"staff_base"}
    conn2 = Connection("s2", 2, "u", await hub.resolve_access(sess(2)))
    assert hub.subscribe(conn2, ["stats"])["accepted"] == ["stats"]


# ---------------------------------------------------------------- live endpoint closes 4403 (item 1)

from tests.unit.test_live_dashboard_ws import (  # noqa: E402  (reuses the WS test harness)
    assert_closed_with,
    env,  # noqa: F401  (pytest fixture)
    open_ws,
    recv_until,
)


def test_ws_member_gone_closes_mid_stream(env, monkeypatch):  # noqa: F811
    env.oracle.update(perms={"moderate_members", "staff_base"}, staff=False)
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "topics": ["moderation", "stats"]})
        recv_until(ws, lambda m: m["type"] == "ack")

        async def gone(*a, **k):
            raise MEMBER_GONE

        monkeypatch.setattr(internal_client, "check_permissions_bulk", gone)
        assert_closed_with(ws, 4403)
    assert env.hub.conns == set()


def test_ws_member_gone_at_handshake_closes_4403(env, monkeypatch):  # noqa: F811
    async def gone(*a, **k):
        raise MEMBER_GONE

    monkeypatch.setattr(internal_client, "check_permissions_bulk", gone)
    with open_ws(env) as ws:
        assert_closed_with(ws, 4403)


# ---------------------------------------------------------------- hidden routes + docs (items 3, 4)


@pytest.fixture
def client(monkeypatch, oracle):
    monkeypatch.setattr(config, "OWNER_ID", OWNER)

    async def must_not_run(*a, **k):  # pragma: no cover - a call here is the bug
        raise AssertionError("hidden route body ran for a non-owner")

    from server.services import supervisor_client

    monkeypatch.setattr(supervisor_client, "rotate_logs", must_not_run)
    monkeypatch.setattr(internal_client, "run_debug_action", must_not_run)
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def _as(user_id: int | None, member: bool = True):
    s = _session(user_id, member) if user_id is not None else None
    app.dependency_overrides[get_current_session] = lambda: s
    if s is not None:
        app.dependency_overrides[require_session] = lambda: s
    else:
        app.dependency_overrides.pop(require_session, None)


HIDDEN = ["/api/debug/run", "/api/debug/actions", "/api/debug/health", "/api/debug/channels",
          "/api/debug/fortnite/preview", "/api/supervisor/rotate-logs", "/api/supervisor/update-deps"]
BODIES = [None, b"{not json", b'{"reason": 5}', b'{"reason": "' + b"x" * 400 + b'"}', b'{"action": "x", "channel_id": "1"}']


@pytest.mark.parametrize("user", [None, STAFF])
@pytest.mark.parametrize("path", HIDDEN + [p + "/" for p in HIDDEN] + ["/api/debug/fortnite/preview?refresh=notabool"])
@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_hidden_routes_are_indistinguishable_from_unknown(client, oracle, user, path, method):
    oracle["staff"] = True  # even staff
    _as(user)
    unknown = client.request(method, "/api/definitely-not-a-route", headers={"Origin": ORIGIN})
    assert unknown.status_code == 404
    for body in BODIES:
        r = client.request(method, path, content=body, headers={"Origin": ORIGIN, "Content-Type": "application/json"})
        assert (r.status_code, r.content) == (unknown.status_code, unknown.content), (method, path, body)
        assert "allow" not in {k.lower() for k in r.headers}


def test_unknown_api_path_any_method_is_bare_404(client):
    for method in ("GET", "POST", "DELETE"):
        r = client.request(method, "/api/nope/x", headers={"Origin": ORIGIN})
        assert r.status_code == 404 and r.json() == {"error": "not_found"}


def test_owner_still_gets_validation_errors(client):
    _as(OWNER)
    r = client.post("/api/supervisor/rotate-logs", content=b"{not json", headers={"Origin": ORIGIN})
    assert r.status_code == 422 and r.json()["detail"] == {"error": "invalid_request"}
    r = client.post("/api/debug/run", json={"action": "x"}, headers={"Origin": ORIGIN})
    assert r.status_code == 422


def test_real_routes_still_answer(client, oracle):
    _as(STAFF)
    oracle["perms"] = {"staff_base"}

    async def stats(guild_id):
        return {"members": 1}

    import server.services.internal_client as ic

    orig = ic.get_guild_stats
    ic.get_guild_stats = stats
    try:
        assert client.get("/api/dashboard/stats").json() == {"members": 1}
    finally:
        ic.get_guild_stats = orig


def test_api_docs_disabled_by_default():
    assert config.API_DOCS_ENABLED is False
    assert app.openapi_url is None and app.docs_url is None and app.redoc_url is None
    c = TestClient(app)
    r = c.get("/openapi.json")
    assert "openapi" not in r.text[:200]
    assert "swagger" not in c.get("/docs").text.lower()
    assert "redoc" not in c.get("/redoc").text.lower()


# ---------------------------------------------------------------- cookies (item 5)


def _req(scheme="http", peer="127.0.0.1", xfp=None):
    headers = [(b"host", b"x")]
    if xfp:
        headers.append((b"x-forwarded-proto", xfp.encode()))
    return Request({"type": "http", "scheme": scheme, "method": "GET", "path": "/", "query_string": b"",
                    "headers": headers, "client": (peer, 1234), "server": ("x", 80)})


@pytest.mark.parametrize("raw,mode", [(None, "auto"), ("", "auto"), ("auto", "auto"), ("weird", "auto"),
                                      ("true", "true"), ("1", "true"), ("false", "false"), ("no", "false")])
def test_cookie_secure_mode_parsing(raw, mode):
    assert config._cookie_secure_mode(raw) == mode


@pytest.mark.parametrize("req,expected", [
    (dict(scheme="http"), False),                                        # http://localhost dev
    (dict(scheme="https"), True),                                        # uvicorn already applied the proxy header
    (dict(scheme="http", peer="127.0.0.1", xfp="https"), True),          # cloudflared on this machine
    (dict(scheme="http", peer="::1", xfp="https,http"), True),
    (dict(scheme="http", peer="203.0.113.9", xfp="https"), False),       # header from a non-loopback peer ignored
])
def test_cookie_secure_auto(monkeypatch, req, expected):
    monkeypatch.setattr(config, "COOKIE_SECURE_MODE", "auto")
    assert oauth.cookie_secure(_req(**req)) is expected


def test_cookie_secure_forced(monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE_MODE", "true")
    assert oauth.cookie_secure(_req(scheme="http")) is True
    monkeypatch.setattr(config, "COOKIE_SECURE_MODE", "false")
    assert oauth.cookie_secure(_req(scheme="https")) is False


def test_login_state_cookie_secure_follows_scheme(monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE_MODE", "auto")
    oauth.limiter.reset()
    https = TestClient(app, base_url="https://dash.example").get("/api/auth/login", follow_redirects=False)
    http = TestClient(app, base_url="http://localhost").get("/api/auth/login", follow_redirects=False)
    assert https.status_code == http.status_code == 302
    assert "secure" in https.headers["set-cookie"].lower()
    assert "secure" not in http.headers["set-cookie"].lower()


# ---------------------------------------------------------------- /api/me + guards (items 6, 7)


@pytest.mark.parametrize("staff,owner,expected", [(True, False, True), (False, False, False), (False, True, True)])
def test_me_is_staff(oracle, monkeypatch, staff, owner, expected):
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    oracle["staff"] = staff
    app.dependency_overrides[require_session] = lambda: _session(OWNER if owner else STAFF)
    try:
        body = TestClient(app).get("/api/me").json()
    finally:
        app.dependency_overrides.pop(require_session, None)
    assert body["is_staff"] is expected


def test_me_is_staff_false_when_staff_check_fails(oracle, monkeypatch):
    async def boom(*a, **k):
        raise InternalAPIError("down")

    monkeypatch.setattr(internal_client, "check_is_staff", boom)
    app.dependency_overrides[require_session] = lambda: _session(STAFF)
    try:
        r = TestClient(app).get("/api/me")
    finally:
        app.dependency_overrides.pop(require_session, None)
    assert r.status_code == 200 and r.json()["is_staff"] is False


@pytest.mark.parametrize("err,status,code", [
    (MEMBER_GONE, 403, "not_a_member"),
    (UNREACHABLE[0], 502, "internal_api_unavailable"),
    (UNREACHABLE[2], 502, "internal_api_unavailable"),
    (ANSWERED[0], 502, "internal_api_unavailable"),  # /api/me never logs out / never says not-a-member on these
])
def test_me_member_gone_is_not_bot_offline(oracle, err, status, code):
    oracle["error"] = err
    app.dependency_overrides[require_session] = lambda: _session(STAFF)
    try:
        r = TestClient(app).get("/api/me")
    finally:
        app.dependency_overrides.pop(require_session, None)
    assert r.status_code == status and r.json()["detail"]["error"] == code


@pytest.mark.parametrize("err,status,code", [(MEMBER_GONE, 403, "not_a_member"), (UNREACHABLE[1], 502, "internal_api_unavailable")])
def test_require_permission_member_gone(oracle, err, status, code):
    oracle["error"] = err
    app.dependency_overrides[require_session] = lambda: _session(STAFF)
    try:
        r = TestClient(app).get("/api/dashboard/stats")
    finally:
        app.dependency_overrides.pop(require_session, None)
    assert r.status_code == status and r.json()["detail"]["error"] == code


@pytest.mark.asyncio
async def test_require_staff_member_gone_is_403_with_cause(oracle):
    from fastapi import HTTPException

    oracle["error"] = MEMBER_GONE
    with pytest.raises(HTTPException) as info:
        await guards.require_staff(_session(STAFF))
    assert info.value.status_code == 403 and info.value.detail["error"] == "forbidden"
    assert info.value.__cause__ is MEMBER_GONE


def test_classifiers():
    assert all(guards.bot_unreachable(e) for e in UNREACHABLE)
    assert not any(guards.bot_unreachable(e) for e in ANSWERED + [MEMBER_GONE])
    assert guards.member_gone(MEMBER_GONE) and not any(guards.member_gone(e) for e in UNREACHABLE + ANSWERED)


@pytest.mark.asyncio
async def test_check_permissions_bulk_keeps_detail(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(404, json={"detail": "Member 5 not found in guild 1 (not cached and not in guild)."})

    monkeypatch.setattr(internal_client, "_client", lambda: httpx.AsyncClient(
        base_url="http://internal.test", transport=httpx.MockTransport(handler)))
    with pytest.raises(InternalAPIError) as info:
        await internal_client.check_permissions_bulk(1, 5, ["staff_base"])
    assert guards.member_gone(info.value)
    with pytest.raises(InternalAPIError) as info:
        await internal_client.check_permission(1, 5, "staff_base")
    assert guards.member_gone(info.value)


# ---------------------------------------------------------------- /api/resolve (item 8)


class _FakeGuild:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.fetched: list[int] = []

    def get_member(self, uid):
        return None

    async def query_members(self, user_ids, limit):
        return []

    async def fetch_member(self, uid):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.fetched.append(uid)
        await asyncio.sleep(0.01)
        self.active -= 1
        if uid % 2:
            raise _NotFound()  # left the guild: definitive null
        raise RuntimeError("transient")  # e.g. Discord 5xx: id left out so the frontend retries


class _NotFound(discord.NotFound):
    def __init__(self):  # skip HTTPException's response parsing
        pass


@pytest.mark.asyncio
async def test_internal_resolve_limits_concurrency_and_dedupes():
    from internal_api.routes import resolve as internal_resolve

    guild = _FakeGuild()
    body = internal_resolve.ResolveRequest(user_ids=[i % 40 + 1 for i in range(100)])
    out = await internal_resolve.resolve_ids(body, guild=guild)
    # odd ids: NotFound -> null; even ids: transient error -> omitted
    assert sorted(int(k) for k in out["users"]) == list(range(1, 41, 2))
    assert all(v is None for v in out["users"].values())
    assert sorted(guild.fetched) == list(range(1, 41))  # each id fetched once
    assert guild.peak <= internal_resolve._FETCH_CONCURRENCY


def test_dashboard_resolve_caps_and_dedupes(oracle, monkeypatch):
    oracle["perms"] = {"staff_base"}
    seen = {}

    async def resolve_ids(guild_id, users, roles, channels):
        seen["users"] = users
        return {"users": {}, "roles": {}, "channels": {}}

    monkeypatch.setattr(internal_client, "resolve_ids", resolve_ids)
    app.dependency_overrides[require_session] = lambda: _session(STAFF)
    c = TestClient(app)
    try:
        r = c.post("/api/resolve", json={"user_ids": ["5", "5", "6"]}, headers={"Origin": ORIGIN})
        assert r.status_code == 200 and seen["users"] == [5, 6]
        seen.clear()
        r = c.post("/api/resolve", json={"user_ids": [str(i) for i in range(1, 102)]}, headers={"Origin": ORIGIN})
        assert r.status_code == 400 and r.json()["detail"] == {"error": "invalid_request"} and not seen
        r = c.post("/api/resolve", json={"user_ids": ["12abc"]}, headers={"Origin": ORIGIN})
        assert r.status_code == 400
    finally:
        app.dependency_overrides.pop(require_session, None)


# ---------------------------------------------------------------- tokens + logout-all (items 9, 10)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_tokens_are_not_stored(factory):
    async with factory() as db:
        s = await create_session(db, discord_user_id=1, discord_username="u", discord_avatar=None,
                                 discord_access_token="SECRET_A", discord_refresh_token="SECRET_R",
                                 token_expires_at=datetime.datetime.now(datetime.timezone.utc),
                                 is_guild_member=True, cached_role_ids=[])
        assert s.discord_access_token is None and s.discord_refresh_token is None and s.token_expires_at is None


def test_callback_does_not_pass_tokens_to_the_session_store():
    import inspect

    src = inspect.getsource(oauth.callback)
    assert "refresh_token" not in src and "discord_access_token" not in src


def test_alembic_migration_nulls_existing_tokens(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "dash.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))  # env.py reads it: never the real DB
    cfg = Config()  # no ini file: env.py then skips fileConfig (would reset test logging)
    cfg.set_main_option("script_location", str(__import__("pathlib").Path(config.__file__).parent / "alembic"))
    command.upgrade(cfg, "77e8789ecc98")
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO sessions VALUES ('sid', 1, 'u', NULL, 'TOKEN_A', 'TOKEN_R', '2026-01-01', 1, '[]', '2026-01-01', '2026-01-01')"
    )
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT discord_access_token, discord_refresh_token, token_expires_at, discord_user_id FROM sessions").fetchone()
        assert row == (None, None, None, 1)
        cols = {r[1]: r[3] for r in con.execute("PRAGMA table_info(sessions)")}
        assert cols["discord_access_token"] == 0 and cols["token_expires_at"] == 0  # nullable now
        assert any("discord_user_id" in r[1] for r in con.execute("PRAGMA index_list(sessions)"))
    finally:
        con.close()
    command.downgrade(cfg, "77e8789ecc98")  # downgrade path works too


@pytest.mark.asyncio
async def test_delete_user_sessions_only_that_user(factory):
    async with factory() as db:
        for uid in (1, 1, 2):
            await create_session(db, discord_user_id=uid, discord_username="u", discord_avatar=None,
                                 is_guild_member=True, cached_role_ids=[])
        ids = await delete_user_sessions(db, 1)
        assert len(ids) == 2
        from sqlalchemy import select

        left = (await db.execute(select(Session.discord_user_id))).scalars().all()
        assert left == [2]


def test_logout_all_route(monkeypatch):
    calls = {}

    async def fake_delete(db, uid):
        calls["uid"] = uid
        return ["a", "b"]

    closed = []

    async def fake_close(sid, code=4401):
        closed.append(sid)
        return 1

    from server.live.hub import hub as live_hub

    monkeypatch.setattr(oauth, "delete_user_sessions", fake_delete)
    monkeypatch.setattr(live_hub, "close_session", fake_close)

    async def no_db():
        yield None

    app.dependency_overrides[oauth._get_db] = no_db
    app.dependency_overrides[get_current_session] = lambda: _session(STAFF)
    try:
        r = TestClient(app).post("/api/auth/logout-all", headers={"Origin": ORIGIN})
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and r.json() == {"ok": True, "sessions_ended": 2}
    assert calls["uid"] == STAFF and closed == ["a", "b"]
    assert config.SESSION_COOKIE_NAME in r.headers["set-cookie"]
