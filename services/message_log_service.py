"""
services/message_log_service.py — short-term moderation message log.

A separate SQLite file (config.MESSAGE_LOG_PATH, default `message_log.db`),
NOT bot.db: it holds a lot of rows that are worthless after a few days, so it
is kept apart from the real data and can simply be deleted (the bot recreates
it on start). No migration system: the schema is created with IF NOT EXISTS
and versioned with PRAGMA user_version.

What is stored per message: ids, channel name, author name, author kind
(member / bot / webhook / app), for app replies the member who ran the
command (invoker_id) and the app, the text including embed text, attachment
file names and URLs (Discord's attachment URLs expire, the names stay
useful), created_at, the latest edited text and when, and when it was deleted
(single deletes, bulk deletes and purges all mark rows, so a purged channel
can still be read back here until the retention runs out).

Writes are buffered (record/edit/delete only append to a list; flush() writes
them in order in one transaction; cogs/message_log.py flushes every few
seconds) so a busy channel costs one commit per flush, not per message.
prune() deletes rows older than the retention and VACUUMs the file.

Timestamps are UTC strings in one fixed format (_ts) so they sort and compare
as text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from services.message_source import app_source, author_kind, message_text

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FLUSH_MAX_OPS = 500  # flush early when this many writes are waiting
QUERY_MAX_ROWS = 5000
CONTENT_MAX = 4000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id     INTEGER PRIMARY KEY,
    guild_id       INTEGER NOT NULL,
    channel_id     INTEGER NOT NULL,
    channel_name   TEXT,
    author_id      INTEGER NOT NULL,
    author_name    TEXT,
    author_kind    TEXT NOT NULL,      -- member | bot | webhook | app
    invoker_id     INTEGER,            -- app replies: the member who ran the command
    app_id         INTEGER,
    app_name       TEXT,
    webhook_id     INTEGER,
    content        TEXT,
    attachments    TEXT,               -- JSON [{filename, url}]
    created_at     TEXT NOT NULL,
    edited_content TEXT,
    edited_at      TEXT,
    deleted_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_invoker ON messages(invoker_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, created_at);
"""

_INSERT = """
INSERT OR IGNORE INTO messages
    (message_id, guild_id, channel_id, channel_name, author_id, author_name, author_kind,
     invoker_id, app_id, app_name, webhook_id, content, attachments, created_at)
VALUES (:message_id, :guild_id, :channel_id, :channel_name, :author_id, :author_name, :author_kind,
        :invoker_id, :app_id, :app_name, :webhook_id, :content, :attachments, :created_at)
"""


def _ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def row_from_message(message) -> dict[str, Any]:
    """The `messages` row for a discord.Message (duck-typed)."""
    via = app_source(message)
    author = message.author
    attachments = [
        {"filename": getattr(a, "filename", None), "url": getattr(a, "url", None)}
        for a in list(getattr(message, "attachments", None) or [])[:10]
    ]
    created = getattr(message, "created_at", None) or _now()
    return {
        "message_id": message.id,
        "guild_id": message.guild.id,
        "channel_id": message.channel.id,
        "channel_name": getattr(message.channel, "name", None),
        "author_id": author.id,
        "author_name": str(getattr(author, "name", None) or author.id),
        "author_kind": author_kind(message, via),
        "invoker_id": getattr(via["invoker"], "id", None) if via else None,
        "app_id": via["app_id"] if via else None,
        "app_name": via["app_name"] if via else None,
        "webhook_id": getattr(message, "webhook_id", None),
        "content": message_text(message)[:CONTENT_MAX],
        "attachments": json.dumps(attachments) if attachments else None,
        "created_at": _ts(created),
    }


