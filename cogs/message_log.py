"""
cogs/message_log.py — short-term moderation message log (its own file).

Records every guild message into services/message_log_service.py's separate
SQLite file (config.MESSAGE_LOG_PATH) and keeps it for
MESSAGE_LOG_RETENTION_DAYS: a daily job (also run at startup) deletes older
rows and shrinks the file. Edits and deletes (single, bulk, `!purge`,
anti-spam, honeypot) are marked on the stored row, so what was removed can
still be read back. Members, bots, webhooks and app replies are all stored
(app replies with the member who ran the command), which is what you need
after a spam wave you couldn't identify.

Never stored: DMs, this bot's own messages, system messages (joins, pins,
boosts) and ticket channels (tickets have their own opt-in chat log, the
`ticket_chat_logging_enabled` setting; a ticket channel stays a ticket
channel, so a positive lookup is cached for good, a negative one for 60 s).

`!msglog` (staff only: owner / STAFF_ROLE_IDS) DMs a .txt export:
  !msglog                         status (rows, oldest, file, retention)
  !msglog <@user|user ID> [dur]   that user's messages, including app replies they triggered
  !msglog <#channel|ID|here> [dur]
  !msglog deleted [dur]           only messages that were deleted
`dur` is 30m / 6h / 2d (default 24h, at most the retention). Exports go to
DMs only, never the channel, because they contain other members' messages.

Turn it off with MESSAGE_LOG_ENABLED=false (the cog is then not added; delete
the file to drop what was stored).
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
from datetime import timedelta

import discord
from discord.ext import commands, tasks

import config as _config
from services.message_log_service import (
    DEFAULT_WINDOW,
    FLUSH_MAX_OPS,
    MessageLogStore,
    build_export,
    parse_window,
    row_from_message,
    send_export_dm,
)
from services.message_source import message_text

log = logging.getLogger(__name__)

_STORED_TYPES = (
    discord.MessageType.default,
    discord.MessageType.reply,
    discord.MessageType.chat_input_command,
    discord.MessageType.context_menu_command,
)
FLUSH_SECONDS = 3
TICKET_NEGATIVE_CACHE_SECONDS = 60.0
_CHANNEL_RE = re.compile(r"^<#(\d+)>$")
_USER_RE = re.compile(r"^<@!?(\d+)>$")
USAGE = (
    "Usage: `!msglog` (status), `!msglog <@user or ID> [30m|6h|2d]`, "
    "`!msglog <#channel, ID or here> [duration]`, `!msglog deleted [duration]`."
)


# parse_window / build_export / send_export_dm live in
# services/message_log_service.py (shared with the dashboard export).


class MessageLog(commands.Cog):
    def __init__(self, bot: commands.Bot, store: MessageLogStore, ticket_repo) -> None:
        self.bot = bot
        self.store = store
        self._tickets = ticket_repo
        self._ticket_cache: dict[int, tuple[float, bool]] = {}

    async def cog_load(self) -> None:
        await self.store.open()
        self.flush_loop.start()
        self.prune_loop.start()
        log.info(
            "message log: storing messages in %s for %d day(s)",
            os.path.abspath(self.store.path), self.store.retention_days,
        )

    async def cog_unload(self) -> None:
        self.flush_loop.cancel()
        self.prune_loop.cancel()
        await self.store.close()

    @tasks.loop(seconds=FLUSH_SECONDS)
    async def flush_loop(self) -> None:
        await self.store.flush()

    @tasks.loop(hours=24)
    async def prune_loop(self) -> None:
        # First run is right at startup, then every 24 h.
        try:
            removed = await self.store.prune()
            if removed:
                log.info("message log: pruned %d message(s) older than %d day(s)", removed, self.store.retention_days)
        except Exception:
            log.exception("message log: prune failed")

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    async def _is_ticket_channel(self, channel_id: int) -> bool:
        now = time.monotonic()
        hit = self._ticket_cache.get(channel_id)
        if hit is not None and (hit[1] or hit[0] > now):
            return hit[1]
        try:
            is_ticket = await self._tickets.get_ticket_by_channel(channel_id) is not None
        except Exception:
            log.exception("message log: ticket lookup failed; not storing to be safe")
            return True
        self._ticket_cache[channel_id] = (now + TICKET_NEGATIVE_CACHE_SECONDS, is_ticket)
        return is_ticket

    def _is_self(self, author) -> bool:
        me = self.bot.user
        return me is not None and getattr(author, "id", None) == me.id

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or self._is_self(message.author) or message.type not in _STORED_TYPES:
            return
        try:
            if await self._is_ticket_channel(message.channel.id):
                return
            self.store.record(row_from_message(message))
            if self.store.pending() >= FLUSH_MAX_OPS:
                await self.store.flush()
        except Exception:
            log.exception("message log: could not record message %s", message.id)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is None:
            return
        message = getattr(payload, "message", None)
        if message is None or self._is_self(message.author):
            return
        self.store.edited(payload.message_id, message_text(message))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is not None:
            self.store.deleted([payload.message_id])

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id is not None:
            self.store.deleted(payload.message_ids)

    async def _audit_export(self, guild_id, actor_id, label, window_label, count, user_id, channel_id, deleted_only) -> None:
        """`message_log_exported` audit row (same as the dashboard export)."""
        db = getattr(self.bot, "db", None)
        if db is None:
            return
        try:
            from database.repositories.audit_repo import AuditRepository
            from services.audit_service import AuditService

            await AuditService(AuditRepository(db)).log(
                guild_id, actor_id, "message_log_exported",
                target_type="user" if user_id else "channel" if channel_id else None,
                target_id=user_id or channel_id,
                detail={"window": window_label, "count": count, "deleted_only": deleted_only, "source": "command"},
            )
        except Exception:
            log.exception("message log: could not write the export audit row")

    # ------------------------------------------------------------------
    # !msglog
    # ------------------------------------------------------------------
    @commands.command(name="msglog", hidden=True)
    async def msglog_cmd(self, ctx: commands.Context, target: str = None, duration: str = None):
        if ctx.guild is None or not self.bot.permission_service.is_staff(ctx.author):  # type: ignore[attr-defined]
            await ctx.reply("You don't have permission to use this command.")
            return

        if target is None:
            s = await self.store.stats()
            size_kb = os.path.getsize(self.store.path) // 1024 if os.path.exists(self.store.path) else 0
            oldest = s["oldest"][:16].replace("T", " ") + " UTC" if s["oldest"] else "nothing yet"
            await ctx.reply(
                f"Message log: **{s['rows']}** message(s) stored ({s['deleted']} deleted), oldest {oldest}.\n"
                f"Kept for {self.store.retention_days} day(s), pruned daily. File: `{os.path.basename(self.store.path)}` "
                f"({size_kb} KB). Ticket channels and DMs are never stored.\n{USAGE}"
            )
            return

        window = parse_window(duration, self.store.retention_days)
        if window is None:
            await ctx.reply(USAGE)
            return

        user_id = channel_id = None
        deleted_only = False
        raw = target.strip()
        label: str
        if raw.lower() == "deleted":
            deleted_only, label = True, "deleted messages"
        elif raw.lower() == "here":
            channel_id, label = ctx.channel.id, f"#{ctx.channel.name}"
        elif m := _CHANNEL_RE.match(raw):
            channel_id = int(m.group(1))
            label = f"<#{channel_id}>"
        elif m := _USER_RE.match(raw):
            user_id = int(m.group(1))
            label = f"<@{user_id}>"
        elif raw.isdigit():
            # A bare ID: a channel if the server has one with it, else a user.
            if ctx.guild.get_channel_or_thread(int(raw)) is not None:
                channel_id, label = int(raw), f"<#{raw}>"
            else:
                user_id, label = int(raw), f"<@{raw}>"
        else:
            await ctx.reply(USAGE)
            return

        rows, data, filename = await build_export(
            self.store, ctx.guild.id, window=window, user_id=user_id, channel_id=channel_id,
            deleted_only=deleted_only, label=label, window_label=duration or "24h",
            exported_by=f"{ctx.author} ({ctx.author.id})",
        )
        if not rows:
            await ctx.reply(f"No stored messages for {label} in that window.", allowed_mentions=discord.AllowedMentions.none())
            return

        try:
            await send_export_dm(ctx.author, label, len(rows), data, filename)
        except discord.HTTPException:
            await ctx.reply("I couldn't DM you the export. Open your DMs from this server and try again.")
            return
        await self._audit_export(ctx.guild.id, ctx.author.id, label, duration or "24h", len(rows), user_id, channel_id, deleted_only)
        await ctx.reply(
            f"Sent {len(rows)} message(s) for {label} to your DMs.", allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    if not _config.MESSAGE_LOG_ENABLED:
        log.info("message log: disabled (MESSAGE_LOG_ENABLED=false)")
        return
    if not hasattr(bot, "db") or bot.db is None:
        log.warning("bot.db not available - message_log cog not loaded.")
        return
    from database.repositories.ticket_repo import TicketRepository

    store = MessageLogStore(_config.MESSAGE_LOG_PATH, _config.MESSAGE_LOG_RETENTION_DAYS)
    await bot.add_cog(MessageLog(bot, store, TicketRepository(bot.db)))
