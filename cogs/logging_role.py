"""
cogs/logging_role.py — guild role create/delete/update audit logging.

on_guild_role_update only logs on meaningful diffs:
  - name change        -> "role_renamed"
  - permissions change  -> "role_permissions_changed"
  - color change        -> "role_color_changed" (optional/low-priority, cheap
    to include)

Position-only changes (drag-reordering the role list) are explicitly NOT
logged — pure noise, same rationale as channel logging.

DB-only: rare/admin-only events, low value to post live.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from database.repositories.audit_repo import AuditRepository
from services.audit_service import AuditService


class LoggingRoleCog(commands.Cog, name="LoggingRole"):
    def __init__(self, bot: commands.Bot, audit: AuditService) -> None:
        self.bot = bot
        self._audit = audit

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self._audit.log(
            role.guild.id, self.bot.user.id, "role_created",  # type: ignore[union-attr]
            target_type="role", target_id=role.id,
            detail={"name": role.name},
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._audit.log(
            role.guild.id, self.bot.user.id, "role_deleted",  # type: ignore[union-attr]
            target_type="role", target_id=role.id,
            detail={"name": role.name},
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        guild_id = after.guild.id
        actor_id = self.bot.user.id  # type: ignore[union-attr]

        if before.name != after.name:
            await self._audit.log(
                guild_id, actor_id, "role_renamed",
                target_type="role", target_id=after.id,
                detail={"before": before.name, "after": after.name},
            )

        if before.permissions != after.permissions:
            await self._audit.log(
                guild_id, actor_id, "role_permissions_changed",
                target_type="role", target_id=after.id,
                detail={"name": after.name},
            )

        if before.color != after.color:
            await self._audit.log(
                guild_id, actor_id, "role_color_changed",
                target_type="role", target_id=after.id,
                detail={"before": str(before.color), "after": str(after.color)},
            )

        # Position-only changes: intentionally not logged (drag-reordering
        # noise).


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning("bot.db not set — logging_role cog not loaded.")
        return
    audit = AuditService(AuditRepository(bot.db))  # type: ignore[attr-defined]
    await bot.add_cog(LoggingRoleCog(bot, audit))
