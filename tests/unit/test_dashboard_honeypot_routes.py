"""Dashboard honeypot + public appeal routes, with the internal client mocked."""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from server import config
from server.app import app
from server.auth.dependencies import require_session
from server.auth.oauth import limiter
from server.db.models import Session
from server.services import internal_client
from server.services.internal_client import InternalAPIError

ORIGIN = config.FRONTEND_BASE_URL
ACTOR_ID = 424242424242424242


def _mk(now):
    s = Session()
    s.session_id = "s"
    s.discord_user_id = ACTOR_ID
    s.discord_username = "mod"
    s.is_guild_member = True
    return s


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def authed(monkeypatch):
    """Logged-in session + a controllable set of granted permissions."""
    granted: set[str] = set()

    async def fake_check(guild_id, user_id, permission):
        return permission in granted

    monkeypatch.setattr(internal_client, "check_permission", fake_check)
    app.dependency_overrides[require_session] = _fake_session_dep
    yield granted
    app.dependency_overrides.pop(require_session, None)


async def _fake_session_dep():
    return _mk(None)


# --------------------------------------------------------------------------
# Permission gating
# --------------------------------------------------------------------------

READ_ROUTES = [
    ("GET", "/api/honeypot/config", None),
    ("GET", "/api/honeypot/catches", None),
    ("GET", "/api/honeypot/stats", None),
    ("GET", "/api/honeypot/appeals", None),
]
WRITE_ROUTES = [
    ("PUT", "/api/honeypot/config", {"enabled": False}),
    ("POST", "/api/honeypot/setup", {}),
    ("POST", "/api/honeypot/scramble", None),
    ("POST", "/api/honeypot/appeals/1/approve", {}),
    ("POST", "/api/honeypot/appeals/1/deny", {}),
]


@pytest.mark.parametrize("method,path,body", READ_ROUTES + WRITE_ROUTES)
def test_unauthenticated_is_401(client, method, path, body):
    r = client.request(method, path, json=body, headers={"Origin": ORIGIN})
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "unauthenticated"


@pytest.mark.parametrize("method,path,body", READ_ROUTES + WRITE_ROUTES)
def test_no_permission_is_403(client, authed, method, path, body):
    r = client.request(method, path, json=body, headers={"Origin": ORIGIN})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "forbidden"


@pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
def test_moderate_only_cannot_write(client, authed, method, path, body):
    authed.add("moderate_members")
    r = client.request(method, path, json=body, headers={"Origin": ORIGIN})
    assert r.status_code == 403
    assert r.json()["detail"]["required_permission"] == "ban_members"


def test_moderate_members_can_read(client, authed, monkeypatch):
    authed.add("moderate_members")

    async def fake(guild_id, limit, before_id):
        assert (guild_id, limit, before_id) == (config.DASHBOARD_GUILD_ID, 25, 9)
        return {"catches": []}

    monkeypatch.setattr(internal_client, "get_honeypot_catches", fake)
    r = client.get("/api/honeypot/catches?limit=25&before_id=9")
    assert r.status_code == 200 and r.json() == {"catches": []}


# --------------------------------------------------------------------------
# Actor comes from the session, never the body
# --------------------------------------------------------------------------

