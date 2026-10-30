"""Moderation message log: services/message_log_service.py (separate SQLite
file in tmp_path, buffered writes, edits/deletes, prune, queries, export
text) and cogs/message_log.py (what is and isn't stored, ticket channels,
!msglog staff-only DM export). Fake discord objects, no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from services import message_log_service as mls
from services.message_log_service import MessageLogStore, format_rows, row_from_message

GUILD = SimpleNamespace(id=100000000000000001)
BOT_ID = 400000000000000004


def chan(cid=10, name="general"):
    return SimpleNamespace(id=cid, name=name)


def msg(mid, author_id=5, *, channel=None, content="hello", bot=False, webhook_id=None, created=None,
        meta=None, application_id=None, embeds=(), attachments=(), mtype=discord.MessageType.default):
    return SimpleNamespace(
        id=mid, guild=GUILD, channel=channel or chan(), content=content, embeds=list(embeds),
        attachments=list(attachments), webhook_id=webhook_id, interaction_metadata=meta,
        application_id=application_id, type=mtype,
        author=SimpleNamespace(id=author_id, name=f"user{author_id}", bot=bot),
        created_at=created or datetime.now(timezone.utc),
    )


def app_meta(invoker_id, user_installed=True):
    return SimpleNamespace(user=SimpleNamespace(id=invoker_id), is_user_integration=lambda: user_installed)


@pytest.fixture
async def store(tmp_path):
    s = MessageLogStore(str(tmp_path / "message_log.db"), retention_days=7)
    await s.open()
    yield s
    await s.close()


def since(hours=1):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def test_row_kinds():
    assert row_from_message(msg(1))["author_kind"] == "member"
    assert row_from_message(msg(2, bot=True))["author_kind"] == "bot"
    assert row_from_message(msg(3, bot=True, webhook_id=99))["author_kind"] == "webhook"
    e = SimpleNamespace(title="FREE NITRO", description="discord.gg/x", fields=[])
    row = row_from_message(msg(4, 777, bot=True, webhook_id=777, meta=app_meta(5), application_id=777,
                               content="", embeds=[e]))
    assert row["author_kind"] == "app" and row["invoker_id"] == 5 and row["app_id"] == 777
    assert row["app_name"] == "user777" and row["content"] == "FREE NITRO\ndiscord.gg/x"
    att = SimpleNamespace(filename="a.png", url="https://cdn/a.png")
    assert '"a.png"' in row_from_message(msg(5, attachments=[att]))["attachments"]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

async def test_buffered_until_flush_then_queryable(store):
    store.record(row_from_message(msg(1)))
    assert store.pending() == 1
    async with store._conn.execute("SELECT COUNT(*) FROM messages") as cur:
        assert (await cur.fetchone())[0] == 0
    rows = await store.query(GUILD.id, since=since())  # query flushes first
    assert [r["message_id"] for r in rows] == [1] and store.pending() == 0


async def test_duplicate_insert_ignored(store):
    store.record(row_from_message(msg(1, content="a")))
    store.record(row_from_message(msg(1, content="b")))
    rows = await store.query(GUILD.id, since=since())
    assert len(rows) == 1 and rows[0]["content"] == "a"


async def test_edit_only_when_text_changes(store):
    store.record(row_from_message(msg(1, content="hi")))
    store.edited(1, "hi")  # link-preview style edit: same text
    (row,) = await store.query(GUILD.id, since=since())
    assert row["edited_at"] is None
    store.edited(1, "bye")
    (row,) = await store.query(GUILD.id, since=since())
    assert row["edited_content"] == "bye" and row["edited_at"]


async def test_deletes_marked_including_bulk_and_order_with_insert(store):
    store.record(row_from_message(msg(1)))
    store.record(row_from_message(msg(2)))
    store.record(row_from_message(msg(3)))
    store.deleted([1])
    store.deleted([2, 3, 999])  # unknown id: no-op
    rows = await store.query(GUILD.id, since=since(), deleted_only=True)
    assert [r["message_id"] for r in rows] == [1, 2, 3] and all(r["deleted_at"] for r in rows)


async def test_user_query_matches_author_or_app_invoker(store):
    store.record(row_from_message(msg(1, 5)))
    store.record(row_from_message(msg(2, 6)))
    store.record(row_from_message(msg(3, 777, bot=True, webhook_id=777, meta=app_meta(5), application_id=777)))
    rows = await store.query(GUILD.id, since=since(), user_id=5)
    assert [r["message_id"] for r in rows] == [1, 3]


async def test_channel_and_window_filters(store):
    now = datetime.now(timezone.utc)
    store.record(row_from_message(msg(1, channel=chan(10), created=now - timedelta(hours=3))))
    store.record(row_from_message(msg(2, channel=chan(10))))
    store.record(row_from_message(msg(3, channel=chan(11))))
    rows = await store.query(GUILD.id, since=since(1), channel_id=10)
    assert [r["message_id"] for r in rows] == [2]


async def test_limit_keeps_newest_oldest_first(store):
    now = datetime.now(timezone.utc)
    for i in range(5):
        store.record(row_from_message(msg(i, created=now - timedelta(minutes=10 - i))))
    rows = await store.query(GUILD.id, since=since(), limit=2)
    assert [r["message_id"] for r in rows] == [3, 4]


async def test_prune_removes_only_rows_past_retention(store):
    now = datetime.now(timezone.utc)
    store.record(row_from_message(msg(1, created=now - timedelta(days=8))))
    store.record(row_from_message(msg(2, created=now - timedelta(days=6))))
    assert await store.prune() == 1
    rows = await store.query(GUILD.id, since=now - timedelta(days=30))
    assert [r["message_id"] for r in rows] == [2]
    assert await store.prune() == 0


async def test_one_day_retention(tmp_path):
    s = MessageLogStore(str(tmp_path / "m.db"), retention_days=1)
    await s.open()
    now = datetime.now(timezone.utc)
    s.record(row_from_message(msg(1, created=now - timedelta(hours=25))))
    s.record(row_from_message(msg(2, created=now - timedelta(hours=23))))
    assert await s.prune() == 1
    await s.close()


async def test_data_survives_reopen_and_close_flushes(tmp_path):
    path = str(tmp_path / "m.db")
    s = MessageLogStore(path, 7)
    await s.open()
    s.record(row_from_message(msg(1)))
    await s.close()  # flushes
    s2 = MessageLogStore(path, 7)
    await s2.open()
    assert (await s2.stats())["rows"] == 1
    await s2.close()


def test_format_rows_tags():
    rows = [
        {"created_at": "2026-10-20T17:05:03.000000Z", "channel_name": "do-not-type", "channel_id": 1,
         "author_name": "SpamApp", "author_id": 777, "author_kind": "app", "app_name": "SpamApp", "app_id": 777,
         "invoker_id": 5, "deleted_at": "2026-10-20T17:13:23.000000Z", "edited_at": None,
         "edited_content": None, "content": "FREE NITRO", "attachments": None},
        {"created_at": "2026-10-20T17:06:00.000000Z", "channel_name": "general", "channel_id": 2,
         "author_name": "Logs", "author_id": 9, "author_kind": "webhook", "app_name": None, "app_id": None,
         "invoker_id": None, "deleted_at": None, "edited_at": "x", "edited_content": "new",
         "content": "old", "attachments": '[{"filename": "a.png", "url": "u"}]'},
    ]
    text = format_rows(rows)
    assert "[APP SpamApp (777)] run by <@5> (5) [DELETED 2026-10-20 17:13:23]" in text
    assert "[WEBHOOK] Logs (9) [EDITED]" in text and "-> edited to: new" in text and "attachment: a.png u" in text


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

def make_cog(store, *, staff=True, ticket_channels=()):
    from cogs.message_log import MessageLog

    bot = SimpleNamespace(user=SimpleNamespace(id=BOT_ID),
                          permission_service=SimpleNamespace(is_staff=lambda m: staff))
    tickets = SimpleNamespace(get_ticket_by_channel=AsyncMock(
        side_effect=lambda cid: {"id": 1} if cid in ticket_channels else None))
    return MessageLog(bot, store, tickets), tickets


async def test_cog_stores_members_apps_and_skips_self_dm_system_tickets(store):
    cog, tickets = make_cog(store, ticket_channels={20})
    await cog.on_message(msg(1))
    await cog.on_message(msg(2, 777, bot=True, webhook_id=777, meta=app_meta(5), application_id=777,
                             mtype=discord.MessageType.chat_input_command))
    await cog.on_message(msg(3, BOT_ID, bot=True))                      # ourselves
    await cog.on_message(msg(4, mtype=discord.MessageType.pins_add))    # system
    dm = msg(5)
    dm.guild = None
    await cog.on_message(dm)
    await cog.on_message(msg(6, channel=chan(20, "ticket-0001")))       # ticket channel
    await cog.on_message(msg(7, channel=chan(20, "ticket-0001")))
    rows = await store.query(GUILD.id, since=since())
    assert [r["message_id"] for r in rows] == [1, 2]
    assert [c.args[0] for c in tickets.get_ticket_by_channel.await_args_list].count(20) == 1  # cached


async def test_cog_marks_raw_deletes(store):
    cog, _ = make_cog(store)
    await cog.on_message(msg(1))
    await cog.on_message(msg(2))
    await cog.on_raw_message_delete(SimpleNamespace(guild_id=GUILD.id, message_id=1))
    await cog.on_raw_bulk_message_delete(SimpleNamespace(guild_id=GUILD.id, message_ids={2}))
    rows = await store.query(GUILD.id, since=since(), deleted_only=True)
    assert len(rows) == 2


def test_parse_window():
    from cogs.message_log import parse_window

    assert parse_window(None, 7) == timedelta(hours=24)
    assert parse_window(None, 1) == timedelta(hours=24)
    assert parse_window("30m", 7) == timedelta(minutes=30)
    assert parse_window("30d", 7) == timedelta(days=7)  # capped at retention
    assert parse_window("0h", 7) is None and parse_window("soon", 7) is None


def ctx(author_send=None):
    author = SimpleNamespace(id=42, send=author_send or AsyncMock())
    channel = SimpleNamespace(id=10, name="general")
    guild = SimpleNamespace(id=GUILD.id, get_channel_or_thread=lambda cid: channel if cid == 10 else None)
    return SimpleNamespace(guild=guild, author=author, channel=channel, reply=AsyncMock())


async def test_msglog_staff_only(store):
    cog, _ = make_cog(store, staff=False)
    c = ctx()
    await type(cog).msglog_cmd.callback(cog, c, "deleted")
    assert "permission" in c.reply.await_args.args[0]
    c.author.send.assert_not_awaited()


async def test_msglog_exports_to_dm_never_channel(store):
    cog, _ = make_cog(store)
    await cog.on_message(msg(1, 5, content="spam spam"))
    c = ctx()
    await type(cog).msglog_cmd.callback(cog, c, "<@5>", "2h")
    sent = c.author.send.await_args
    f = sent.kwargs["file"]
    assert f.filename.startswith("msglog-") and b"spam spam" in f.fp.read()
    assert "Sent 1 message(s)" in c.reply.await_args.args[0]

    # bare id that is a channel
    c2 = ctx()
    await type(cog).msglog_cmd.callback(cog, c2, "10")
    assert "<#10>" in c2.author.send.await_args.args[0]


async def test_msglog_dm_closed(store):
    cog, _ = make_cog(store)
    await cog.on_message(msg(1))
    c = ctx(author_send=AsyncMock(side_effect=discord.HTTPException(SimpleNamespace(status=403, reason="x"), "x")))
    await type(cog).msglog_cmd.callback(cog, c, "here")
    assert "couldn't DM you" in c.reply.await_args.args[0]


async def test_msglog_status_and_usage(store):
    cog, _ = make_cog(store)
    c = ctx()
    await type(cog).msglog_cmd.callback(cog, c)
    assert "Kept for 7 day(s)" in c.reply.await_args.args[0]
    await type(cog).msglog_cmd.callback(cog, c, "who?")
    assert "Usage" in c.reply.await_args.args[0]


def test_timestamp_format_sorts_as_text():
    a = mls._ts(datetime(2026, 10, 20, 17, 5, 3, tzinfo=timezone.utc))
    b = mls._ts(datetime(2026, 10, 20, 17, 5, 3, 500, tzinfo=timezone.utc))
    assert a == "2026-10-20T17:05:03.000000Z" and a < b
