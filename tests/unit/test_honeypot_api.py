"""Internal API routes against create_app() with a fake bot and an in-memory DB."""
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

import config
from internal_api.app import create_app
from services.honeypot_service import HoneypotService
from tests.unit.test_honeypot_support import (
    CHANNEL_ID, GUILD_ID, LOG_CHANNEL_ID, configured, db, make_bot, make_guild, make_text_channel, repo,
)
from database.repositories.mod_action_repo import ModActionRepository
from services.moderation_service import ModerationService

pytestmark = pytest.mark.asyncio

AUTH = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
BASE = f"/guilds/{GUILD_ID}/honeypot"


@pytest_asyncio.fixture
async def client(repo, db):
    guild = make_guild([make_text_channel(CHANNEL_ID, "trap"), make_text_channel(LOG_CHANNEL_ID, "log")])
    bot = make_bot(guild)
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(return_value=MagicMock(send=AsyncMock()))
    service = HoneypotService(bot, repo, ModerationService(ModActionRepository(db)))
    cog = MagicMock()
    cog.honeypot_service = service
    bot.get_cog = lambda name: cog if name == "Honeypot" else None
    app = create_app()
    app.state.bot = bot
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AUTH) as c:
        c.guild, c.repo, c.service = guild, repo, service
        yield c


