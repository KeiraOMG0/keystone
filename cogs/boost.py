from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.renderer import TemplateRenderer
from database.repositories.audit_repo import AuditRepository
from database.repositories.boost_repo import BoostRepository
from database.repositories.embed_repo import EmbedRepository
from database.repositories.template_repo import TemplateRepository
from database.repositories.version_repo import VersionRepository
from services.audit_service import AuditService
from services.boost_service import BoostService
from services.permission_service import PermissionService
from services.template_service import TemplateService

log = logging.getLogger(__name__)


class BoostCog(commands.Cog, name="Boost"):
    def __init__(
        self,
        bot: commands.Bot,
        service: BoostService,
        permission_service: PermissionService,
        audit: AuditService,
    ) -> None:
        self.bot = bot
        self._service = service
        self.permission_service = permission_service
        self._audit = audit

    boost = app_commands.Group(name="boost", description="Configure boost announcement messages.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild or message.type != discord.MessageType.premium_guild_subscription:
            return
        cfg = await self._service.get_config(message.guild.id)
        if not cfg or not cfg["enabled"] or not cfg["channel_id"]:
            return
        channel = message.guild.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            log.warning("boost: channel %d not found in guild %d", cfg["channel_id"], message.guild.id)
            return
        try:
            await self._service.send_boost(channel, message.author)
        except discord.Forbidden:
            log.error("boost: missing permission to send in channel %d", cfg["channel_id"])
        except discord.HTTPException as e:
            log.error("boost: failed to send boost message: %s", e)

    @boost.command(name="setchannel", description="Set the channel where boost announcements are posted")
    @app_commands.describe(
        channel="Channel to post boost announcements in",
        test="Send a test boost message to the channel now (default: off)",
    )
    async def boost_setchannel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        test: bool = False,
    ) -> None:
        if not await self.permission_service.is_any_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._service.set_channel(interaction.guild_id, channel.id)

        msg = f"Boost announcements will now post in {channel.mention}."
        if test:
            try:
                await self._service.send_boost(channel, interaction.user)
                msg += " Test message sent."
            except discord.Forbidden:
                msg += " Could not send test message — missing permission in that channel."
            except discord.HTTPException as e:
                msg += f" Could not send test message — {e}"

        await interaction.followup.send(msg, ephemeral=True)
        log.info("boost: setchannel — guild=%d channel=%d test=%s by %s", interaction.guild_id, channel.id, test, interaction.user)
        await self._audit.log(
            interaction.guild_id, interaction.user.id, "boost_setchannel",
            target_type="channel", target_id=channel.id,
            detail={"test": test},
        )

    @boost.command(name="toggle", description="Enable or disable boost announcements")
    @app_commands.describe(enabled="Whether boost announcements should be sent")
    async def boost_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not await self.permission_service.is_any_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._service.set_enabled(interaction.guild_id, enabled)
        state = "enabled" if enabled else "disabled"
        await interaction.followup.send(f"Boost announcements are now {state}.", ephemeral=True)
        await self._audit.log(
            interaction.guild_id, interaction.user.id, "boost_toggle",
            detail={"enabled": enabled},
        )

    @boost.command(name="status", description="Show the current boost announcement configuration")
    async def boost_status(self, interaction: discord.Interaction) -> None:
        # Same gate as setchannel/toggle (was ungated; permission review #12).
        if not await self.permission_service.is_any_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        cfg = await self._service.get_config(interaction.guild_id)
        if not cfg or not cfg["channel_id"]:
            await interaction.response.send_message(
                "Boost announcements are not configured. Use `/boost setchannel` to set them up.", ephemeral=True
            )
            return

        channel_mention = f"<#{cfg['channel_id']}>"
        state = "enabled" if cfg["enabled"] else "disabled"
        embed = discord.Embed(title="Boost Configuration", color=discord.Color.blurple())
        embed.add_field(name="Status", value=state, inline=True)
        embed.add_field(name="Channel", value=channel_mention, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        log.warning("bot.db not available — boost cog not loaded.")
        return

    repo = BoostRepository(bot.db)
    template_service = TemplateService(
        TemplateRepository(bot.db), EmbedRepository(bot.db),
        VersionRepository(bot.db), AuditRepository(bot.db),
    )
    renderer = TemplateRenderer()
    service = BoostService(repo, template_service, renderer)
    permission_service = bot.permission_service  # type: ignore[attr-defined]
    audit = AuditService(AuditRepository(bot.db))

    await bot.add_cog(BoostCog(bot, service, permission_service, audit))