class MessageLogStore:
    def __init__(self, path: str, retention_days: int) -> None:
        self.path = path
        self.retention_days = retention_days
        self._conn: aiosqlite.Connection | None = None
        self._ops: list[tuple] = []
        self._flush_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        try:
            await self.flush()
        finally:
            await self._conn.close()
            self._conn = None

    # -- buffered writes ---------------------------------------------------
    def record(self, row: dict[str, Any]) -> None:
        self._ops.append(("insert", row))

    def edited(self, message_id: int, content: str, at: datetime | None = None) -> None:
        self._ops.append(("edit", message_id, content[:CONTENT_MAX], _ts(at or _now())))

    def deleted(self, message_ids, at: datetime | None = None) -> None:
        ids = [int(i) for i in message_ids]
        if ids:
            self._ops.append(("delete", ids, _ts(at or _now())))

    def pending(self) -> int:
        return len(self._ops)

    async def flush(self) -> int:
        """Write every buffered op, in order, in one transaction."""
        async with self._flush_lock:
            return await self._flush_locked()

    async def _flush_locked(self) -> int:
        if self._conn is None or not self._ops:
            return 0
        ops, self._ops = self._ops, []
        try:
            for op in ops:
                if op[0] == "insert":
                    await self._conn.execute(_INSERT, op[1])
                elif op[0] == "edit":
                    # Embed/link-preview resolution also fires edits: only a
                    # real text change counts.
                    await self._conn.execute(
                        "UPDATE messages SET edited_content = ?, edited_at = ? "
                        "WHERE message_id = ? AND COALESCE(edited_content, content, '') != ?",
                        (op[2], op[3], op[1], op[2]),
                    )
                else:
                    ids, at = op[1], op[2]
                    for start in range(0, len(ids), 500):
                        chunk = ids[start:start + 500]
                        marks = ",".join("?" * len(chunk))
                        await self._conn.execute(
                            f"UPDATE messages SET deleted_at = ? WHERE deleted_at IS NULL AND message_id IN ({marks})",
                            (at, *chunk),
                        )
            await self._conn.commit()
        except Exception:
            log.exception("message log: flush of %d op(s) failed; dropped", len(ops))
            try:
                await self._conn.rollback()
            except Exception:
                pass
            return 0
        return len(ops)

    # -- retention ---------------------------------------------------------
    async def prune(self, now: datetime | None = None) -> int:
        """Delete rows older than the retention, then shrink the file."""
        if self._conn is None:
            return 0
        await self.flush()
        cutoff = _ts((now or _now()) - timedelta(days=self.retention_days))
        cur = await self._conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        removed = cur.rowcount or 0
        await self._conn.commit()
        if removed:
            await self._conn.execute("VACUUM")
        return removed

    # -- reads -------------------------------------------------------------
    async def query(
        self,
        guild_id: int,
        *,
        since: datetime,
        user_id: int | None = None,
        channel_id: int | None = None,
        deleted_only: bool = False,
        limit: int = QUERY_MAX_ROWS,
    ) -> list[dict]:
        """Oldest first. user_id matches the author OR the member who ran an app."""
        await self.flush()
        where = ["guild_id = ?", "created_at >= ?"]
        params: list[Any] = [guild_id, _ts(since)]
        if user_id is not None:
            where.append("(author_id = ? OR invoker_id = ?)")
            params += [user_id, user_id]
        if channel_id is not None:
            where.append("channel_id = ?")
            params.append(channel_id)
        if deleted_only:
            where.append("deleted_at IS NOT NULL")
        sql = (
            f"SELECT * FROM (SELECT * FROM messages WHERE {' AND '.join(where)} "
            f"ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC"
        )
        params.append(min(limit, QUERY_MAX_ROWS))
        async with self._conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def stats(self) -> dict:
        await self.flush()
        async with self._conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest, "
            "SUM(deleted_at IS NOT NULL) AS deleted FROM messages"
        ) as cur:
            row = await cur.fetchone()
        return {"rows": row["n"], "oldest": row["oldest"], "deleted": row["deleted"] or 0}


def format_rows(rows: list[dict]) -> str:
    """Plain-text export, one block per message."""
    lines = []
    for r in rows:
        when = r["created_at"][:19].replace("T", " ") + " UTC"
        who = f"{r['author_name']} ({r['author_id']})"
        if r["author_kind"] == "app":
            who = f"[APP {r['app_name']} ({r['app_id']})] run by <@{r['invoker_id']}> ({r['invoker_id']})"
        elif r["author_kind"] != "member":
            who = f"[{r['author_kind'].upper()}] {who}"
        tags = []
        if r.get("deleted_at"):
            tags.append(f"DELETED {r['deleted_at'][:19].replace('T', ' ')}")
        if r.get("edited_at"):
            tags.append("EDITED")
        tag = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"[{when}] #{r['channel_name'] or r['channel_id']} | {who}{tag}")
        lines.append(f"    {(r['content'] or '(no text)').replace(chr(10), chr(10) + '    ')}")
        if r.get("edited_content") is not None:
            lines.append(f"    -> edited to: {r['edited_content'].replace(chr(10), chr(10) + '    ')}")
        if r.get("attachments"):
            for a in json.loads(r["attachments"]):
                lines.append(f"    attachment: {a.get('filename')} {a.get('url')}")
    return "\n".join(lines) + ("\n" if lines else "")


# ---------------------------------------------------------------------------
# Export (`!msglog` and the dashboard's "Send to my DMs"; both DM-only: the
# log holds other members' messages, so an export never goes to a channel or
# a browser download).
# ---------------------------------------------------------------------------

DEFAULT_WINDOW = timedelta(hours=24)
_WINDOW_RE = re.compile(r"^(\d+)\s*([mhd])$", re.IGNORECASE)
_WINDOW_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_window(raw: str | None, retention_days: int) -> timedelta | None:
    """'6h' -> timedelta; None for a bad value. Capped at the retention.
    None (no value) means the default 24 h (also capped)."""
    if raw is None:
        return min(DEFAULT_WINDOW, timedelta(days=retention_days))
    match = _WINDOW_RE.match(raw.strip())
    if not match or int(match.group(1)) <= 0:
        return None
    window = timedelta(**{_WINDOW_UNITS[match.group(2).lower()]: int(match.group(1))})
    return min(window, timedelta(days=retention_days))


async def build_export(
    store: "MessageLogStore",
    guild_id: int,
    *,
    window: timedelta,
    user_id: int | None = None,
    channel_id: int | None = None,
    deleted_only: bool = False,
    label: str,
    window_label: str,
    exported_by: str,
) -> tuple[list[dict], bytes, str]:
    """(rows, file bytes, filename). rows == [] means nothing to send."""
    rows = await store.query(
        guild_id, since=_now() - window, user_id=user_id, channel_id=channel_id, deleted_only=deleted_only,
    )
    if not rows:
        return rows, b"", ""
    header = (
        f"Message log export: {label} | last {window_label} | {len(rows)} message(s) | "
        f"exported by {exported_by} at {_now():%Y-%m-%d %H:%M} UTC\n\n"
    )
    data = (header + format_rows(rows)).encode("utf-8")
    return rows, data, f"msglog-{_now():%Y%m%d-%H%M}.txt"


async def send_export_dm(user, label: str, count: int, data: bytes, filename: str) -> None:
    """DM the export file. discord.HTTPException propagates (DMs closed)."""
    import io

    import discord

    await user.send(
        f"Message log for {label} ({count} message(s)).",
        file=discord.File(io.BytesIO(data), filename=filename),
        allowed_mentions=discord.AllowedMentions.none(),
    )
