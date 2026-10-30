"""
cogs/managed_commands.py — Managed message slash commands.

Provides:
    /managed list                    — list all managed messages in this server
    /managed untrack [message_id]    — stop tracking a managed message (does not delete the message)
    /managed sync [name]             — force re-sync a template's managed messages now

Note: /template post lives in template_commands.py (TemplateCog) to avoid
registering the 'template' group twice.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class ManagedCog(commands.Cog, name="ManagedMessages"):
    def __init__(
        self,
        bot: commands.Bot,
        managed_service,
        permission_service,
        sync_queue,
        template_repo,
    ):
        self.bot = bot
        self.managed_service = managed_service
        self.permission_service = permission_service
        self.sync_queue = sync_queue
        self._template_repo = template_repo

    # ------------------------------------------------------------------
    # /managed group
    # ------------------------------------------------------------------

    managed = app_commands.Group(
        name="managed",
        description="Manage tracked messages.",
    )

    @managed.command(name="list", description="List all managed messages in this server.")
    async def managed_list(self, interaction: discord.Interaction) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to view managed messages.", ephemeral=True
            )
            return

        records = await self.managed_service.list_managed(interaction.guild_id)

        if not records:
            await interaction.response.send_message(
                "No managed messages in this server.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Managed Messages",
            color=discord.Color.blurple(),
            description=f"{len(records)} tracked message(s)",
        )
        for r in records[:25]:
            status = "failed" if r.get("sync_failed") else "ok"
            synced = r.get("last_synced_at") or "never"
            embed.add_field(
                name=f"**{r.get('template_name', '?')}** — <#{r['channel_id']}>",
                value=(
                    f"Message: `{r['message_id']}`\n"
                    f"Last synced: {synced} | Status: {status}"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @managed.command(name="untrack", description="Stop tracking a managed message (does not delete it).")
    @app_commands.describe(message_id="The message ID to stop tracking.")
    async def managed_untrack(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to manage tracked messages.", ephemeral=True
            )
            return

        try:
            mid = int(message_id)
        except ValueError:
            await interaction.response.send_message(
                "Invalid message ID.", ephemeral=True
            )
            return

        removed = await self.managed_service.untrack(interaction.guild_id, mid, actor_id=interaction.user.id)
        if removed:
            await interaction.response.send_message(
                f"Stopped tracking message `{mid}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"No managed message with ID `{mid}` found in this server.", ephemeral=True
            )

    @managed.command(name="sync", description="Force re-sync all managed messages for a template now.")
    @app_commands.describe(name="Name of the template to sync.")
    async def managed_sync(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to sync managed messages.", ephemeral=True
            )
            return

        template = await self._template_repo.get_by_name(interaction.guild_id, name)
        if not template:
            await interaction.response.send_message(
                f"No template named **{name}**.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.managed_service.sync_now(interaction.guild_id, template["id"], actor_id=interaction.user.id)
        await interaction.followup.send(
            f"Synced all managed messages for **{name}**.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Cog setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning(
            "bot.db is not available — managed_commands cog not loaded."
        )
        return

    from database.repositories.managed_repo import ManagedMessageRepository
    from database.repositories.template_repo import TemplateRepository
    from database.repositories.audit_repo import AuditRepository
    from services.audit_service import AuditService
    from services.managed_message_service import ManagedMessageService
    from core.renderer import TemplateRenderer
    from core.sync_queue import SyncQueue

    managed_repo = ManagedMessageRepository(bot.db)
    template_repo = TemplateRepository(bot.db)
    renderer = TemplateRenderer()
    perm = bot.permission_service  # type: ignore[attr-defined]

    svc = ManagedMessageService(managed_repo, template_repo, renderer, bot, audit=AuditService(AuditRepository(bot.db)))

    # Start the sync queue and attach it to bot for cross-cog access
    sq = SyncQueue()
    sq.set_handler(svc.sync_template)
    sq.start()
    bot.sync_queue = sq  # type: ignore[attr-defined]

    # Wire managed_service into TemplateCog so /template post is available
    template_cog = bot.get_cog("Templates")
    if template_cog is not None:
        template_cog.managed_service = svc

    await bot.add_cog(ManagedCog(bot, svc, perm, sq, template_repo))
