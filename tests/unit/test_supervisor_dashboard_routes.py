"""/api/supervisor/*: staff gate (incl. the bot-down fallback), session-derived
requested_by, error mapping, scrubbing. The supervisor client is stubbed."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from server import config
from server.app import app
from server.auth.dependencies import require_session
from server.db.models import Session
from server.services import internal_client, supervisor_client
from server.services.internal_client import InternalAPIError
from server.services.supervisor_client import SupervisorError, SupervisorUnavailable

STAFF_ROLE = 777000111


def _session(roles=()):
    s = Session()
    s.session_id = "sid"
    s.discord_user_id = 123456789012345678
    s.discord_username = "keira"
    s.is_guild_member = True
    s.cached_role_ids = list(roles)
    s.created_at = datetime.datetime.now(datetime.timezone.utc)  # fresh login: cached roles trusted
    return s


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def st(monkeypatch):
    st = {"staff": True, "bot_down": False, "roles": [], "calls": [], "fail": None,
          "status": {"supervisor": {"pid": 1, "control": True}, "services": []}}

    async def check_is_staff(guild_id, user_id):
        if st.get("bot_error") is not None:
            raise st["bot_error"]
        if st["bot_down"]:
            raise InternalAPIError("down")
        return st["staff"]

    async def get_status():
        if st["fail"]:
            raise st["fail"]
        return st["status"]

    async def request_restart(target, **kw):
        st["calls"].append((target, kw))
        if st["fail"]:
            raise st["fail"]
        return {"accepted": [target], "skipped": [], "delay_s": 1.5, "eta_s": 3.5}

    async def get_logs(service, lines):
        if st["fail"]:
            raise st["fail"]
        return {"service": service, "lines": ["hello", f"secret {config.SESSION_COOKIE_SECRET}"]}

    monkeypatch.setattr(internal_client, "check_is_staff", check_is_staff)
    monkeypatch.setattr(supervisor_client, "get_status", get_status)
    monkeypatch.setattr(supervisor_client, "request_restart", request_restart)
    monkeypatch.setattr(supervisor_client, "get_logs", get_logs)
    app.dependency_overrides[require_session] = lambda: _session(st["roles"])
    yield st
    app.dependency_overrides.pop(require_session, None)


def test_status_for_staff(client, st):
    r = client.get("/api/supervisor/status")
    assert r.status_code == 200 and r.json()["supervisor"]["control"] is True


def test_non_staff_forbidden(client, st):
    st["staff"] = False
    assert client.get("/api/supervisor/status").status_code == 403
    r = client.post("/api/supervisor/restart", json={"target": "bot"})
    assert r.status_code == 403 and st["calls"] == []


def test_bot_down_uses_cached_staff_roles(client, st, monkeypatch):
    st["bot_down"] = True
    monkeypatch.setenv("STAFF_ROLE_IDS", str(STAFF_ROLE))
    monkeypatch.setenv("OWNER_ID", "1")
    assert client.get("/api/supervisor/status").status_code == 502  # not staff by cached roles
    st["roles"] = [STAFF_ROLE]
    r = client.post("/api/supervisor/restart", json={"target": "bot", "reason": "hung"})
    assert r.status_code == 202 and r.json()["accepted"] == ["bot"]


def test_bot_down_stale_cached_roles_refused(client, st, monkeypatch):
    """Roles cached at a login older than CACHED_STAFF_MAX_AGE, never re-confirmed
    by the bot, no longer unlock the supervisor while the bot is down."""
    from server.routes import diagnostics
    monkeypatch.setattr(diagnostics, "_staff_confirmed_at", {})
    st["bot_down"] = True
    st["roles"] = [STAFF_ROLE]
    monkeypatch.setenv("STAFF_ROLE_IDS", str(STAFF_ROLE))
    monkeypatch.setenv("OWNER_ID", "1")
    sess = _session([STAFF_ROLE])
    sess.created_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
    assert not diagnostics._cached_staff_is_fresh(sess)
    diagnostics._staff_confirmed_at[sess.discord_user_id] = __import__("time").monotonic()
    assert diagnostics._cached_staff_is_fresh(sess)  # confirmed live by the bot recently


def test_restart_uses_session_identity(client, st):
    r = client.post("/api/supervisor/restart", json={"target": "dashboard", "reason": "  ui stuck  "})
    assert r.status_code == 202 and r.json()["eta_s"] == 3.5
    target, kw = st["calls"][0]
    assert target == "dashboard" and kw["source"] == "dashboard" and kw["reason"] == "ui stuck"
    assert kw["requested_by"] == "keira (123456789012345678)"


def test_invalid_target_rejected(client, st):
    assert client.post("/api/supervisor/restart", json={"target": "everything"}).status_code == 422
    assert client.post("/api/supervisor/restart", json={"target": "bot", "reason": "x" * 301}).status_code == 422
    assert st["calls"] == []


def test_csrf_foreign_origin_blocked(client, st):
    r = client.post("/api/supervisor/restart", json={"target": "bot"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"] == "csrf_check_failed" and st["calls"] == []


def test_unavailable_maps_to_503(client, st):
    st["fail"] = SupervisorUnavailable("ConnectError")
    for r in (client.get("/api/supervisor/status"), client.post("/api/supervisor/restart", json={"target": "bot"})):
        assert r.status_code == 503 and r.json()["detail"]["error"] == "supervisor_unavailable"


def test_cooldown_and_conflict_pass_through(client, st):
    st["fail"] = SupervisorError(429, {"error": "cooldown", "retry_after_s": 17, "message": "try again in 17s"})
    r = client.post("/api/supervisor/restart", json={"target": "bot"})
    assert r.status_code == 429 and r.json()["detail"]["retry_after_s"] == 17 and r.headers["Retry-After"] == "17"
    st["fail"] = SupervisorError(409, {"error": "not_managed", "message": "port 8080 is held"})
    r = client.post("/api/supervisor/restart", json={"target": "dashboard"})
    assert r.status_code == 409 and r.json()["detail"] == {"error": "not_managed", "message": "port 8080 is held"}
    st["fail"] = SupervisorError(401, {"error": "unauthorized"})
    r = client.get("/api/supervisor/status")
    assert r.status_code == 502 and r.json()["detail"] == {"error": "supervisor_error"}


def test_logs_scrubbed_and_validated(client, st):
    r = client.get("/api/supervisor/logs?service=bot&lines=10")
    assert r.status_code == 200
    assert config.SESSION_COOKIE_SECRET not in r.text
    assert client.get("/api/supervisor/logs?service=evil").status_code == 422
    assert client.get("/api/supervisor/logs?service=bot&lines=999").status_code == 422


# ---------------------------------------------------------------------------
# The cached-role fallback applies only when the bot genuinely cannot answer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("err", [
    InternalAPIError("timeout"),  # no HTTP answer at all
    InternalAPIError("not ready", status_code=503, detail="Bot instance is not available yet."),
    InternalAPIError("boom", status_code=500),
    InternalAPIError("guild", status_code=404,
                     detail="Guild 1 not found (bot is not in that guild or hasn't cached it yet)."),
])
def test_fallback_when_bot_cannot_answer(client, st, monkeypatch, err):
    monkeypatch.setenv("STAFF_ROLE_IDS", str(STAFF_ROLE))
    st["roles"] = [STAFF_ROLE]
    st["bot_error"] = err
    assert client.get("/api/supervisor/status").status_code == 200


@pytest.mark.parametrize("err", [
    InternalAPIError("gone", status_code=404,
                     detail="Member 123456789012345678 not found in guild 1 (not cached and not in guild)."),
    InternalAPIError("gone", status_code=404, detail=None),  # unrecognised 404: fail closed
    InternalAPIError("auth", status_code=401),
    InternalAPIError("bad", status_code=422),
])
def test_no_fallback_when_bot_answers(client, st, monkeypatch, err):
    """A kicked/banned ex-staff member with a still-valid session and staff
    roles cached at login must NOT pass while the bot is up."""
    monkeypatch.setenv("STAFF_ROLE_IDS", str(STAFF_ROLE))
    monkeypatch.setenv("OWNER_ID", "123456789012345678")
    st["roles"] = [STAFF_ROLE]
    st["bot_error"] = err
    r = client.post("/api/supervisor/restart", json={"target": "all"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "forbidden"
    assert client.get("/api/supervisor/logs?service=dashboard").status_code == 403
    assert st["calls"] == []


async def test_check_is_staff_keeps_internal_api_detail(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(404, json={"detail": "Member 5 not found in guild 1 (not cached and not in guild)."})

    monkeypatch.setattr(internal_client, "_client", lambda: httpx.AsyncClient(
        base_url="http://internal.test", transport=httpx.MockTransport(handler)))
    with pytest.raises(InternalAPIError) as info:
        await internal_client.check_is_staff(1, 5)
    assert info.value.status_code == 404 and info.value.detail.startswith("Member ")
