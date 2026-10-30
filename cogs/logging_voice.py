"""
cogs/logging_voice.py — voice channel join/leave/switch and server-mute/deafen
audit logging.

on_voice_state_update diffs for:
  - channel join   (before.channel is None, after.channel is not None)
  - channel leave  (before.channel is not None, after.channel is None)
  - channel switch (both set, different channel)
  - server-mute/deafen (before.mute != after.mute, before.deaf != after.deaf)
    — moderator-applied, NOT self-mute/self-deafen.

self_mute / self_deaf toggles are explicitly IGNORED under all circumstances
(approved noise-reduction: these are user-initiated, not moderation-relevant).

Join/leave/switch: DB-only, no live post (high volume per the design
analysis). Server-mute/deafen: DB-log AND route to log_router's "moderation"
category (rare, genuinely moderation-relevant).
"""
from __future__ import annotations

import discord
from discord.ext import commands

from database.repositories.audit_repo import AuditRepository
from services.audit_service import AuditService
from services.log_router import build_embed, route_and_post


class LoggingVoiceCog(commands.Cog, name="LoggingVoice"):
    def __init__(self, bot: commands.Bot, audit: AuditService) -> None:
        self.bot = bot
        self._audit = audit

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild

        # ------------------------------------------------------------
        # Join / leave / switch (DB-only, high volume -> no live post)
        # ------------------------------------------------------------
        if before.channel is None and after.channel is not None:
            await self._audit.log(
                guild.id, member.id, "voice_channel_joined",
                target_type="channel", target_id=after.channel.id,
                detail={"member_id": member.id},
            )
        elif before.channel is not None and after.channel is None:
            await self._audit.log(
                guild.id, member.id, "voice_channel_left",
                target_type="channel", target_id=before.channel.id,
                detail={"member_id": member.id},
            )
        elif (
            before.channel is not None
            and after.channel is not None
            and before.channel.id != after.channel.id
        ):
            await self._audit.log(
                guild.id, member.id, "voice_channel_switched",
                target_type="channel", target_id=after.channel.id,
                detail={"member_id": member.id, "from_channel_id": before.channel.id},
            )

        # ------------------------------------------------------------
        # Server-mute / server-deafen only. self_mute / self_deaf are
        # explicitly ignored — not logged under any circumstances.
        # ------------------------------------------------------------
        if before.mute != after.mute:
            actor_id = self.bot.user.id  # type: ignore[union-attr]
            action = "member_voice_server_muted" if after.mute else "member_voice_server_unmuted"
            await self._audit.log(
                guild.id, actor_id, action,
                target_type="member", target_id=member.id,
            )
            embed = build_embed(action, member=member, actor_id=actor_id)
            await route_and_post(self.bot, guild, action, embed)

        if before.deaf != after.deaf:
            actor_id = self.bot.user.id  # type: ignore[union-attr]
            action = "member_voice_server_deafened" if after.deaf else "member_voice_server_undeafened"
            await self._audit.log(
                guild.id, actor_id, action,
                target_type="member", target_id=member.id,
            )
            embed = build_embed(action, member=member, actor_id=actor_id)
            await route_and_post(self.bot, guild, action, embed)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning("bot.db not set — logging_voice cog not loaded.")
        return
    audit = AuditService(AuditRepository(bot.db))  # type: ignore[attr-defined]
    await bot.add_cog(LoggingVoiceCog(bot, audit))
