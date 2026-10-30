"""Dashboard backend proxy for the Roles page (server/routes/role_admin.py),
with the internal client mocked. No network."""

from __future__ import annotations

import pytest

ACTOR = 424242424242424242

@pytest.fixture
def dash(monkeypatch):
    from fastapi.testclient import TestClient

    from server import config as sconfig
    from server.app import app
    from server.auth.dependencies import require_session
    from server.db.models import Session
    from server.services import internal_client

    state = {"staff": True, "calls": []}

    async def fake_is_staff(guild_id, user_id):
        return state["staff"]

    async def fake_session():
        s = Session()
        s.session_id = "s"
        s.discord_user_id = ACTOR
        s.discord_username = "keira"
        s.is_guild_member = True
        return s

    monkeypatch.setattr(internal_client, "check_is_staff", fake_is_staff)
    app.dependency_overrides[require_session] = fake_session
    client = TestClient(app, headers={"Origin": sconfig.FRONTEND_BASE_URL})
    client.state = state
    client.ic = internal_client
    yield client
    app.dependency_overrides.pop(require_session, None)


def test_dashboard_requires_staff(dash, monkeypatch):
    dash.state["staff"] = False
    r = dash.get("/api/roles")
    assert r.status_code == 403 and r.json()["detail"]["error"] == "forbidden"


def test_dashboard_passes_actor_from_session(dash, monkeypatch):
    seen = {}

    async def fake_create(guild_id, payload):
        seen.update(payload)
        return {"role": {"id": "1"}, "warnings": []}

    monkeypatch.setattr(dash.ic, "create_role_admin", fake_create)
    r = dash.post("/api/roles", json={"name": "Helpers", "actor_id": 1, "actor_name": "spoof"})
    assert r.status_code == 201
    assert seen["actor_id"] == ACTOR and seen["actor_name"] == "keira"


def test_dashboard_translates_errors(dash, monkeypatch):
    from server.services.internal_client import InternalAPIError

    async def boom403(*a, **k):
        raise InternalAPIError("x", status_code=403, detail={"error": "permission_not_held", "message": "m"})

    async def boom409(*a, **k):
        raise InternalAPIError("x", status_code=409, detail={"error": "role_not_manageable", "message": "m"})

    async def down(*a, **k):
        raise InternalAPIError("x")

    monkeypatch.setattr(dash.ic, "update_role_admin", boom403)
    r = dash.patch("/api/roles/123456789012345678", json={"name": "x"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "permission_not_held"
    monkeypatch.setattr(dash.ic, "move_role_admin", boom409)
    r = dash.post("/api/roles/123456789012345678/move", json={"position": 2})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "role_not_manageable"
    monkeypatch.setattr(dash.ic, "get_role_admin_overview", down)
    assert dash.get("/api/roles").json()["detail"]["error"] == "internal_api_unavailable"
    assert dash.patch("/api/roles/abc", json={"name": "x"}).status_code == 400


def test_dashboard_overview_and_duplicate_pass_session_actor(dash, monkeypatch):
    seen = {}

    async def fake_overview(guild_id, actor_id=None):
        seen["overview_actor"] = actor_id
        return {"roles": [], "actor": None}

    async def fake_duplicate(guild_id, role_id, payload):
        seen["duplicate"] = payload
        return {"role": {"id": "1"}}

    monkeypatch.setattr(dash.ic, "get_role_admin_overview", fake_overview)
    monkeypatch.setattr(dash.ic, "duplicate_role_admin", fake_duplicate)
    assert dash.get("/api/roles").status_code == 200
    assert seen["overview_actor"] == ACTOR
    r = dash.post("/api/roles/123456789012345678/duplicate", json={"confirm_administrator": True, "actor_id": 1})
    assert r.status_code == 201
    assert seen["duplicate"]["confirm_administrator"] is True and seen["duplicate"]["actor_id"] == ACTOR
