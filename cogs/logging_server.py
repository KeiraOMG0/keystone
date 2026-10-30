"""
cogs/logging_server.py — invite / emoji / sticker audit logging.

on_guild_emojis_update and on_guild_stickers_update hand us the FULL
before/after collections each time, so we diff by ID: added = after - before,
removed = before - after, logging one entry per added/removed item.

DB-only: all rare/low-value-to-spam-live events.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from database.repositories.audit_repo import AuditRepository
from services.audit_service import AuditService


class LoggingServerCog(commands.Cog, name="LoggingServer"):
    def __init__(self, bot: commands.Bot, audit: AuditService) -> None:
        self.bot = bot
        self._audit = audit

    # ------------------------------------------------------------------
    # Invites
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        actor_id = invite.inviter.id if invite.inviter is not None else self.bot.user.id  # type: ignore[union-attr]
        await self._audit.log(
            invite.guild.id, actor_id, "invite_created",
            target_type="invite", target_id=None,
            detail={
                "code": invite.code,
                "channel_id": invite.channel.id if invite.channel else None,
                "max_uses": invite.max_uses,
                "max_age": invite.max_age,
            },
        )

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        await self._audit.log(
            invite.guild.id, self.bot.user.id, "invite_deleted",  # type: ignore[union-attr]
            target_type="invite", target_id=None,
            detail={"code": invite.code},
        )

    # ------------------------------------------------------------------
    # Emojis
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self, guild: discord.Guild, before: "tuple[discord.Emoji, ...]", after: "tuple[discord.Emoji, ...]"
    ) -> None:
        actor_id = self.bot.user.id  # type: ignore[union-attr]
        before_ids = {e.id: e for e in before}
        after_ids = {e.id: e for e in after}

        added = set(after_ids) - set(before_ids)
        removed = set(before_ids) - set(after_ids)

        for emoji_id in added:
            emoji = after_ids[emoji_id]
            await self._audit.log(
                guild.id, actor_id, "emoji_added",
                target_type="emoji", target_id=emoji_id,
                detail={"name": emoji.name},
            )
        for emoji_id in removed:
            emoji = before_ids[emoji_id]
            await self._audit.log(
                guild.id, actor_id, "emoji_removed",
                target_type="emoji", target_id=emoji_id,
                detail={"name": emoji.name},
            )

    # ------------------------------------------------------------------
    # Stickers
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_guild_stickers_update(
        self, guild: discord.Guild, before: "tuple[discord.GuildSticker, ...]", after: "tuple[discord.GuildSticker, ...]"
    ) -> None:
        actor_id = self.bot.user.id  # type: ignore[union-attr]
        before_ids = {s.id: s for s in before}
        after_ids = {s.id: s for s in after}

        added = set(after_ids) - set(before_ids)
        removed = set(before_ids) - set(after_ids)

        for sticker_id in added:
            sticker = after_ids[sticker_id]
            await self._audit.log(
                guild.id, actor_id, "sticker_added",
                target_type="sticker", target_id=sticker_id,
                detail={"name": sticker.name},
            )
        for sticker_id in removed:
            sticker = before_ids[sticker_id]
            await self._audit.log(
                guild.id, actor_id, "sticker_removed",
                target_type="sticker", target_id=sticker_id,
                detail={"name": sticker.name},
            )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning("bot.db not set — logging_server cog not loaded.")
        return
    audit = AuditService(AuditRepository(bot.db))  # type: ignore[attr-defined]
    await bot.add_cog(LoggingServerCog(bot, audit))
