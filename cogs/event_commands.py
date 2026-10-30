"""
cogs/event_commands.py — /event slash command group.

    /event set   <type> <channel> <template> [send_dm]
    /event clear <type>
    /event list
    /event test  <type>
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from services.event_service import VALID_EVENT_TYPES


_EVENT_CHOICES = [
    app_commands.Choice(name="Member Join", value="member_join"),
    app_commands.Choice(name="Member Leave", value="member_leave"),
    app_commands.Choice(name="Boost", value="boost"),
    app_commands.Choice(name="Role Add", value="role_add"),
    app_commands.Choice(name="Role Remove", value="role_remove"),
]


class EventCommandsCog(commands.Cog, name="Events"):
    def __init__(self, bot: commands.Bot, event_service, permission_service) -> None:
        self.bot = bot
        self.event_service = event_service
        self.permission_service = permission_service

    event = app_commands.Group(name="event", description="Configure event-driven messages.")

    # ------------------------------------------------------------------
    # /event set
    # ------------------------------------------------------------------

    @event.command(name="set", description="Bind a template to a Discord event.")
    @app_commands.describe(
        event_type="Which event triggers this message.",
        channel="Channel where the message is sent.",
        template="Name of the template to send.",
        send_dm="Also DM the member? (default: no)",
    )
    @app_commands.choices(event_type=_EVENT_CHOICES)
    async def event_set(
        self,
        interaction: discord.Interaction,
        event_type: str,
        channel: discord.TextChannel,
        template: str,
        send_dm: bool = False,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_events"):
            await interaction.response.send_message(
                "You don't have permission to configure events.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        errors = await self.event_service.set_binding(
            guild_id=interaction.guild_id,
            event_type=event_type,
            channel_id=channel.id,
            template_name=template,
            actor_id=interaction.user.id,
            send_dm=send_dm,
        )

        if errors:
            await interaction.followup.send(
                "Could not set event binding:\n" + "\n".join(f"• {e}" for e in errors),
                ephemeral=True,
            )
            return

        dm_note = " (+ DM to member)" if send_dm else ""
        await interaction.followup.send(
            f"Event **{event_type}** → {channel.mention} using template **{template}**{dm_note}.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /event clear
    # ------------------------------------------------------------------

    @event.command(name="clear", description="Remove the binding for an event.")
    @app_commands.describe(event_type="Which event to clear.")
    @app_commands.choices(event_type=_EVENT_CHOICES)
    async def event_clear(
        self,
        interaction: discord.Interaction,
        event_type: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_events"):
            await interaction.response.send_message(
                "You don't have permission to configure events.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        deleted = await self.event_service.clear_binding(
            interaction.guild_id, event_type, actor_id=interaction.user.id
        )

        if deleted:
            await interaction.followup.send(
                f"Cleared binding for **{event_type}**.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"No binding found for **{event_type}**.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /event list
    # ------------------------------------------------------------------

    @event.command(name="list", description="Show all configured event bindings.")
    async def event_list(self, interaction: discord.Interaction) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_events"):
            await interaction.response.send_message(
                "You don't have permission to view event bindings.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        bindings = await self.event_service.list_bindings(interaction.guild_id)

        if not bindings:
            await interaction.followup.send(
                "No event bindings configured. Use `/event set` to add one.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="Event Bindings", color=discord.Color.blurple())
        for b in bindings:
            channel = interaction.guild.get_channel(b["channel_id"])
            channel_str = channel.mention if channel else f"#{b['channel_id']} (not found)"
            templates = ", ".join(t["name"] for t in b.get("templates", [])) or "—"
            status = "enabled" if b["enabled"] else "disabled"
            dm_note = " | DM: on" if b["send_dm"] else ""
            embed.add_field(
                name=b["event_type"],
                value=f"Channel: {channel_str}\nTemplate(s): {templates}\nStatus: {status}{dm_note}",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    # /event test
    # ------------------------------------------------------------------

    @event.command(name="test", description="Fire an event with yourself as the member.")
    @app_commands.describe(event_type="Which event to simulate.")
    @app_commands.choices(event_type=_EVENT_CHOICES)
    async def event_test(
        self,
        interaction: discord.Interaction,
        event_type: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_events"):
            await interaction.response.send_message(
                "You don't have permission to test events.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        member = interaction.guild.get_member(interaction.user.id)
        if member is None:
            await interaction.followup.send("Could not resolve your member object.", ephemeral=True)
            return

        sent, errors = await self.event_service.test_event(
            event_type=event_type,
            guild=interaction.guild,
            member=member,
        )

        if errors:
            await interaction.followup.send(
                "\n".join(f"• {e}" for e in errors), ephemeral=True
            )
            return

        if not sent:
            await interaction.followup.send(
                f"No active binding for **{event_type}** or the bound channel/template was not found. Use `/event set` first.",
                ephemeral=True,
            )
            return

        channel_id = await self.event_service.active_channel_id(interaction.guild_id, event_type)
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        await interaction.followup.send(
            f"Test message for **{event_type}** sent to {channel.mention if channel else 'the configured channel'}.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning("bot.db not available — event_commands cog not loaded.")
        return

    from database.repositories.audit_repo import AuditRepository
    from database.repositories.event_repo import EventRepository
    from database.repositories.template_repo import TemplateRepository
    from services.audit_service import AuditService
    from services.event_service import EventService
    from core.renderer import TemplateRenderer

    event_repo = EventRepository(bot.db)
    template_repo = TemplateRepository(bot.db)
    renderer = TemplateRenderer()
    perm = bot.permission_service  # type: ignore[attr-defined]

    svc = EventService(
        event_repo=event_repo,
        template_repo=template_repo,
        renderer=renderer,
        bus=bot.event_bus,
        bot=bot,
        audit=AuditService(AuditRepository(bot.db)),
    )
    # Also reached by the internal API (dashboard Automation > Events).
    bot.event_service = svc  # type: ignore[attr-defined]

    await bot.add_cog(EventCommandsCog(bot, svc, perm))
