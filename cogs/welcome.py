from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.renderer import TemplateRenderer
from database.repositories.audit_repo import AuditRepository
from database.repositories.embed_repo import EmbedRepository
from database.repositories.template_repo import TemplateRepository
from database.repositories.version_repo import VersionRepository
from database.repositories.welcome_repo import WelcomeRepository
from services.audit_service import AuditService
from services.permission_service import PermissionService
from services.template_service import TemplateService
from services.welcome_service import WelcomeService

log = logging.getLogger(__name__)


class WelcomeCog(commands.Cog, name="Welcome"):
    def __init__(
        self,
        bot: commands.Bot,
        service: WelcomeService,
        permission_service: PermissionService,
        audit: AuditService,
    ) -> None:
        self.bot = bot
        self._service = service
        self.permission_service = permission_service
        self._audit = audit

    welcome = app_commands.Group(name="welcome", description="Configure welcome messages.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = await self._service.get_config(member.guild.id)
        if not cfg or not cfg["enabled"] or not cfg["channel_id"]:
            return
        channel = member.guild.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            log.warning("welcome: channel %d not found in guild %d", cfg["channel_id"], member.guild.id)
            return
        try:
            await self._service.send_welcome(channel, member)
        except discord.Forbidden:
            log.error("welcome: missing permission to send in channel %d", cfg["channel_id"])
        except discord.HTTPException as e:
            log.error("welcome: failed to send welcome message: %s", e)

    @welcome.command(name="setchannel", description="Set the channel where welcome messages are posted")
    @app_commands.describe(
        channel="Channel to post welcome messages in",
        test="Send a test welcome message to the channel now (default: off)",
    )
    async def welcome_setchannel(
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

        msg = f"Welcome messages will now post in {channel.mention}."
        if test:
            try:
                await self._service.send_welcome(channel, interaction.user)
                msg += " Test message sent."
            except discord.Forbidden:
                msg += " Could not send test message — missing permission in that channel."
            except discord.HTTPException as e:
                msg += f" Could not send test message — {e}"

        await interaction.followup.send(msg, ephemeral=True)
        log.info("welcome: setchannel — guild=%d channel=%d test=%s by %s", interaction.guild_id, channel.id, test, interaction.user)
        await self._audit.log(
            interaction.guild_id, interaction.user.id, "welcome_setchannel",
            target_type="channel", target_id=channel.id,
            detail={"test": test},
        )

    @welcome.command(name="toggle", description="Enable or disable welcome messages")
    @app_commands.describe(enabled="Whether welcome messages should be sent")
    async def welcome_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not await self.permission_service.is_any_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self._service.set_enabled(interaction.guild_id, enabled)
        state = "enabled" if enabled else "disabled"
        await interaction.followup.send(f"Welcome messages are now {state}.", ephemeral=True)
        await self._audit.log(
            interaction.guild_id, interaction.user.id, "welcome_toggle",
            detail={"enabled": enabled},
        )

    @welcome.command(name="status", description="Show the current welcome message configuration")
    async def welcome_status(self, interaction: discord.Interaction) -> None:
        # Same gate as setchannel/toggle (was ungated; permission review #12).
        if not await self.permission_service.is_any_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        cfg = await self._service.get_config(interaction.guild_id)
        if not cfg or not cfg["channel_id"]:
            await interaction.response.send_message(
                "Welcome messages are not configured. Use `/welcome setchannel` to set them up.", ephemeral=True
            )
            return

        channel_mention = f"<#{cfg['channel_id']}>"
        state = "enabled" if cfg["enabled"] else "disabled"
        embed = discord.Embed(title="Welcome Configuration", color=discord.Color.blurple())
        embed.add_field(name="Status", value=state, inline=True)
        embed.add_field(name="Channel", value=channel_mention, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        log.warning("bot.db not available — welcome cog not loaded.")
        return

    repo = WelcomeRepository(bot.db)
    template_service = TemplateService(
        TemplateRepository(bot.db), EmbedRepository(bot.db),
        VersionRepository(bot.db), AuditRepository(bot.db),
    )
    renderer = TemplateRenderer()
    service = WelcomeService(repo, template_service, renderer)
    permission_service = bot.permission_service  # type: ignore[attr-defined]
    audit = AuditService(AuditRepository(bot.db))

    await bot.add_cog(WelcomeCog(bot, service, permission_service, audit))
