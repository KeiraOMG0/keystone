"""
services/moderation_service.py — unified kick/ban/unban/timeout/untimeout.

Extracted from the inline logic that used to live directly in
cogs/moderation.py (!timeout/!untimeout/!ban/!unban) and cogs/kick.py
(!kick, do_kick()). Behavior is preserved EXACTLY:

  - Discord-side action performed first (member.kick/guild.ban/member.timeout).
  - discord.Forbidden / discord.HTTPException are allowed to propagate to the
    caller (the Discord cog command or the dashboard route) — this service
    does not swallow them, matching the old inline try/except-in-the-command
    structure. Callers decide how to present the error.
  - AuditService.log() is called with the SAME action strings the old inline
    code used ("member_kicked", "member_banned", "member_unbanned",
    "member_timeout", "member_untimeout") so services/log_router.py's
    category mapping and cogs/logging_moderation.py's "_native" companion
    events keep lining up with existing audit history.
  - The moderation-log / join-leave channel post uses the SAME template-name
    constants and fallback-embed builders as the old inline code
    (mod-ban-log, mod-kick-log, etc., with services/log_router.build_embed
    as the non-template fallback), so the live #moderation-log /
    #join-leave channel behavior is unchanged.

Rank checks (services/hierarchy.member_action_block_reason): kick/ban/
timeout/untimeout take `check_hierarchy=True` from every HUMAN-initiated path
(the prefix commands in cogs/moderation.py and cogs/kick.py, and the internal
API routes the dashboard calls). The check runs BEFORE any Discord call and
raises ModerationBlocked(reason) when the actor may not act on the target.
Bot-initiated paths (the auto-kick loop, the honeypot) leave it False: the
bot is the actor there and Discord itself enforces the bot's own rank.
Owner (config.OWNER_ID or the guild owner) skips only the actor rank check.
Ban-by-ID of a non-member only gets the identity rules (self/bot/owner).

Input bounds (shared by the cogs and the internal API): timeouts 1 minute to
28 days (Discord's maximum), ban message deletion 0-7 days, reasons at most
512 characters (Discord's audit-log reason limit). The service raises
ModerationInputError when a caller passes something outside them.

This service does NOT touch the `warnings` table — that remains
WarningService's job. It DOES write a companion row to `mod_actions` for
every action it performs (including warn/warning_removed, via record_warn_action
helpers called from cogs/moderation.py) so the dashboard can show one unified
per-user history without parsing audit_log's JSON detail blob.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import discord

import config as _config
from core.placeholder_engine import RenderContext
from database.repositories.mod_action_repo import ModActionRepository
from services.audit_service import AuditService
from services.hierarchy import member_action_block_reason
from services.live_events import publish as live_publish
from services.log_router import build_embed

MOD_BAN_LOG_TEMPLATE_NAME = "mod-ban-log"
MOD_UNBAN_LOG_TEMPLATE_NAME = "mod-unban-log"
MOD_TIMEOUT_LOG_TEMPLATE_NAME = "mod-timeout-log"
MOD_UNTIMEOUT_LOG_TEMPLATE_NAME = "mod-untimeout-log"
MOD_KICK_LOG_TEMPLATE_NAME = "mod-kick-log"
MOD_WARN_LOG_TEMPLATE_NAME = "mod-warn-log"
MOD_DELWARN_LOG_TEMPLATE_NAME = "mod-delwarn-log"

# Input bounds. The dashboard backend (server/routes/moderation.py) keeps its
# own copy of these numbers for its pydantic models; keep them in sync.
MAX_TIMEOUT_MINUTES = 28 * 24 * 60  # Discord's maximum timeout: 28 days
MAX_DELETE_MESSAGE_DAYS = 7
MAX_REASON_LENGTH = 512  # Discord's audit-log reason limit


class ModerationBlocked(Exception):
    """The actor may not act on the target (rank / identity rule). `reason`
    is a short sentence that is safe to show to the actor."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ModerationInputError(ValueError):
    """An input is outside the allowed bounds. str(exc) is user-facing."""


def timeout_minutes_error(minutes: Any) -> str | None:
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return "The timeout duration must be a whole number of minutes."
    if minutes < 1 or minutes > MAX_TIMEOUT_MINUTES:
        return f"The timeout duration must be between 1 minute and 28 days ({MAX_TIMEOUT_MINUTES} minutes)."
    return None


