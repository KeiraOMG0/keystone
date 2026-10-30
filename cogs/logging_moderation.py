"""
cogs/logging_moderation.py — gateway-level moderation audit logging.

Covers actions performed through Discord's native UI (server settings / member
context menu), which are otherwise invisible to the bot since they don't go
through cogs/moderation.py's !ban/!unban/!timeout/!untimeout commands:

  - on_member_ban / on_member_unban   (native bans/unbans)
  - on_member_update diff for communication_disabled_until (native timeouts)

Actor attribution: after the gateway event fires, we read the last few
guild.audit_logs(action=...) entries and take `.user` from the first entry
whose TARGET is this member and that was created within ACTOR_MATCH_WINDOW
seconds (pick_actor_entry). If none matches yet we wait AUDIT_RETRY_DELAY once
and look again (the audit-log entry can land a moment after the gateway
event). This requires "View Audit Log". If nothing matches (missing
permission, HTTP error, no fresh entry for this member) the moderator is
"unknown": the embed says so and the audit_log row uses actor_id 0 with
detail {"actor": "unknown"}. It used to take the newest entry of that type
without checking the target or its age (and fell back to the bot's id), which
credited the wrong moderator.

Double-logging note: cogs/moderation.py's !ban/!unban/!timeout/!untimeout
commands ALREADY audit-log these actions (as "member_banned"/"member_unbanned"/
"member_timeout"/"member_untimeout") at the command level. The gateway events
in THIS file fire for BOTH bot-driven and manually-performed actions, and
there is no reliable way to distinguish "this gateway event was caused by our
own bot a moment ago" from "this was done manually" without fragile timing
heuristics. Rather than attempt deduplication, this cog uses DISTINCT action
strings (member_banned_native / member_unbanned_native / member_timeout_native
/ member_untimeout_native) so both can coexist in `/bot audit` output without
looking like a bug. Known, accepted tradeoff: staff using !ban will see TWO
audit entries — one from the command, one from this gateway confirmation.

Routing: bans/unbans -> "join_leave" category (bans are membership removals,
per existing convention in log_router.py / moderation.py). Timeouts ->
"moderation" category.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import discord
from discord.ext import commands

from database.repositories.audit_repo import AuditRepository
from services.audit_service import AuditService
from services.log_router import build_embed, route_and_post

log = logging.getLogger(__name__)

ACTOR_MATCH_WINDOW = 30.0  # seconds between the audit-log entry and now
AUDIT_LOOKBACK = 10  # audit-log entries read per lookup
AUDIT_RETRY_DELAY = 2.0  # seconds before the single retry
UNKNOWN_ACTOR_ID = 0  # audit_log.actor_id is NOT NULL; 0 = unknown moderator


def is_timeout_change(entry: Any) -> bool:
    """member_update entries also cover nickname/role edits; only one that
    changed the timeout (discord.py's `timed_out_until` diff) counts."""
    return hasattr(getattr(entry, "after", None), "timed_out_until") or hasattr(
        getattr(entry, "before", None), "timed_out_until"
    )


def pick_actor_entry(
    entries: Iterable[Any], target_id: int, now: datetime, window: float = ACTOR_MATCH_WINDOW,
    predicate: Callable[[Any], bool] | None = None,
) -> Any | None:
    """First entry whose target is `target_id`, created within `window`
    seconds of `now` (either side, to tolerate clock skew), with a user, and
    (when given) accepted by `predicate`."""
    for entry in entries:
        if getattr(getattr(entry, "target", None), "id", None) != target_id:
            continue
        created = getattr(entry, "created_at", None)
        if created is None or abs((now - created).total_seconds()) > window:
            continue
        if getattr(entry, "user", None) is None:
            continue
        if predicate is not None and not predicate(entry):
            continue
        return entry
    return None


class LoggingModerationCog(commands.Cog, name="LoggingModeration"):
    def __init__(self, bot: commands.Bot, audit: AuditService) -> None:
        self.bot = bot
        self._audit = audit

    # ------------------------------------------------------------------
    # Actor attribution helper
    # ------------------------------------------------------------------
    async def _resolve_actor_id(
        self,
        guild: discord.Guild,
        audit_action: discord.AuditLogAction,
        gateway_action: str,
        target_id: int,
        predicate: Callable[[Any], bool] | None = None,
    ) -> int | None:
        """Who performed `gateway_action` on `target_id`, or None (unknown).
        See the module docstring for the matching rules."""
        for attempt in range(2):
            try:
                entries = [e async for e in guild.audit_logs(action=audit_action, limit=AUDIT_LOOKBACK)]
            except discord.Forbidden:
                log.warning(
                    "logging_moderation: missing View Audit Log permission in guild %s; "
                    "moderator for %s is unknown", guild.id, gateway_action,
                )
                return None
            except discord.HTTPException as e:
                log.warning(
                    "logging_moderation: audit log lookup failed for %s in guild %s: %s; moderator unknown",
                    gateway_action, guild.id, e,
                )
                return None
            entry = pick_actor_entry(entries, target_id, datetime.now(timezone.utc), predicate=predicate)
            if entry is not None:
                return entry.user.id
            if attempt == 0:
                await asyncio.sleep(AUDIT_RETRY_DELAY)

        log.info(
            "logging_moderation: no recent audit log entry for %s on %s in guild %s; moderator unknown",
            gateway_action, target_id, guild.id,
        )
        return None

    async def _record(
        self, guild: discord.Guild, action: str, target_id: int, actor_id: int | None,
        detail: dict | None, embed: discord.Embed,
    ) -> None:
        if actor_id is None:
            detail = {**(detail or {}), "actor": "unknown"}
            embed.add_field(name="Moderator", value="Unknown", inline=False)
        await self._audit.log(
            guild.id, actor_id if actor_id is not None else UNKNOWN_ACTOR_ID, action,
            target_type="member", target_id=target_id, detail=detail,
        )
        await route_and_post(self.bot, guild, action, embed)

    # ------------------------------------------------------------------
    # Native bans / unbans
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        actor_id = await self._resolve_actor_id(guild, discord.AuditLogAction.ban, "member_banned_native", user.id)
        embed = build_embed("member_banned_native", member_id=user.id, actor_id=actor_id)
        await self._record(guild, "member_banned_native", user.id, actor_id, None, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        actor_id = await self._resolve_actor_id(guild, discord.AuditLogAction.unban, "member_unbanned_native", user.id)
        embed = build_embed("member_unbanned_native", member_id=user.id, actor_id=actor_id)
        await self._record(guild, "member_unbanned_native", user.id, actor_id, None, embed)

    # ------------------------------------------------------------------
    # Native timeouts (separate on_member_update listener — see module
    # docstring; gateway.py owns role diffing, logging_member.py owns
    # nickname diffing, this listener owns timeout diffing only).
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.timed_out_until == after.timed_out_until:
            return

        guild = after.guild
        was_timed_out = before.timed_out_until is not None
        is_timed_out = after.timed_out_until is not None

        if not was_timed_out and is_timed_out:
            actor_id = await self._resolve_actor_id(
                guild, discord.AuditLogAction.member_update, "member_timeout_native", after.id, is_timeout_change
            )
            embed = build_embed(
                "member_timeout_native", member=after, actor_id=actor_id,
                extra_fields={"Until": discord.utils.format_dt(after.timed_out_until, "F")}
                if after.timed_out_until else None,
            )
            await self._record(
                guild, "member_timeout_native", after.id, actor_id,
                {"until": after.timed_out_until.isoformat() if after.timed_out_until else None}, embed,
            )
        elif was_timed_out and not is_timed_out:
            actor_id = await self._resolve_actor_id(
                guild, discord.AuditLogAction.member_update, "member_untimeout_native", after.id, is_timeout_change
            )
            embed = build_embed("member_untimeout_native", member=after, actor_id=actor_id)
            await self._record(guild, "member_untimeout_native", after.id, actor_id, None, embed)
        # else: timeout duration changed while already timed out — treated as
        # a re-timeout update; not distinctly logged to avoid noise (the new
        # expiry is visible via a fresh native timeout event only when the
        # transition is None -> set, matching Discord's own audit log UX).


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging as _logging
        _logging.getLogger(__name__).warning("bot.db not set — logging_moderation cog not loaded.")
        return
    audit = AuditService(AuditRepository(bot.db))  # type: ignore[attr-defined]
    await bot.add_cog(LoggingModerationCog(bot, audit))
