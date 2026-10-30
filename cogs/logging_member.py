"""
cogs/logging_member.py — nickname-change audit logging.

Separate from cogs/gateway.py's existing on_member_update listener (which
handles role diffing). discord.py fires on_member_update independently for
every cog that registers it, so this cog owns a second, nickname-only
listener without touching gateway.py.

Global avatar-change tracking (on_user_update) is explicitly out of scope —
confirmed with the user as not worth the architectural awkwardness.

DB-only: no live channel post for nickname changes (low-value, not
moderation-relevant).
"""
from __future__ import annotations

import discord
from discord.ext import commands

from database.repositories.audit_repo import AuditRepository
from services.audit_service import AuditService


class LoggingMemberCog(commands.Cog, name="LoggingMember"):
    def __init__(self, bot: commands.Bot, audit: AuditService) -> None:
        self.bot = bot
        self._audit = audit

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick == after.nick:
            return
        await self._audit.log(
            after.guild.id, after.id, "member_nickname_changed",
            target_type="member", target_id=after.id,
            detail={"before": before.nick, "after": after.nick},
        )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning("bot.db not set — logging_member cog not loaded.")
        return
    audit = AuditService(AuditRepository(bot.db))  # type: ignore[attr-defined]
    await bot.add_cog(LoggingMemberCog(bot, audit))