def test_setup_actor_from_session(client, authed, monkeypatch):
    authed.add("ban_members")
    seen = {}

    async def fake(guild_id, actor_id, values):
        seen.update(actor_id=actor_id, values=values)
        return {"config": {}, "warnings": []}

    monkeypatch.setattr(internal_client, "setup_honeypot", fake)
    r = client.post(
        "/api/honeypot/setup",
        json={"channel_name": "trap", "actor_id": 1, "reviewer_id": 2},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 201
    assert seen["actor_id"] == ACTOR_ID
    assert "actor_id" not in seen["values"] and seen["values"]["channel_name"] == "trap"


def test_scramble_actor_from_session_and_error_mapping(client, authed, monkeypatch):
    authed.add("ban_members")
    seen = {}
    outcome: dict = {}

    async def fake(guild_id, actor_id):
        seen.update(guild_id=guild_id, actor_id=actor_id)
        if "error" in outcome:
            raise outcome["error"]
        return {"config": {}, "channel": {"old": "a", "new": "b"}, "category": None, "warnings": []}

    monkeypatch.setattr(internal_client, "scramble_honeypot_names", fake)
    r = client.post("/api/honeypot/scramble", headers={"Origin": ORIGIN})
    assert r.status_code == 200 and r.json()["channel"]["new"] == "b"
    assert seen == {"guild_id": config.DASHBOARD_GUILD_ID, "actor_id": ACTOR_ID}
    for status, detail, expected in ((409, "too_soon", (409, "too_soon")), (409, "rate_limited", (409, "rate_limited")),
                                     (403, "x", (502, "discord_forbidden")), (502, None, (502, "internal_api_unavailable"))):
        outcome["error"] = InternalAPIError("x", status_code=status, detail=detail)
        r = client.post("/api/honeypot/scramble", headers={"Origin": ORIGIN})
        assert (r.status_code, r.json()["detail"]["error"]) == expected


def test_scramble_interval_bounds(client, authed, monkeypatch):
    authed.add("ban_members")

    async def fake(guild_id, values):
        return values

    monkeypatch.setattr(internal_client, "update_honeypot_config", fake)
    r = client.put("/api/honeypot/config", json={"scramble_enabled": True, "scramble_interval_hours": 6},
                   headers={"Origin": ORIGIN})
    assert r.status_code == 200 and r.json() == {"scramble_enabled": True, "scramble_interval_hours": 6}
    for bad in (0, 169):
        r = client.put("/api/honeypot/config", json={"scramble_interval_hours": bad}, headers={"Origin": ORIGIN})
        assert r.status_code == 422


def test_setup_conflict_maps_to_409(client, authed, monkeypatch):
    authed.add("ban_members")

    async def fake(*a, **k):
        raise InternalAPIError("x", status_code=409, detail="already_configured")

    monkeypatch.setattr(internal_client, "setup_honeypot", fake)
    r = client.post("/api/honeypot/setup", json={}, headers={"Origin": ORIGIN})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "already_configured"


@pytest.mark.parametrize("which", ["approve", "deny"])
def test_review_reviewer_from_session(client, authed, monkeypatch, which):
    authed.add("ban_members")
    seen = {}

    async def fake(guild_id, appeal_id, reviewer_id, note):
        seen.update(appeal_id=appeal_id, reviewer_id=reviewer_id, note=note)
        return {"ok": True}

    monkeypatch.setattr(internal_client, f"{which}_honeypot_appeal", fake)
    r = client.post(
        f"/api/honeypot/appeals/7/{which}",
        json={"note": "hi", "reviewer_id": 5},
        headers={"Origin": ORIGIN},
    )
    assert r.status_code == 200
    assert seen == {"appeal_id": 7, "reviewer_id": ACTOR_ID, "note": "hi"}


def test_internal_down_is_502(client, authed, monkeypatch):
    authed.add("moderate_members")

    async def fake(guild_id):
        raise InternalAPIError("down")

    monkeypatch.setattr(internal_client, "get_honeypot_stats", fake)
    r = client.get("/api/honeypot/stats")
    assert r.status_code == 502 and r.json()["detail"]["error"] == "internal_api_unavailable"


def test_update_config_partial_and_validation(client, authed, monkeypatch):
    authed.add("ban_members")
    seen = {}

    async def fake(guild_id, values):
        seen.update(values)
        return {}

    monkeypatch.setattr(internal_client, "update_honeypot_config", fake)
    r = client.put("/api/honeypot/config", json={"action": "kick", "log_channel_id": "123456789012345678"},
                   headers={"Origin": ORIGIN})
    assert r.status_code == 200
    assert seen == {"action": "kick", "log_channel_id": 123456789012345678}
    assert client.put("/api/honeypot/config", json={"delete_message_days": 9},
                      headers={"Origin": ORIGIN}).status_code == 422
    assert client.put("/api/honeypot/config", json={"action": "nuke"},
                      headers={"Origin": ORIGIN}).status_code == 422


# --------------------------------------------------------------------------
# Public appeal routes: no session at all
# --------------------------------------------------------------------------

TOKEN = "abcDEF123_-abcDEF123_-abcDEF"


def _public_headers(ip="203.0.113.1"):
    return {"Origin": ORIGIN, "CF-Connecting-IP": ip}


def test_public_lookup_no_session(client, monkeypatch):
    async def fake(guild_id, token):
        assert token == TOKEN
        return {"user_id": 123456789012345678, "username": "bob", "status": "invited", "secret": "leak"}

    monkeypatch.setattr(internal_client, "lookup_appeal_token", fake)
    r = client.get(f"/api/public/appeals/{TOKEN}", headers=_public_headers())
    assert r.status_code == 200
    assert r.json() == {
        "user_id": "123456789012345678", "username": "bob", "status": "invited",
        "submitted_at": None, "reviewed_at": None, "rejoin_invite_url": None, "rejoined": False,
    }


def test_public_lookup_invalid_token_generic(client, monkeypatch):
    async def fake(guild_id, token):
        raise InternalAPIError("nope", status_code=404, detail="invalid_token")

    monkeypatch.setattr(internal_client, "lookup_appeal_token", fake)
    r = client.get(f"/api/public/appeals/{TOKEN}", headers=_public_headers())
    assert r.status_code == 404 and r.json() == {"error": "not_eligible"}
    # malformed token never reaches the bot
    r = client.get("/api/public/appeals/short", headers=_public_headers())
    assert r.status_code == 404 and r.json() == {"error": "not_eligible"}


def test_public_submit_requires_discord_sign_in(client, monkeypatch):
    """Appeals need Discord sign-in (2026-10-29): POST /api/public/appeals refuses EVERY
    body identically (typed id, DM token, garbage) and never calls the bot."""
    assert not hasattr(internal_client, "submit_appeal")  # the unverified client path is gone
    h = _public_headers("203.0.113.2")
    bodies = [
        {"user_id": "123456789012345678", "name": "Bob", "message": "hi"},
        {"token": TOKEN, "name": "Bob"},
        {"name": "Bob"},
        {},
    ]
    answers = [client.post("/api/public/appeals", json=b, headers=h) for b in bodies]
    answers.append(client.post("/api/public/appeals", content=b"not json", headers=h))
    for r in answers:
        assert r.status_code == 403
        assert r.content == answers[0].content
    assert answers[0].json() == {"error": "signin_required"}


def test_public_submit_rate_limited(client):
    h = _public_headers("203.0.113.9")
    body = {"token": TOKEN, "name": "Bob"}
    codes = [client.post("/api/public/appeals", json=body, headers=h).status_code for _ in range(6)]
    assert codes == [403] * 5 + [429]
    assert client.post("/api/public/appeals", json=body, headers=h).json() == {"error": "rate_limited"}
    # A different client IP has its own bucket.
    assert client.post("/api/public/appeals", json=body, headers=_public_headers("203.0.113.10")).status_code == 403


def test_public_lookup_rate_limited(client, monkeypatch):
    async def fake(guild_id, token):
        return {"user_id": 1, "username": "b", "status": "invited"}

    monkeypatch.setattr(internal_client, "lookup_appeal_token", fake)
    h = _public_headers("203.0.113.20")
    codes = [client.get(f"/api/public/appeals/{TOKEN}", headers=h).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30 and codes[30] == 429


def test_public_csrf_origin_check(client):
    body = {"token": TOKEN, "name": "Bob"}
    r = client.post("/api/public/appeals", json=body,
                    headers={"Origin": "https://evil.example", "CF-Connecting-IP": "203.0.113.30"})
    assert r.status_code == 403 and r.json() != {"error": "signin_required"}  # blocked by the Origin check first
    for origin in (ORIGIN, f"http://localhost:{config.DASHBOARD_PORT}"):
        r = client.post("/api/public/appeals", json=body,
                        headers={"Origin": origin, "CF-Connecting-IP": "203.0.113.31"})
        assert r.json() == {"error": "signin_required"}, origin
        limiter.reset()


def test_spa_routes_serve_index(client):
    # Public pages fall back to index.html (only when a build exists).
    r = client.get("/appeal/" + TOKEN)
    if r.status_code == 200:
        assert "text/html" in r.headers["content-type"]
