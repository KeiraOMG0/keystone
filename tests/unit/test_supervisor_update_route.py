"""POST /api/supervisor/update-deps: owner only (hidden 404 for everyone else),
requested_by from the session, works without the bot, error mapping; GET
/api/supervisor/status leaves `dependency_update` out for non-owner staff. The
supervisor client is stubbed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import config
from server.app import app
from server.auth.dependencies import get_current_session, require_session
from server.db.models import Session
from server.routes import supervisor as sup_routes
from server.services import internal_client, supervisor_client
from server.services.internal_client import InternalAPIError
from server.services.supervisor_client import SupervisorError, SupervisorUnavailable

OWNER = 111222333444555666
STAFF = 999888777666555444
ORIGIN = config.FRONTEND_BASE_URL
PATH = "/api/supervisor/update-deps"


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

    async def update_deps(**kw):
        st["calls"].append(kw)
        if st["fail"]:
            raise st["fail"]
        return {"queued": True, "mode": kw["mode"], "message": "Dependency check queued"}

    async def get_status():
        return {"supervisor": {"version": "2.2"}, "services": [],
                "dependency_update": {"running": False, "last_run": {"plan": {"python": [{"package": "aiohttp"}]}}}}

    async def check_is_staff(guild_id, user_id):  # the bot is down; must not matter
        raise InternalAPIError("down")

    monkeypatch.setattr(supervisor_client, "update_deps", update_deps)
    monkeypatch.setattr(supervisor_client, "get_status", get_status)
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
@pytest.mark.parametrize("body", [{"mode": "apply"}, {"mode": "nope"}, None])
def test_non_owner_gets_hidden_404(client, user, body):
    _as(user)
    r = client.post(PATH, json=body, headers={"Origin": ORIGIN})
    unknown = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404 and r.content == unknown.content
    assert client.st["calls"] == []


def test_owner_proxies_with_session_identity_while_bot_down(client):
    _as(OWNER)
    r = client.post(PATH, json={"mode": "apply", "reason": "  weekly alert  "}, headers={"Origin": ORIGIN})
    assert r.status_code == 202 and r.json()["mode"] == "apply" and r.json()["queued"] is True
    assert client.st["calls"] == [{"mode": "apply", "requested_by": f"keira ({OWNER})", "source": "dashboard",
                                   "reason": "weekly alert"}]
    r = client.post(PATH, headers={"Origin": ORIGIN})  # no body: a check
    assert r.status_code == 202 and client.st["calls"][-1]["mode"] == "check"


def test_owner_validation_and_errors(client):
    _as(OWNER)
    assert client.post(PATH, json={"mode": "force"}, headers={"Origin": ORIGIN}).status_code == 422
    assert client.st["calls"] == []
    client.st["fail"] = SupervisorError(409, {"error": "update_in_progress", "message": "already running"})
    r = client.post(PATH, json={"mode": "check"}, headers={"Origin": ORIGIN})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "update_in_progress"
    client.st["fail"] = SupervisorError(429, {"error": "cooldown", "retry_after_s": 12, "message": "wait"})
    r = client.post(PATH, json={"mode": "check"}, headers={"Origin": ORIGIN})
    assert r.status_code == 429 and r.headers["Retry-After"] == "12"
    client.st["fail"] = SupervisorUnavailable("ConnectError")
    r = client.post(PATH, json={"mode": "check"}, headers={"Origin": ORIGIN})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "supervisor_unavailable"


def test_foreign_origin_blocked(client):
    _as(OWNER)
    r = client.post(PATH, json={"mode": "apply"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and client.st["calls"] == []


def test_status_hides_the_dependency_plan_from_non_owner_staff(client):
    async def owner_gate():
        return _session(OWNER)

    async def staff_gate():
        return _session(STAFF)

    app.dependency_overrides[sup_routes.require_staff_even_if_bot_down] = owner_gate
    assert "dependency_update" in client.get("/api/supervisor/status").json()
    app.dependency_overrides[sup_routes.require_staff_even_if_bot_down] = staff_gate
    body = client.get("/api/supervisor/status").json()
    assert "dependency_update" not in body and body["supervisor"]["version"] == "2.2"
