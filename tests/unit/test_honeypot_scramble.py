"""Honeypot name scrambling (migration 029): name generation, the schedule, the
service method against fake channels, the internal route and the dashboard route."""
from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
import pytest_asyncio

import config
from internal_api.app import create_app
from services import honeypot_service as hs
from services.honeypot_service import HoneypotError
from tests.unit.test_honeypot_support import (
    CHANNEL_ID, GUILD_ID, configured, db, make_guild, make_service, make_text_channel, repo,  # noqa: F401
)

CATEGORY_ID = 1000000000000000009


def _forbidden() -> discord.Forbidden:
    return discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Permissions")


def trap(channel_name="🚫┊do-not-type", category_name="───⋅ Ignore ⋅───", *, others=0, in_category=True):
    channel = make_text_channel(CHANNEL_ID, channel_name)
    channel.edit = AsyncMock(return_value=None)
    category = None
    if in_category:
        category = MagicMock(spec=discord.CategoryChannel)
        category.id = CATEGORY_ID
        category.name = category_name
        category.edit = AsyncMock(return_value=None)
        extra = [make_text_channel(CHANNEL_ID + 100 + i, f"other{i}") for i in range(others)]
        category.channels = [channel, *extra]
    channel.category = category
    return channel, category


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_split_decoration_on_the_live_names():
    assert hs.split_decoration("🚫┊do-not-type") == ("🚫┊", "do-not-type", "")
    assert hs.split_decoration("───⋅ Ignore ⋅───") == ("───⋅ ", "Ignore", " ⋅───")
    assert hs.split_decoration("general") == ("", "general", "")
    assert hs.split_decoration("") == ("", "", "")


def test_scrambled_names_keep_decoration_and_change_words():
    rng = random.Random(7)
    for _ in range(200):
        ch = hs.scrambled_name("🚫┊do-not-type", "channel", rng)
        assert ch.startswith("🚫┊") and ch != "🚫┊do-not-type"
        assert re.fullmatch(r"[bcdfghjklmnpqrstvwxz2-9]{2,5}(-[bcdfghjklmnpqrstvwxz2-9]{2,5}){0,2}", ch[2:])
        cat = hs.scrambled_name("───⋅ Ignore ⋅───", "category", rng)
        assert cat.startswith("───⋅ ") and cat.endswith(" ⋅───")
        words = cat[5:-5].split(" ")
        assert 1 <= len(words) <= 2 and all(w[0].isupper() and 4 <= len(w) <= 6 for w in words)
        assert len(ch) <= 100 and len(cat) <= 100


def test_scramble_due():
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    row = {"scramble_enabled": True, "channel_id": 1, "scramble_interval_hours": 6, "last_scrambled_at": None}
    assert hs.scramble_due(row, now)  # never scrambled: due at once
    assert not hs.scramble_due({**row, "scramble_enabled": False}, now)
    assert not hs.scramble_due({**row, "channel_id": None}, now)
    assert not hs.scramble_due({**row, "last_scrambled_at": (now - timedelta(hours=5)).isoformat()}, now)
    assert hs.scramble_due({**row, "last_scrambled_at": (now - timedelta(hours=6)).isoformat()}, now)
    assert not hs.scramble_due(None, now)


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

async def _setup(repo, db, *, audit=None, **trap_kwargs):
    channel, category = trap(**trap_kwargs)
    guild = make_guild([channel])
    service, _bot = make_service(guild, repo, db, audit=audit)
    await configured(repo)
    return service, guild, channel, category


async def test_scramble_renames_channel_and_category(repo, db):
    audit = MagicMock(log=AsyncMock())
    service, guild, channel, category = await _setup(repo, db, audit=audit)
    res = await service.scramble_names(guild, actor_id=77, trigger="manual")
    new_channel = channel.edit.await_args.kwargs["name"]
    new_category = category.edit.await_args.kwargs["name"]
    assert new_channel.startswith("🚫┊") and new_category.startswith("───⋅ ")
    assert res["channel"] == {"old": "🚫┊do-not-type", "new": new_channel}
    assert res["category"] == {"old": "───⋅ Ignore ⋅───", "new": new_category}
    assert res["warnings"] == []
    assert channel.edit.await_args.kwargs["reason"].startswith("Honeypot")
    assert (await repo.get_config(GUILD_ID))["last_scrambled_at"] is not None
    args, kwargs = audit.log.await_args
    assert args[:3] == (GUILD_ID, 77, "honeypot_scramble") and kwargs["detail"]["trigger"] == "manual"


async def test_shared_category_is_left_alone(repo, db):
    service, guild, channel, category = await _setup(repo, db, others=2)
    res = await service.scramble_names(guild)
    assert channel.edit.await_count == 1 and category.edit.await_count == 0
    assert res["category"] is None and "also holds other channels" in res["warnings"][0]


async def test_channel_without_category(repo, db):
    service, guild, channel, _ = await _setup(repo, db, in_category=False)
    res = await service.scramble_names(guild)
    assert channel.edit.await_count == 1 and res["category"] is None
    assert "not in a category" in res["warnings"][0]