async def test_requires_secret(client):
    r = await client.get(f"{BASE}/config", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403
    r = await client.get(f"{BASE}/config", headers={"Authorization": ""})
    assert r.status_code == 401


async def test_config_unconfigured_then_configured_with_string_snowflakes(client):
    body = (await client.get(f"{BASE}/config")).json()
    assert body["configured"] is False and body["channel_id"] is None and body["action"] == "ban"
    assert body["appeal_base_url"].startswith("http")
    await configured(client.repo, log_channel_id=LOG_CHANNEL_ID, stats_message_id=778899)
    body = (await client.get(f"{BASE}/config")).json()
    assert body["configured"] and body["channel_exists"] is True
    assert body["channel_id"] == str(CHANNEL_ID) and body["log_channel_id"] == str(LOG_CHANNEL_ID)
    assert body["category_id"] == "1000000000000000009" and body["stats_message_id"] == "778899"


async def test_put_config_partial_and_validation(client):
    await configured(client.repo)
    r = await client.put(f"{BASE}/config", json={"action": "kick", "delete_message_days": 4})
    assert r.status_code == 200 and r.json()["action"] == "kick" and r.json()["delete_message_days"] == 4
    r = await client.put(f"{BASE}/config", json={"log_channel_id": LOG_CHANNEL_ID})
    assert r.json()["log_channel_id"] == str(LOG_CHANNEL_ID)
    r = await client.put(f"{BASE}/config", json={"log_channel_id": None})
    assert r.json()["log_channel_id"] is None and r.json()["action"] == "kick"  # untouched fields stay
    for bad in ({"action": "nuke"}, {"delete_message_days": 8}, {"log_channel_id": 5}):
        assert (await client.put(f"{BASE}/config", json=bad)).status_code == 400
    assert (await client.put(f"{BASE}/config", json={"enabled": "maybe"})).status_code == 422


async def test_put_config_before_setup_is_400(client):
    r = await client.put(f"{BASE}/config", json={"enabled": True})
    assert r.status_code == 400


async def test_setup_conflict_when_channel_exists(client):
    await configured(client.repo)
    r = await client.post(f"{BASE}/setup", json={"actor_id": 1})
    assert r.status_code == 409 and r.json() == {"detail": "already_configured"}


async def test_setup_missing_permission_is_502_with_clear_detail(client):
    client.guild.me.guild_permissions.manage_channels = False
    r = await client.post(f"{BASE}/setup", json={"actor_id": 1})
    assert r.status_code == 502 and "Manage Channels" in r.json()["detail"]


async def test_catches_and_stats(client):
    ids = [await client.repo.add_event(GUILD_ID, 2000000000000000000 + i, "ban", username=f"u{i}", errors=["e"]) for i in range(3)]
    body = (await client.get(f"{BASE}/catches", params={"limit": 2})).json()
    assert [c["id"] for c in body["catches"]] == ids[::-1][:2]
    c = body["catches"][0]
    assert c["user_id"] == "2000000000000000002" and isinstance(c["id"], int)
    assert c["errors"] == ["e"] and set(c["flags"]) == {"link", "invite", "everyone", "attachment"}
    assert c["appeal_status"] is None
    older = (await client.get(f"{BASE}/catches", params={"before_id": ids[1]})).json()
    assert [x["id"] for x in older["catches"]] == [ids[0]]
    assert (await client.get(f"{BASE}/catches", params={"limit": 0})).status_code == 422

    stats = (await client.get(f"{BASE}/stats")).json()
    assert stats["totals"]["all"] == 3 and len(stats["timeline_30d"]) == 30 and len(stats["by_hour_utc"]) == 24


async def test_stats_empty(client):
    stats = (await client.get(f"{BASE}/stats")).json()
    assert stats["totals"]["all"] == 0 and stats["first_catch_at"] is None


async def test_public_appeal_flow_over_http(client):
    eid = await client.repo.add_event(GUILD_ID, 5000000000000000001, "ban", username="eve")
    await client.repo.create_appeal(GUILD_ID, 5000000000000000001, "tok", username="eve", catch_id=eid)

    r = await client.get(f"{BASE}/appeals/lookup/tok")
    assert r.json() == {
        "user_id": "5000000000000000001", "username": "eve", "status": "invited",
        "submitted_at": None, "reviewed_at": None, "rejoin_invite_url": None, "rejoined": False,
    }
    r = await client.get(f"{BASE}/appeals/lookup/bad")
    assert r.status_code == 404 and r.json() == {"detail": "invalid_token"}

    # The unverified submit route is gone (appeals need Discord sign-in, 2026-10-29).
    r = await client.post(f"{BASE}/appeals/submit", json={"token": "tok", "name": "Eve", "message": "hi"})
    assert r.status_code in (404, 405)
    body = {"user_id": 5000000000000000001, "username": "eve", "name": "Eve", "message": "hi"}
    r = await client.post(f"{BASE}/appeals/submit-verified", json=body)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    r = await client.post(f"{BASE}/appeals/submit-verified", json=body)
    assert r.status_code == 409

    listed = (await client.get(f"{BASE}/appeals", params={"status": "pending"})).json()["appeals"]
    assert len(listed) == 1 and listed[0]["user_id"] == "5000000000000000001" and "token" not in listed[0]
    assert (await client.get(f"{BASE}/appeals", params={"status": "zzz"})).status_code == 400

    aid = listed[0]["id"]
    r = await client.post(f"{BASE}/appeals/{aid}/approve", json={"reviewer_id": 9, "note": "ok"})
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["unbanned"] is True
    client.guild.unban.assert_awaited_once()
    r = await client.post(f"{BASE}/appeals/{aid}/approve", json={"reviewer_id": 9})
    assert r.status_code == 409
    r = await client.post(f"{BASE}/appeals/{aid}/deny", json={"reviewer_id": 9})
    assert r.status_code == 409
    r = await client.post(f"{BASE}/appeals/99999/deny", json={"reviewer_id": 9})
    assert r.status_code == 404
    appeals = (await client.get(f"{BASE}/appeals")).json()["appeals"]
    assert appeals[0]["status"] == "approved" and appeals[0]["reviewed_by"] == "9"


async def test_deny_over_http(client):
    aid = await client.repo.create_appeal(GUILD_ID, 7, "t7", status="pending", name="n", message="m")
    r = await client.post(f"{BASE}/appeals/{aid}/deny", json={"reviewer_id": 1, "note": "no"})
    assert r.status_code == 200 and r.json() == {"ok": True, "dm_sent": True}


async def test_503_when_cog_not_loaded(client):
    client._transport.app.state.bot.get_cog = lambda name: None
    assert (await client.get(f"{BASE}/config")).status_code == 503