def delete_days_error(days: Any) -> str | None:
    if isinstance(days, bool) or not isinstance(days, int) or days < 0 or days > MAX_DELETE_MESSAGE_DAYS:
        return f"Message deletion must be between 0 and {MAX_DELETE_MESSAGE_DAYS} days."
    return None


def reason_error(reason: str | None) -> str | None:
    if reason is not None and len(reason) > MAX_REASON_LENGTH:
        return f"The reason is too long ({len(reason)} characters, the limit is {MAX_REASON_LENGTH})."
    return None


def _check(error: str | None) -> None:
    if error:
        raise ModerationInputError(error)


def actor_is_owner(guild: Any, user_id: int | None) -> bool:
    """Owner = config.OWNER_ID (read at call time) or the guild owner."""
    if user_id is None:
        return False
    owner_cfg = int(getattr(_config, "OWNER_ID", 0) or 0)
    if owner_cfg and user_id == owner_cfg:
        return True
    guild_owner = getattr(guild, "owner_id", None)
    return guild_owner is not None and user_id == guild_owner


def member_block_reason(guild: Any, actor: Any, target: Any) -> str | None:
    """services/hierarchy.member_action_block_reason with the owner flag and
    the bot member filled in from `guild`. `target` may be a Member or, for a
    user who is not in the guild, any object with only `.id` (identity rules
    only)."""
    return member_action_block_reason(
        actor, target, getattr(guild, "me", None),
        actor_is_owner=actor_is_owner(guild, getattr(actor, "id", None)),
        guild_owner_id=getattr(guild, "owner_id", None),
    )


