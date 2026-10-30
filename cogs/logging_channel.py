"""
cogs/logging_channel.py — guild channel create/delete/update audit logging.

on_guild_channel_update only logs on meaningful diffs:
  - name change              -> "channel_renamed"
  - topic change (if the channel type has one) -> "channel_topic_changed"
  - permission overwrite change -> "channel_permissions_changed" (distinct
    action string since this is security-relevant and staff should be able
    to find it separately from cosmetic renames)

Position-only changes (drag-reordering) and other cosmetic diffs (slowmode,
nsfw toggle) are explicitly NOT logged — pure noise.

DB-only: these are rare/admin-only events, low value to post live. Staff can
check /bot audit if needed.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from database.repositories.audit_repo import AuditRepository
from services.audit_service import AuditService


class LoggingChannelCog(commands.Cog, name="LoggingChannel"):
    def __init__(self, bot: commands.Bot, audit: AuditService) -> None:
        self.bot = bot
        self._audit = audit

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self._audit.log(
            channel.guild.id, self.bot.user.id, "channel_created",  # type: ignore[union-attr]
            target_type="channel", target_id=channel.id,
            detail={"name": channel.name, "type": str(channel.type)},
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self._audit.log(
            channel.guild.id, self.bot.user.id, "channel_deleted",  # type: ignore[union-attr]
            target_type="channel", target_id=channel.id,
            detail={"name": channel.name, "type": str(channel.type)},
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel
    ) -> None:
        guild_id = after.guild.id
        actor_id = self.bot.user.id  # type: ignore[union-attr]

        if before.name != after.name:
            await self._audit.log(
                guild_id, actor_id, "channel_renamed",
                target_type="channel", target_id=after.id,
                detail={"before": before.name, "after": after.name},
            )

        before_topic = getattr(before, "topic", None)
        after_topic = getattr(after, "topic", None)
        if before_topic != after_topic:
            await self._audit.log(
                guild_id, actor_id, "channel_topic_changed",
                target_type="channel", target_id=after.id,
                detail={"before": before_topic, "after": after_topic},
            )

        if before.overwrites != after.overwrites:
            await self._audit.log(
                guild_id, actor_id, "channel_permissions_changed",
                target_type="channel", target_id=after.id,
                detail={"name": after.name},
            )

        # Position-only changes, slowmode, nsfw toggle: intentionally not
        # logged (noise from drag-reordering / low-priority cosmetic diffs).


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning("bot.db not set — logging_channel cog not loaded.")
        return
    audit = AuditService(AuditRepository(bot.db))  # type: ignore[attr-defined]
    await bot.add_cog(LoggingChannelCog(bot, audit))
