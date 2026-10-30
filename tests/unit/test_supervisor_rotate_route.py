"""POST /api/supervisor/rotate-logs: owner only (404 for everyone else, byte-identical
to an unknown /api/* path, even for staff), requested_by from the session, works
without the bot, error mapping. The supervisor client is stubbed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import config
from server.app import app
from server.auth.dependencies import get_current_session, require_session
from server.db.models import Session
from server.services import internal_client, supervisor_client
from server.services.internal_client import InternalAPIError
from server.services.supervisor_client import SupervisorError, SupervisorUnavailable

OWNER = 111222333444555666
STAFF = 999888777666555444
ORIGIN = config.FRONTEND_BASE_URL
PATH = "/api/supervisor/rotate-logs"


def _session(user_id: int, member: bool = True) -> Session:
    s = Session()
    s.session_id = "sid"
    s.discord_user_id = user_id
    s.discord_username = "keira" if user_id == OWNER else "mod"
    s.discord_avatar = None
    s.is_guild_member = member
    s.cached_role_ids = []
    return s


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    st: dict = {"calls": [], "fail": None}

    async def rotate_logs(**kw):
        st["calls"].append(kw)
        if st["fail"]:
            raise st["fail"]
        return {"queued": True, "message": "Log rotation queued"}

    async def check_is_staff(guild_id, user_id):  # the bot is down; must not matter for this route
        raise InternalAPIError("down")

    monkeypatch.setattr(supervisor_client, "rotate_logs", rotate_logs)
    monkeypatch.setattr(internal_client, "check_is_staff", check_is_staff)
    c = TestClient(app)
    c.st = st  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def _as(user_id: int | None, member: bool = True):
    sess = _session(user_id, member) if user_id is not None else None
    app.dependency_overrides[get_current_session] = lambda: sess
    if sess is not None:
        app.dependency_overrides[require_session] = lambda: sess
    else:
        app.dependency_overrides.pop(require_session, None)


@pytest.mark.parametrize("user", [None, STAFF])
@pytest.mark.parametrize("body", [{"reason": "x"}, {"reason": "x" * 999}, None])
def test_non_owner_gets_hidden_404(client, user, body):
    _as(user)
    r = client.post(PATH, json=body, headers={"Origin": ORIGIN})
    unknown = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404 and r.content == unknown.content
    assert client.st["calls"] == []


def test_owner_not_member_gets_404(client):
    _as(OWNER, member=False)
    assert client.post(PATH, json={}, headers={"Origin": ORIGIN}).status_code == 404


def test_owner_proxies_with_session_identity_while_bot_down(client):
    _as(OWNER)
    r = client.post(PATH, json={"reason": "  logs are huge  "}, headers={"Origin": ORIGIN})
    assert r.status_code == 202 and r.json() == {"queued": True, "message": "Log rotation queued"}
    assert client.st["calls"] == [{"requested_by": f"keira ({OWNER})", "source": "dashboard", "reason": "logs are huge"}]
    r = client.post(PATH, headers={"Origin": ORIGIN})  # no body at all
    assert r.status_code == 202 and client.st["calls"][-1]["reason"] is None


def test_owner_errors_mapped(client):
    _as(OWNER)
    client.st["fail"] = SupervisorUnavailable("ConnectError")
    r = client.post(PATH, json={}, headers={"Origin": ORIGIN})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "supervisor_unavailable"
    client.st["fail"] = SupervisorError(409, {"error": "rotation_in_progress", "message": "already running"})
    r = client.post(PATH, json={}, headers={"Origin": ORIGIN})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "rotation_in_progress"
    client.st["fail"] = SupervisorError(429, {"error": "cooldown", "retry_after_s": 42, "message": "wait"})
    r = client.post(PATH, json={}, headers={"Origin": ORIGIN})
    assert r.status_code == 429 and r.headers["Retry-After"] == "42"


def test_owner_reason_too_long_is_422(client):
    _as(OWNER)
    assert client.post(PATH, json={"reason": "x" * 301}, headers={"Origin": ORIGIN}).status_code == 422
    assert client.st["calls"] == []


def test_foreign_origin_blocked(client):
    _as(OWNER)
    r = client.post(PATH, json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and client.st["calls"] == []