async def test_manual_cooldown(repo, db):
    service, guild, channel, _ = await _setup(repo, db)
    await service.scramble_names(guild)
    with pytest.raises(HoneypotError) as e:
        await service.scramble_names(guild)
    assert e.value.code == "too_soon" and e.value.status == 409 and e.value.detail == "too_soon"
    assert channel.edit.await_count == 1


async def test_channel_forbidden_changes_nothing(repo, db):
    service, guild, channel, category = await _setup(repo, db)
    channel.edit.side_effect = _forbidden()
    with pytest.raises(HoneypotError) as e:
        await service.scramble_names(guild)
    assert e.value.status == 403 and "Manage Channels" in e.value.detail
    assert category.edit.await_count == 0
    assert (await repo.get_config(GUILD_ID))["last_scrambled_at"] is None


async def test_category_failure_is_a_warning(repo, db):
    service, guild, channel, category = await _setup(repo, db)
    category.edit.side_effect = _forbidden()
    res = await service.scramble_names(guild)
    assert res["category"] is None and "category was not" in res["warnings"][0]
    assert (await repo.get_config(GUILD_ID))["last_scrambled_at"] is not None


async def test_rate_limited_rename_times_out(repo, db):
    service, guild, channel, _ = await _setup(repo, db)

    async def slow(**_):
        await asyncio.sleep(5)

    channel.edit = slow
    with pytest.raises(HoneypotError) as e:
        await service.scramble_names(guild, timeout=0.05)
    assert e.value.code == "rate_limited" and e.value.status == 409


async def test_missing_channel(repo, db):
    service, guild, _channel, _ = await _setup(repo, db)
    guild.get_channel = lambda cid: None
    with pytest.raises(HoneypotError) as e:
        await service.scramble_names(guild)
    assert e.value.code == "channel_missing"


async def test_scramble_if_due_schedule_and_retry(repo, db):
    service, guild, channel, _ = await _setup(repo, db)
    assert await service.scramble_if_due(guild) is False  # scrambling is off
    await repo.upsert_config(GUILD_ID, scramble_enabled=True, scramble_interval_hours=6)
    assert await service.scramble_if_due(guild) is True  # never scrambled: due
    assert await service.scramble_if_due(guild) is False  # not due again for 6 h
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    await repo.upsert_config(GUILD_ID, last_scrambled_at=old)
    channel.edit.side_effect = _forbidden()
    assert await service.scramble_if_due(guild) is False  # failed: logged, not raised
    calls = channel.edit.await_count
    assert await service.scramble_if_due(guild) is False  # inside the retry window: not even tried
    assert channel.edit.await_count == calls
    assert (await repo.get_config(GUILD_ID))["last_scrambled_at"] == old


async def test_update_config_and_get_config(repo, db):
    service, guild, _channel, _ = await _setup(repo, db)
    cfg = await service.update_config(guild, {"scramble_enabled": True, "scramble_interval_hours": 12})
    assert cfg["scramble_enabled"] is True and cfg["scramble_interval_hours"] == 12
    assert cfg["next_scramble_at"] is None  # never scrambled yet
    assert cfg["channel_name"] == "🚫┊do-not-type" and cfg["category_name"] == "───⋅ Ignore ⋅───"
    for bad in ({"scramble_interval_hours": 0}, {"scramble_interval_hours": 169},
                {"scramble_interval_hours": True}, {"scramble_enabled": "yes"}):
        with pytest.raises(HoneypotError):
            await service.update_config(guild, bad)
    await service.scramble_names(guild)
    cfg = await service.get_config(GUILD_ID)
    nxt = datetime.fromisoformat(cfg["next_scramble_at"])
    last = datetime.fromisoformat(cfg["last_scrambled_at"])
    assert nxt - last == timedelta(hours=12)


# --------------------------------------------------------------------------
# Internal API
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def api(repo, db):
    channel, _category = trap()
    guild = make_guild([channel])
    service, bot = make_service(guild, repo, db)
    cog = MagicMock()
    cog.honeypot_service = service
    bot.get_cog = lambda name: cog if name == "Honeypot" else None
    app = create_app()
    app.state.bot = bot
    headers = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=headers) as c:
        yield c


async def test_internal_scramble_route(api, repo):
    base = f"/guilds/{GUILD_ID}/honeypot"
    assert (await api.post(f"{base}/scramble", json={"actor_id": 5})).status_code == 400  # not configured
    await configured(repo)
    r = await api.post(f"{base}/scramble", json={"actor_id": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["channel"]["old"] == "🚫┊do-not-type" and body["config"]["channel_id"] == str(CHANNEL_ID)
    r = await api.post(f"{base}/scramble", json={"actor_id": 5})
    assert r.status_code == 409 and r.json() == {"detail": "too_soon"}
    r = await api.put(f"{base}/config", json={"scramble_enabled": True, "scramble_interval_hours": 3})
    assert r.status_code == 200 and r.json()["scramble_interval_hours"] == 3
    assert (await api.put(f"{base}/config", json={"scramble_interval_hours": 500})).status_code == 400
    assert (await api.put(f"{base}/config", json={"scramble_enabled": "x"})).status_code == 422
