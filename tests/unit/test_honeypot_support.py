"""Shared fixtures/fakes for the test_honeypot_* modules (no tests in here)."""
from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest_asyncio

from database.connection import Database
from database.repositories.honeypot_repo import HoneypotRepository
from database.repositories.mod_action_repo import ModActionRepository
from services.honeypot_service import HoneypotService
from services.moderation_service import ModerationService

SQL_DIR = pathlib.Path(__file__).resolve().parents[2] / "database" / "migrations" / "sql"
GUILD_ID = 100000000000000001
CHANNEL_ID = 1000000000000000001
LOG_CHANNEL_ID = 1000000000000000002
BOT_ID = 999


async def make_db() -> Database:
    db = Database(":memory:")
    await db.connect()
    for name in ("022_mod_actions.sql", "025_honeypot.sql", "026_honeypot_appeal_verified.sql", "028_honeypot_via_app.sql",
                 "029_honeypot_scramble.sql", "030_honeypot_appeal_outcomes.sql",
                 "031_honeypot_test_cases.sql"):
        await db.executescript((SQL_DIR / name).read_text(encoding="utf-8"))
    return db


@pytest_asyncio.fixture
async def db():
    database = await make_db()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def repo(db):
    return HoneypotRepository(db)


def make_text_channel(cid: int, name: str = "chan") -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = cid
    ch.name = name
    ch.send = AsyncMock()
    return ch


def make_guild(channels=()) -> MagicMock:
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.name = "Test Guild"
    by_id = {c.id: c for c in channels}
    guild.get_channel = lambda cid: by_id.get(cid)
    guild.ban = AsyncMock()
    guild.unban = AsyncMock()
    # Default: whoever is asked about is currently banned by the honeypot.
    guild.fetch_ban = AsyncMock(return_value=MagicMock(reason="Honeypot: posted in #do-not-type"))
    guild.get_member = MagicMock(return_value=None)
    guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Member"))
    return guild


def make_bot(guild) -> MagicMock:
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = BOT_ID
    bot.permission_service.is_staff = MagicMock(return_value=False)
    bot.get_guild = lambda gid: guild if gid == guild.id else None
    return bot


def make_member(uid: int = 42, *, admin=False, manage_messages=False, ban_members=False, bot=False) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.bot = bot
    m.name = f"user{uid}"
    m.display_name = f"User {uid}"
    m.roles = []
    perms = MagicMock()
    perms.administrator = admin
    perms.manage_messages = manage_messages
    perms.ban_members = ban_members
    m.guild_permissions = perms
    m.avatar = None
    m.display_avatar = MagicMock()
    m.display_avatar.url = "https://cdn.example/a.png"
    m.joined_at = discord.utils.utcnow()
    m.created_at = discord.utils.utcnow()
    m.send = AsyncMock()
    m.kick = AsyncMock()
    return m


def make_message(member, channel, content="free nitro http://x.y", guild=None) -> MagicMock:
    msg = MagicMock()
    msg.guild = guild
    msg.author = member
    msg.channel = channel
    msg.content = content
    msg.webhook_id = None
    msg.interaction_metadata = None
    msg.application_id = None
    msg.embeds = []
    msg.attachments = []
    msg.mention_everyone = False
    msg.id = 555
    msg.delete = AsyncMock()
    return msg


def make_service(guild, repo, db, *, moderation=None, audit=None):
    bot = make_bot(guild)
    mod = moderation or ModerationService(ModActionRepository(db))
    return HoneypotService(bot, repo, mod, audit=audit), bot


async def configured(repo, *, action="ban", **over):
    fields = dict(enabled=True, channel_id=CHANNEL_ID, category_id=1000000000000000009,
                  action=action, dm_before=True, appeal_enabled=True, delete_message_days=1)
    fields.update(over)
    return await repo.upsert_config(GUILD_ID, **fields)
