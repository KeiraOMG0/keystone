"""Internal API moderation routes (actor resolution, rank checks, error
contract, bounds) and the dashboard proxy (session actor, pass-through,
validation). Fake discord objects, mocked services/client. No network."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
import pytest_asyncio

import config
from internal_api.app import create_app
from internal_api.routes import moderation as routes

GUILD_ID = 100000000000000001
BOT_ID = 900000000000000001
OWNER = 424242424242424242
MOD = 555000000000000005
TARGET = 555000000000000006
HIGH = 555000000000000007
OUTSIDER = 555000000000000009
AUTH = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
BASE = f"/guilds/{GUILD_ID}/moderation"


def member(uid, pos):
    m = SimpleNamespace(id=uid, top_role=SimpleNamespace(position=pos), name=f"u{uid}", display_name=f"U{uid}",
                        roles=[], display_avatar=SimpleNamespace(url="https://cdn/x.png"))
    m.kick = AsyncMock()
    m.timeout = AsyncMock()
    return m


def make_guild():
    members = {MOD: member(MOD, 5), TARGET: member(TARGET, 2), HIGH: member(HIGH, 8)}
    g = SimpleNamespace(id=GUILD_ID, owner_id=None, me=member(BOT_ID, 10), members=list(members.values()))
    g.get_member = lambda uid: members.get(uid)
    g.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Member"))
    g.ban = AsyncMock()
    g.unban = AsyncMock()
    g.get_channel = lambda cid: None
    g.m = members
    return g


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(config, "OWNER_ID", OWNER)


@pytest_asyncio.fixture
async def api(monkeypatch):
    from services.moderation_service import ModerationService

    guild = make_guild()
    bot = MagicMock()
    bot.get_guild = lambda gid: guild if gid == GUILD_ID else None
    mod_repo = SimpleNamespace(add_action=AsyncMock(return_value=7), get_actions=AsyncMock(return_value=[]))
    audit = SimpleNamespace(log=AsyncMock())
    svc = ModerationService(mod_repo, audit)
    warnings = SimpleNamespace(
        add_warning=AsyncMock(return_value=3), remove_warning=AsyncMock(return_value={"id": 3, "user_id": TARGET}),
        list_recent=AsyncMock(return_value=[]), get_warnings=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(routes, "_get_moderation_service", lambda b: svc)
    monkeypatch.setattr(routes, "_get_warning_service", lambda b: warnings)
    app = create_app()
    app.state.bot = bot
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AUTH) as c:
        c.guild, c.svc, c.warnings, c.audit = guild, svc, warnings, audit
        yield c


async def test_kick_allowed(api):
    r = await api.post(f"{BASE}/kick/{TARGET}", json={"moderator_id": MOD, "reason": "spam"})
    assert r.status_code == 201
    api.guild.m[TARGET].kick.assert_awaited_once()


async def test_kick_higher_member_is_403_hierarchy(api):
    r = await api.post(f"{BASE}/kick/{HIGH}", json={"moderator_id": MOD})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "hierarchy_blocked" and "above yours" in r.json()["detail"]["message"]
    api.guild.m[HIGH].kick.assert_not_awaited()


async def test_non_member_actor_is_403(api):
    r = await api.post(f"{BASE}/timeout/{TARGET}", json={"moderator_id": OUTSIDER, "duration_minutes": 5})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "actor_not_member"


async def test_owner_outside_guild_may_act(api):
    r = await api.post(f"{BASE}/timeout/{HIGH}", json={"moderator_id": OWNER, "duration_minutes": 5})
    assert r.status_code == 201
    api.guild.m[HIGH].timeout.assert_awaited_once()


async def test_missing_target_is_404_dict(api):
    r = await api.post(f"{BASE}/untimeout/{OUTSIDER}", json={"moderator_id": MOD})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "member_not_found"


async def test_ban_non_member_identity_only(api):
    r = await api.post(f"{BASE}/ban", json={"user_id": OUTSIDER, "moderator_id": MOD, "delete_message_days": 7})
    assert r.status_code == 201
    assert api.guild.ban.await_args.kwargs["delete_message_days"] == 7
    r = await api.post(f"{BASE}/ban", json={"user_id": HIGH, "moderator_id": MOD})
    assert r.status_code == 403


async def test_discord_errors_are_4xx(api):
    api.guild.m[TARGET].kick = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "Missing Permissions"))
    r = await api.post(f"{BASE}/kick/{TARGET}", json={"moderator_id": MOD})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "discord_forbidden"
    api.guild.m[TARGET].kick = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=400, reason="x"), "bad"))
    r = await api.post(f"{BASE}/kick/{TARGET}", json={"moderator_id": MOD})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "discord_rejected"
    api.guild.m[TARGET].kick = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500, reason="x"), "oops"))
    r = await api.post(f"{BASE}/kick/{TARGET}", json={"moderator_id": MOD})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "discord_unavailable"
    api.guild.unban = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Ban"))
    r = await api.post(f"{BASE}/unban", json={"user_id": OUTSIDER, "moderator_id": MOD})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_banned"


@pytest.mark.parametrize("path,body", [
    (f"/timeout/{TARGET}", {"moderator_id": MOD, "duration_minutes": 40321}),
    (f"/timeout/{TARGET}", {"moderator_id": MOD, "duration_minutes": 0}),
    (f"/kick/{TARGET}", {"moderator_id": MOD, "reason": "x" * 513}),
    ("/ban", {"user_id": TARGET, "moderator_id": MOD, "delete_message_days": 8}),
    ("/warnings", {"user_id": TARGET, "moderator_id": MOD, "reason": "x" * 513}),
])
async def test_bounds_are_422(api, path, body):
    r = await api.post(BASE + path, json=body)
    assert r.status_code == 422


async def test_recent_limit_bounds(api):
    assert (await api.get(f"{BASE}/warnings/recent?limit=201")).status_code == 422
    assert (await api.get(f"{BASE}/warnings/recent?limit=0")).status_code == 422
    assert (await api.get(f"{BASE}/warnings/recent?limit=200")).status_code == 200


async def test_add_warning_rank_checked_and_recorded(api):
    r = await api.post(f"{BASE}/warnings", json={"user_id": HIGH, "moderator_id": MOD})
    assert r.status_code == 403
    api.warnings.add_warning.assert_not_awaited()
    r = await api.post(f"{BASE}/warnings", json={"user_id": TARGET, "moderator_id": MOD, "reason": "r"})
    assert r.status_code == 201 and r.json() == {"warning_id": 3}
    # mod_actions companion row like !warn
    assert api.svc._repo.add_action.await_args.args[3] == "warn"


async def test_remove_warning_records_actor(api):
    r = await api.delete(f"{BASE}/warnings/3", params={"moderator_id": MOD, "user_id": TARGET})
    assert r.status_code == 200
    api.warnings.remove_warning.assert_awaited_once_with(GUILD_ID, 3, user_id=TARGET, actor_id=MOD)
    assert api.svc._repo.add_action.await_args.args[2:4] == (MOD, "warning_removed")
    api.warnings.remove_warning = AsyncMock(return_value=None)
    r = await api.delete(f"{BASE}/warnings/3", params={"moderator_id": MOD})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "warning_not_found"
    # moderator_id is required
    assert (await api.delete(f"{BASE}/warnings/3")).status_code == 422


# ---------------------------------------------------------------------------
# Dashboard proxy (server/routes/moderation.py)
# ---------------------------------------------------------------------------

SESSION_USER = 424242424242424243


@pytest.fixture
def dash(monkeypatch):
    from fastapi.testclient import TestClient

    from server import config as sconfig
    from server.app import app
    from server.auth.dependencies import require_session
    from server.db.models import Session
    from server.services import internal_client

    async def allowed(guild_id, user_id, permission):
        return True

    async def fake_session():
        s = Session()
        s.session_id = "s"
        s.discord_user_id = SESSION_USER
        s.discord_username = "keira"
        s.is_guild_member = True
        return s

    monkeypatch.setattr(internal_client, "check_permission", allowed)
    app.dependency_overrides[require_session] = fake_session
    client = TestClient(app, headers={"Origin": sconfig.FRONTEND_BASE_URL})
    client.ic = internal_client
    yield client
    app.dependency_overrides.pop(require_session, None)


def test_dash_actor_comes_from_session(dash, monkeypatch):
    seen = {}

    async def fake_kick(guild_id, user_id, moderator_id, reason):
        seen.update(user_id=user_id, moderator_id=moderator_id)
        return {"action_id": 1}

    monkeypatch.setattr(dash.ic, "kick_member", fake_kick)
    r = dash.post("/api/moderation/kick", json={"user_id": str(TARGET), "moderator_id": 1, "reason": "x"})
    assert r.status_code == 201 and seen == {"user_id": TARGET, "moderator_id": SESSION_USER}


def test_dash_remove_warning_passes_session_and_user(dash, monkeypatch):
    seen = {}

    async def fake_remove(guild_id, warning_id, moderator_id, user_id=None):
        seen.update(warning_id=warning_id, moderator_id=moderator_id, user_id=user_id)

    monkeypatch.setattr(dash.ic, "remove_warning", fake_remove)
    r = dash.delete(f"/api/moderation/warnings/4?user_id={TARGET}")
    assert r.status_code == 200 and seen == {"warning_id": 4, "moderator_id": SESSION_USER, "user_id": TARGET}


def test_dash_passes_bot_refusals_through(dash, monkeypatch):
    from server.services.internal_client import InternalAPIError

    def raiser(code, detail):
        async def f(*a, **k):
            raise InternalAPIError("x", status_code=code, detail=detail)
        return f

    monkeypatch.setattr(dash.ic, "timeout_member", raiser(403, {"error": "hierarchy_blocked", "message": "nope"}))
    r = dash.post("/api/moderation/timeout", json={"user_id": TARGET, "duration_minutes": 5})
    assert r.status_code == 403 and r.json()["detail"] == {"error": "hierarchy_blocked", "message": "nope"}
    monkeypatch.setattr(dash.ic, "ban_user", raiser(409, {"error": "discord_forbidden", "message": "m"}))
    r = dash.post("/api/moderation/ban", json={"user_id": TARGET})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "discord_forbidden"
    monkeypatch.setattr(dash.ic, "unban_user", raiser(404, {"error": "not_banned", "message": "m"}))
    assert dash.post("/api/moderation/unban", json={"user_id": TARGET}).status_code == 404
    # A string-detail 404 (guild not cached) or no response at all = bot unavailable.
    monkeypatch.setattr(dash.ic, "kick_member", raiser(404, "Guild not found"))
    r = dash.post("/api/moderation/kick", json={"user_id": TARGET})
    assert r.status_code == 502 and r.json()["detail"]["error"] == "internal_api_unavailable"
    monkeypatch.setattr(dash.ic, "kick_member", raiser(None, None))
    assert dash.post("/api/moderation/kick", json={"user_id": TARGET}).status_code == 502


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/moderation/timeout", {"user_id": TARGET, "duration_minutes": 40321}),
    ("post", "/api/moderation/timeout", {"user_id": TARGET, "duration_minutes": 0}),
    ("post", "/api/moderation/ban", {"user_id": TARGET, "delete_message_days": 8}),
    ("post", "/api/moderation/ban", {"user_id": TARGET, "delete_message_days": -1}),
    ("post", "/api/moderation/kick", {"user_id": TARGET, "reason": "x" * 513}),
    ("post", "/api/moderation/warnings", {"user_id": TARGET, "reason": "x" * 513}),
    ("post", "/api/moderation/kick", {"user_id": -5}),
    ("get", "/api/moderation/warnings/recent?limit=201", None),
    ("get", "/api/moderation/warnings/recent?limit=0", None),
    ("get", "/api/moderation/members/search?q=ab&limit=26", None),
])
def test_dash_bounds_are_422(dash, method, path, body):
    r = dash.post(path, json=body) if method == "post" else dash.get(path)
    assert r.status_code == 422
