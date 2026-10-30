"""GET /api/diagnostics: staff gating, bot-down fallback, no session/token leakage."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server import config
from server.app import app
from server.auth.dependencies import require_session
from server.db.base import Base
from server.db.models import Session
from server.routes import diagnostics as route
from server.services import internal_client
from server.services.internal_client import InternalAPIError

ACCESS = "ACCESS_TOKEN_SENTINEL_aaaaaaaaaaaa"
REFRESH = "REFRESH_TOKEN_SENTINEL_bbbbbbbbbbbb"
SESSION_ID = "SESSION_ID_SENTINEL_cccccccccccc"
STAFF_ROLE = 555000111


def _session(member: bool = True, roles=()):
    s = Session()
    s.session_id = SESSION_ID
    s.discord_user_id = 424242
    s.discord_username = "mod"
    s.is_guild_member = member
    s.cached_role_ids = list(roles)
    return s


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def state(monkeypatch):
    """Controllable staff flag / bot availability; session dependency overridden."""
    st = {"staff": True, "bot_down": False, "member": True, "roles": [], "dump": {"process": {"pid": 1}, "config": {"env": {}}}}

    async def check_is_staff(guild_id, user_id):
        if st["bot_down"]:
            raise InternalAPIError("down")
        return st["staff"]

    async def get_diag(guild_id):
        if st["bot_down"]:
            raise InternalAPIError("down")
        return st["dump"]

    monkeypatch.setattr(internal_client, "check_is_staff", check_is_staff)
    monkeypatch.setattr(internal_client, "get_diagnostics", get_diag)
    app.dependency_overrides[require_session] = lambda: _session(st["member"], st["roles"])
    yield st
    app.dependency_overrides.pop(require_session, None)


@pytest.fixture(autouse=True)
async def _temp_dashboard_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        now = datetime.datetime.now(datetime.timezone.utc)
        db.add(Session(
            session_id=SESSION_ID, discord_user_id=424242, discord_username="mod",
            discord_access_token=ACCESS, discord_refresh_token=REFRESH,
            token_expires_at=now, is_guild_member=True, cached_role_ids=[], created_at=now, last_seen_at=now,
        ))
        await db.commit()
    monkeypatch.setattr(route, "async_session_factory", factory)
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(tmp_path / "d.db"))
    yield
    await engine.dispose()


def test_unauthenticated_is_401(client):
    r = client.get("/api/diagnostics")
    assert r.status_code == 401 and r.json()["detail"]["error"] == "unauthenticated"


def test_not_a_member_is_403(client, state):
    state["member"] = False
    r = client.get("/api/diagnostics")
    assert r.status_code == 403 and r.json()["detail"]["error"] == "not_a_member"


def test_non_staff_is_403(client, state):
    state["staff"] = False
    r = client.get("/api/diagnostics")
    assert r.status_code == 403 and r.json()["detail"]["error"] == "forbidden"


def test_staff_gets_merged_dump_without_session_secrets(client, state, monkeypatch):
    monkeypatch.setattr(config, "SESSION_COOKIE_SECRET", "sess-cookie-secret-VALUE")
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "internal-secret-VALUE")
    monkeypatch.setattr(config, "DISCORD_CLIENT_SECRET", "client-secret-VALUE")
    state["dump"] = {"process": {"pid": 1}, "leak": "oops internal-secret-VALUE inside"}
    r = client.get("/api/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert body["bot"]["reachable"] is True and body["bot"]["process"]["pid"] == 1
    d = body["dashboard"]
    assert d["sessions"]["total"] == 1 and d["sessions"]["active_24h"] == 1
    assert d["sessions"]["by_user"][0]["username"] == "mod"
    # who + when only: no per-user count (expired sessions are deleted, so it was never a lifetime counter)
    assert set(d["sessions"]["by_user"][0]) == {"user_id", "username", "last_seen"}
    assert d["database"]["size_bytes"] is not None
    assert d["internal_api"]["reachable"] is True
    assert d["config"]["SESSION_COOKIE_SECRET"] == "set" and d["config"]["INTERNAL_API_SECRET"] == "set"
    assert "pid" in d["process"] or "error" in d["process"]  # psutil optional
    assert any(x["path"] == "/api/diagnostics" for x in d["routes"])
    for leaked in (ACCESS, REFRESH, SESSION_ID, "sess-cookie-secret-VALUE", "internal-secret-VALUE", "client-secret-VALUE"):
        assert leaked not in r.text


def test_bot_down_still_returns_dashboard_section(client, state, monkeypatch):
    monkeypatch.setenv("STAFF_ROLE_IDS", f"1,{STAFF_ROLE}")
    state["bot_down"] = True
    state["roles"] = [STAFF_ROLE]  # cached staff role: fallback guard admits
    r = client.get("/api/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert body["bot"]["reachable"] is False and body["bot"]["error"] == "internal_api_unavailable"
    assert body["dashboard"]["sessions"]["total"] == 1
    assert body["dashboard"]["internal_api"]["reachable"] is False


def test_bot_down_non_staff_still_502_not_leaked(client, state, monkeypatch):
    monkeypatch.setenv("STAFF_ROLE_IDS", f"1,{STAFF_ROLE}")
    monkeypatch.setenv("OWNER_ID", "1")
    state["bot_down"] = True
    state["roles"] = [999]  # not a staff role
    r = client.get("/api/diagnostics")
    assert r.status_code == 502 and r.json()["detail"]["error"] == "internal_api_unavailable"
    assert "dashboard" not in r.json()


def test_bot_up_but_dump_fails_returns_dashboard_only(client, state, monkeypatch):
    async def fail(guild_id):
        raise InternalAPIError("boom", status_code=500)

    monkeypatch.setattr(internal_client, "get_diagnostics", fail)
    r = client.get("/api/diagnostics")
    assert r.status_code == 200
    assert r.json()["bot"]["reachable"] is False and r.json()["bot"]["status_code"] == 500


def test_request_metrics_middleware_records_route_templates(client, state):
    before = route.METRICS.total
    client.get("/api/diagnostics")
    assert route.METRICS.total > before
    routes = [r["route"] for r in route.METRICS.snapshot()["routes"]]
    assert "GET /api/diagnostics" in routes


def test_cmdline_redaction_in_dashboard_process():
    out = route._redact_cmdline(["cloudflared", "tunnel", "run", "--token", "eyABC.secret", "--metrics=127.0.0.1:20241"])
    assert "eyABC.secret" not in " ".join(out) and "--metrics=127.0.0.1:20241" in out
