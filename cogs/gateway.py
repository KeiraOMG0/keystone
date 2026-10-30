"""
cogs/gateway.py — thin bridge between discord.py gateway events and EventBus.

No business logic lives here. Each listener just calls bus.publish() with
the relevant kwargs and returns. EventService handles the rest.
"""
from __future__ import annotations

import discord
from discord.ext import commands

from core.event_bus import EventBus
from database.repositories.audit_repo import AuditRepository
from services.audit_service import AuditService
from services.log_router import build_embed, route_and_post


class GatewayCog(commands.Cog, name="Gateway"):
    def __init__(self, bot: commands.Bot, bus: EventBus, audit: AuditService) -> None:
        self.bot = bot
        self.bus = bus
        self._audit = audit

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        await self.bus.publish("member_join", member=member)
        autorole = getattr(self.bot, "autorole_service", None)
        if autorole:
            await autorole.fire(member.guild, member, "join")
        await self._audit.log(
            member.guild.id, member.id, "member_joined",
            target_type="member", target_id=member.id,
        )
        embed = build_embed("member_joined", member=member)
        await route_and_post(self.bot, member.guild, "member_joined", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self.bus.publish("member_leave", member=member)
        await self._audit.log(
            member.guild.id, member.id, "member_left",
            target_type="member", target_id=member.id,
        )
        embed = build_embed("member_left", member=member)
        await route_and_post(self.bot, member.guild, "member_left", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        autorole = getattr(self.bot, "autorole_service", None)
        added_roles = set(r.id for r in after.roles) - set(r.id for r in before.roles)
        removed_roles = set(r.id for r in before.roles) - set(r.id for r in after.roles)
        for role_id in added_roles:
            if autorole:
                await autorole.fire(after.guild, after, "role_add", trigger_role_id=role_id)
            await self.bus.publish("role_add", member=after, role_id=role_id)
            await self._audit.log(
                after.guild.id, after.id, "role_added",
                target_type="role", target_id=role_id,
                detail={"member_id": after.id},
            )
        for role_id in removed_roles:
            await self.bus.publish("role_remove", member=after, role_id=role_id)
            await self._audit.log(
                after.guild.id, after.id, "role_removed",
                target_type="role", target_id=role_id,
                detail={"member_id": after.id},
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.type == discord.MessageType.premium_guild_subscription and message.guild:
            await self.bus.publish("boost", member=message.author)
            autorole = getattr(self.bot, "autorole_service", None)
            if autorole:
                await autorole.fire(message.guild, message.author, "boost")
            await self._audit.log(
                message.guild.id, message.author.id, "server_boosted",
                target_type="member", target_id=message.author.id,
            )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author is None or message.author.bot:
            return
        await self._audit.log(
            message.guild.id, message.author.id, "message_deleted",
            target_type="message", target_id=message.id,
            detail={
                "content": (message.content or "")[:500],
                "channel_id": message.channel.id,
                "author_id": message.author.id,
            },
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.author is None or before.author.bot:
            return
        if before.content == after.content:
            return
        await self._audit.log(
            before.guild.id, before.author.id, "message_edited",
            target_type="message", target_id=before.id,
            detail={
                "before": (before.content or "")[:500],
                "after": (after.content or "")[:500],
                "channel_id": before.channel.id,
            },
        )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "event_bus"):
        import logging
        logging.getLogger(__name__).warning("bot.event_bus not set — gateway cog not loaded.")
        return
    audit = AuditService(AuditRepository(bot.db))  # type: ignore[attr-defined]
    await bot.add_cog(GatewayCog(bot, bot.event_bus, audit))
