"""Dashboard Settings proxy (server/routes/settings.py) with the internal
client mocked: local mirror validation (422 per-field), 4xx passthrough from
the bot, 502 only when the bot is unreachable, actor from the session."""

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

    async def fake_update(guild_id, values, updated_by):
        state["calls"].append((values, updated_by))
        return {"values": {}, "changed": list(values)}

    monkeypatch.setattr(internal_client, "check_is_staff", fake_is_staff)
    monkeypatch.setattr(internal_client, "update_settings", fake_update)
    app.dependency_overrides[require_session] = fake_session
    client = TestClient(app, headers={"Origin": sconfig.FRONTEND_BASE_URL})
    client.state = state
    client.ic = internal_client
    yield client
    app.dependency_overrides.pop(require_session, None)


def test_requires_staff(dash):
    dash.state["staff"] = False
    assert dash.get("/api/settings").status_code == 403
    assert dash.put("/api/settings", json={"values": {"auto_kick": False}}).status_code == 403


@pytest.mark.parametrize("values,bad", [
    ({"owner_id": "123456789012345678"}, "owner_id"),
    ({"discord_token": "x"}, "discord_token"),
    ({"ticket_archive_purge_days": 0}, "ticket_archive_purge_days"),
    ({"check_interval_minutes": 2000}, "check_interval_minutes"),
    ({"music_default_volume": 150}, "music_default_volume"),
    ({"music_default_search": "bing"}, "music_default_search"),
    ({"auto_kick": "yes"}, "auto_kick"),
    ({"log_channel_id": 12}, "log_channel_id"),
    ({"verify_template_name": "  "}, "verify_template_name"),
])
def test_local_validation_422_without_calling_bot(dash, values, bad):
    r = dash.put("/api/settings", json={"values": values})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "validation_failed" and bad in detail["fields"]
    assert dash.state["calls"] == []


def test_valid_put_forwards_with_session_actor(dash):
    r = dash.put("/api/settings", json={"values": {"auto_kick": "false", "log_channel_id": "1200000000000000002",
                                                   "check_interval_minutes": None}, "updated_by": 1})
    assert r.status_code == 200
    values, actor = dash.state["calls"][0]
    assert actor == ACTOR
    assert values == {"auto_kick": "false", "log_channel_id": "1200000000000000002", "check_interval_minutes": None}


def test_empty_put_is_400(dash):
    assert dash.put("/api/settings", json={"values": {}}).status_code == 400


def test_bot_errors_pass_through(dash, monkeypatch):
    from server.services.internal_client import InternalAPIError

    def raiser(status_code, detail=None):
        async def f(*a, **k):
            raise InternalAPIError("x", status_code=status_code, detail=detail)
        return f

    fields = {"log_channel_id": "That channel is not a text channel."}
    monkeypatch.setattr(dash.ic, "update_settings",
                        raiser(422, {"error": "validation_failed", "message": "m", "fields": fields}))
    r = dash.put("/api/settings", json={"values": {"log_channel_id": "1200000000000000001"}})
    assert r.status_code == 422 and r.json()["detail"]["fields"] == fields

    monkeypatch.setattr(dash.ic, "update_settings", raiser(503, {"error": "guild_unavailable", "message": "m"}))
    r = dash.put("/api/settings", json={"values": {"log_channel_id": "1200000000000000001"}})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "guild_unavailable"

    monkeypatch.setattr(dash.ic, "update_settings", raiser(403))
    r = dash.put("/api/settings", json={"values": {"auto_kick": True}})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "discord_forbidden"

    monkeypatch.setattr(dash.ic, "update_settings", raiser(None))
    r = dash.put("/api/settings", json={"values": {"auto_kick": True}})
    assert r.status_code == 502 and r.json()["detail"]["error"] == "internal_api_unavailable"

    monkeypatch.setattr(dash.ic, "get_settings", raiser(500))
    r = dash.get("/api/settings")
    assert r.status_code == 502 and r.json()["detail"]["error"] == "internal_api_unavailable"


def test_mirror_matches_bot_schema():
    """server/routes/settings.py's _FIELD_RULES must mirror the bot's schema."""
    from server.routes.settings import _FIELD_RULES
    from services.settings_service import SETTINGS_SCHEMA

    assert set(_FIELD_RULES) == set(SETTINGS_SCHEMA)
    for key, field in SETTINGS_SCHEMA.items():
        rule = _FIELD_RULES[key]
        assert rule["type"] == field.type, key
        assert bool(rule.get("read_only")) == field.read_only, key
        if field.type == "int":
            assert (rule["min"], rule["max"]) == (field.min, field.max), key
        if field.pattern:
            assert rule["pattern"] == field.pattern, key