class ModerationService:
    def __init__(
        self,
        mod_action_repo: ModActionRepository,
        audit: AuditService | None = None,
        templates: "Any | None" = None,
        renderer: "Any | None" = None,
    ) -> None:
        self._repo = mod_action_repo
        self._audit = audit
        self._templates = templates
        self._renderer = renderer

    # ------------------------------------------------------------------
    # Shared mod-log sender (template-rendered, falls back to a built embed
    # — identical contract to the old per-cog _send_mod_log/_send_kick_log).
    # ------------------------------------------------------------------
    async def send_mod_log(
        self,
        guild: discord.Guild,
        template_name: str,
        channel_id: int,
        fallback_embed: discord.Embed,
        member: discord.Member | discord.abc.User | None = None,
        actor: discord.abc.User | None = None,
        extra: dict | None = None,
    ) -> None:
        """`extra` feeds the moderation placeholders ({reason}, {action},
        {caseid}, {duration}, ... see core/placeholder_engine.py)."""
        log_channel = guild.get_channel(channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            return

        content = None
        embeds: list[discord.Embed] = []

        if self._templates is not None and self._renderer is not None:
            try:
                template_full = await self._templates.get_full(guild.id, template_name)
            except Exception:
                template_full = None

            if template_full is not None:
                ctx = RenderContext(guild=guild, member=member, channel=log_channel, actor=actor, extra=dict(extra or {}))
                try:
                    rendered = self._renderer.render_message(template_full, ctx)
                    content = rendered.get("content") or None
                    embeds = rendered.get("embeds") or []
                except Exception:
                    content = None
                    embeds = []

        if not content and not embeds:
            embeds = [fallback_embed]

        try:
            await log_channel.send(content=content, embeds=embeds[:10])
        except discord.HTTPException:
            pass

    @staticmethod
    def ensure_allowed(guild: Any, actor: Any, target: Any) -> None:
        """Raise ModerationBlocked when `actor` may not act on `target`."""
        reason = member_block_reason(guild, actor, target)
        if reason:
            raise ModerationBlocked(reason)

    @staticmethod
    def _live(kind: str, guild_id: int, user_id: int, moderator_id: int, reason: str | None, action_id: int, **extra) -> None:
        """Live `moderation` topic event. Never raises (live_publish guards)."""
        live_publish("moderation", {
            "kind": kind, "guild_id": guild_id, "user_id": user_id, "moderator_id": moderator_id,
            "reason": reason, "action_id": action_id, **extra,
        })

    # ------------------------------------------------------------------
    # Kick
    # ------------------------------------------------------------------
    async def kick(
        self,
        member: discord.Member,
        guild: discord.Guild,
        moderator: discord.abc.User,
        reason: str | None,
        *,
        log_channel_id: int | None = None,
        fallback_embed: discord.Embed | None = None,
        audit_detail: dict | None = None,
        check_hierarchy: bool = False,
        log_extra: dict | None = None,
    ) -> int:
        """Kicks `member`, records the action, and posts the mod log.
        `log_extra` adds placeholder data to the log template (cogs/kick.py
        passes kick_number and source).

        `fallback_embed` / `audit_detail` let cogs/kick.py's do_kick() (which
        has its own kicks.log counter and "Total Kicks" embed for the
        auto-kick-unverified flow) supply its custom embed/detail instead of
        the generic log_router.build_embed() default — same fallback
        contract as before, just routed through this service now.

        Returns the mod_actions row id. Raises discord.Forbidden /
        discord.HTTPException on Discord API failure (caller handles).
        """
        _check(reason_error(reason))
        if check_hierarchy:
            self.ensure_allowed(guild, moderator, member)
        await member.kick(reason=reason)

        action_id = await self._repo.add_action(
            guild.id, member.id, moderator.id, "kick", reason=reason,
        )
        self._live("kick", guild.id, member.id, moderator.id, reason, action_id)

        if self._audit:
            await self._audit.log(
                guild.id, moderator.id, "member_kicked",
                target_type="member", target_id=member.id,
                detail=audit_detail if audit_detail is not None else {"reason": reason},
            )

        if log_channel_id:
            embed = fallback_embed or build_embed("member_kicked", member=member, actor=moderator, reason=reason)
            await self.send_mod_log(
                guild, MOD_KICK_LOG_TEMPLATE_NAME, log_channel_id, embed,
                member=member, actor=moderator,
                extra={"action": "Kick", "reason": reason, "case_id": action_id, **(log_extra or {})},
            )

        return action_id

    # ------------------------------------------------------------------
    # Ban / Unban
    # ------------------------------------------------------------------
    async def ban(
        self,
        guild: discord.Guild,
        user_id: int,
        moderator: discord.abc.User,
        reason: str | None,
        *,
        member: discord.Member | None = None,
        log_channel_id: int | None = None,
        delete_message_days: int = 0,
        check_hierarchy: bool = False,
        log_extra: dict | None = None,
    ) -> int:
        _check(reason_error(reason))
        _check(delete_days_error(delete_message_days))
        target = member or discord.Object(id=user_id)
        if check_hierarchy:
            self.ensure_allowed(guild, moderator, target)
        await guild.ban(target, reason=reason, delete_message_days=delete_message_days)

        action_id = await self._repo.add_action(
            guild.id, user_id, moderator.id, "ban", reason=reason,
        )
        self._live("ban", guild.id, user_id, moderator.id, reason, action_id)

        if self._audit:
            await self._audit.log(
                guild.id, moderator.id, "member_banned",
                target_type="member", target_id=user_id,
                detail={"reason": reason},
            )

        if log_channel_id:
            fallback = build_embed("member_banned", member_id=user_id, actor=moderator, reason=reason)
            await self.send_mod_log(
                guild, MOD_BAN_LOG_TEMPLATE_NAME, log_channel_id, fallback,
                member=member, actor=moderator,
                extra={"action": "Ban", "reason": reason, "case_id": action_id, "target_id": user_id,
                       "target_user": target if isinstance(target, discord.abc.User) else None,
                       "delete_days": delete_message_days, **(log_extra or {})},
            )

        return action_id

    async def unban(
        self,
        guild: discord.Guild,
        user_id: int,
        moderator: discord.abc.User,
        reason: str | None,
        *,
        log_channel_id: int | None = None,
        log_extra: dict | None = None,
    ) -> int:
        _check(reason_error(reason))
        await guild.unban(discord.Object(id=user_id), reason=reason)

        action_id = await self._repo.add_action(
            guild.id, user_id, moderator.id, "unban", reason=reason,
        )
        self._live("unban", guild.id, user_id, moderator.id, reason, action_id)

        if self._audit:
            await self._audit.log(
                guild.id, moderator.id, "member_unbanned",
                target_type="member", target_id=user_id,
                detail={"reason": reason},
            )

        if log_channel_id:
            fallback = build_embed("member_unbanned", member_id=user_id, actor=moderator, reason=reason)
            await self.send_mod_log(
                guild, MOD_UNBAN_LOG_TEMPLATE_NAME, log_channel_id, fallback,
                member=None, actor=moderator,
                extra={"action": "Unban", "reason": reason, "case_id": action_id, "target_id": user_id,
                       **(log_extra or {})},
            )

        return action_id

    # ------------------------------------------------------------------
    # Timeout / Untimeout
    # ------------------------------------------------------------------
    async def timeout(
        self,
        member: discord.Member,
        guild: discord.Guild,
        moderator: discord.abc.User,
        duration_minutes: int,
        reason: str | None,
        *,
        log_channel_id: int | None = None,
        check_hierarchy: bool = False,
        log_extra: dict | None = None,
    ) -> int:
        _check(timeout_minutes_error(duration_minutes))
        _check(reason_error(reason))
        if check_hierarchy:
            self.ensure_allowed(guild, moderator, member)
        until = discord.utils.utcnow() + timedelta(minutes=duration_minutes)
        await member.timeout(until, reason=reason)

        action_id = await self._repo.add_action(
            guild.id, member.id, moderator.id, "timeout", reason=reason,
            duration_minutes=duration_minutes, expires_at=until.isoformat(),
        )
        self._live("timeout", guild.id, member.id, moderator.id, reason, action_id,
                   duration_minutes=duration_minutes)

        if self._audit:
            await self._audit.log(
                guild.id, moderator.id, "member_timeout",
                target_type="member", target_id=member.id,
                detail={"duration_minutes": duration_minutes, "reason": reason},
            )

        if log_channel_id:
            fallback = build_embed(
                "member_timeout", member=member, actor=moderator, reason=reason,
                extra_fields={"Duration": f"{duration_minutes} minute(s)"},
            )
            await self.send_mod_log(
                guild, MOD_TIMEOUT_LOG_TEMPLATE_NAME, log_channel_id, fallback,
                member=member, actor=moderator,
                extra={"action": "Timeout", "reason": reason, "case_id": action_id,
                       "duration_minutes": duration_minutes,
                       "expires_at": until, **(log_extra or {})},
            )

        return action_id

    async def untimeout(
        self,
        member: discord.Member,
        guild: discord.Guild,
        moderator: discord.abc.User,
        reason: str | None,
        *,
        log_channel_id: int | None = None,
        check_hierarchy: bool = False,
        log_extra: dict | None = None,
    ) -> int:
        _check(reason_error(reason))
        if check_hierarchy:
            self.ensure_allowed(guild, moderator, member)
        await member.timeout(None, reason=reason)

        action_id = await self._repo.add_action(
            guild.id, member.id, moderator.id, "untimeout", reason=reason,
        )
        self._live("untimeout", guild.id, member.id, moderator.id, reason, action_id)

        if self._audit:
            await self._audit.log(
                guild.id, moderator.id, "member_untimeout",
                target_type="member", target_id=member.id,
                detail={"reason": reason},
            )

        if log_channel_id:
            fallback = build_embed("member_untimeout", member=member, actor=moderator, reason=reason)
            await self.send_mod_log(
                guild, MOD_UNTIMEOUT_LOG_TEMPLATE_NAME, log_channel_id, fallback,
                member=member, actor=moderator,
                extra={"action": "Timeout removed", "reason": reason, "case_id": action_id, **(log_extra or {})},
            )

        return action_id

    # ------------------------------------------------------------------
    # Warn companion rows (warnings table stays the system of record;
    # this just mirrors into mod_actions for the unified history view).
    # ------------------------------------------------------------------
    async def record_warn_action(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str | None, warning_id: int,
    ) -> int:
        return await self._repo.add_action(
            guild_id, user_id, moderator_id, "warn", reason=reason,
            extra_data={"warning_id": warning_id},
        )

    async def record_warning_removed_action(
        self, guild_id: int, user_id: int, moderator_id: int, warning_id: int,
    ) -> int:
        return await self._repo.add_action(
            guild_id, user_id, moderator_id, "warning_removed",
            extra_data={"warning_id": warning_id},
        )

    # ------------------------------------------------------------------
    # Unified history
    # ------------------------------------------------------------------
    async def get_history(self, guild_id: int, user_id: int, limit: int = 100) -> list[dict]:
        return await self._repo.get_actions(guild_id, user_id, limit)
