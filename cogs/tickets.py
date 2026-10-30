"""
cogs/tickets.py — Ticket system slash commands and persistent Views.

Slash commands:
    /ticket setup   — create a new ticket panel
    /ticket post    — post the panel button in its channel
    /ticket list    — list all panels in this server
    /ticket delete  — delete a panel and its message

Persistent views (survive bot restarts):
    TicketPanelView         — the "Open Ticket" button posted in a channel
    TicketControlView       — close / claim buttons inside a ticket channel
    TicketVerifyControlView — verify & close / claim / close buttons for verify-purpose tickets

Close is staff only and deletes the channel 30 s later (TicketService.close_ticket);
there is no separate Archive button. Background loops: finish_closes_loop (restart
recovery for that delete), stale_sweep_loop (weekly auto-close of idle tickets),
purge_archived_tickets_loop (daily purge of old archived records).
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config as _config
import services.settings_service as settings_module

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistent Views
# ---------------------------------------------------------------------------

async def _staff_close(ticket_id: int, interaction: discord.Interaction) -> None:
    """Shared Close button callback. Staff only (manage_tickets): closing
    deletes the channel, so the opener cannot close their own ticket."""
    cog: TicketCog | None = interaction.client.get_cog("Tickets")  # type: ignore[assignment]
    if cog is None:
        await interaction.response.send_message("Ticket system unavailable.", ephemeral=True)
        return

    if not await cog.permission_service.has_permission(interaction.user, "manage_tickets"):
        await interaction.response.send_message("Only staff can close tickets.", ephemeral=True)
        return

    await interaction.response.defer()
    errors = await cog.ticket_service.close_ticket(ticket_id, interaction.guild, interaction.user)
    if errors:
        await interaction.followup.send(errors[0], ephemeral=True)


class TicketPanelView(discord.ui.View):
    """The panel button posted in a channel. Survives restarts via custom_id."""

    def __init__(self, panel_id: int, button_label: str = "Open Ticket", button_emoji: str | None = None):
        super().__init__(timeout=None)
        emoji = discord.PartialEmoji.from_str(button_emoji) if button_emoji else None
        btn = discord.ui.Button(
            label=button_label,
            emoji=emoji,
            style=discord.ButtonStyle.primary,
            custom_id=f"ticket_open:{panel_id}",
        )
        btn.callback = self._open_callback
        self.add_item(btn)
        self._panel_id = panel_id

    async def _open_callback(self, interaction: discord.Interaction) -> None:
        cog: TicketCog | None = interaction.client.get_cog("Tickets")  # type: ignore[assignment]
        if cog is None:
            await interaction.response.send_message("Ticket system unavailable.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        channel, errors = await cog.ticket_service.open_ticket(
            self._panel_id, interaction.guild, interaction.user
        )
        if errors:
            await interaction.followup.send(errors[0], ephemeral=True)
        else:
            await interaction.followup.send(
                f"Your ticket has been opened: {channel.mention}", ephemeral=True
            )


class TicketControlView(discord.ui.View):
    """Close / Claim buttons inside an open ticket channel.

    `claimed_by_label` is set once a ticket has been claimed (see
    TicketService._disable_claim_button, which rebuilds this view with it
    populated and edits the control message in place) — it swaps the Claim
    button for a disabled "Claimed by @X" indicator so re-clicking is not
    possible from a UI that's actually current. The server-side guard in
    TicketService.claim_ticket is still the real enforcement — this is
    belt-and-suspenders against a stale cached button on a client that
    hasn't refreshed.
    """

    def __init__(self, ticket_id: int, claimed_by_label: str | None = None):
        super().__init__(timeout=None)
        self._ticket_id = ticket_id

        close_btn = discord.ui.Button(
            label="Close",
            style=discord.ButtonStyle.danger,
            custom_id=f"ticket_close:{ticket_id}",
            emoji="🔒",
        )
        close_btn.callback = self._close_callback
        self.add_item(close_btn)

        if claimed_by_label:
            claim_btn = discord.ui.Button(
                label=f"Claimed by {claimed_by_label}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"ticket_claim:{ticket_id}",
                emoji="✋",
                disabled=True,
            )
        else:
            claim_btn = discord.ui.Button(
                label="Claim",
                style=discord.ButtonStyle.secondary,
                custom_id=f"ticket_claim:{ticket_id}",
                emoji="✋",
            )
        claim_btn.callback = self._claim_callback
        self.add_item(claim_btn)

    async def _close_callback(self, interaction: discord.Interaction) -> None:
        await _staff_close(self._ticket_id, interaction)

    async def _claim_callback(self, interaction: discord.Interaction) -> None:
        cog: TicketCog | None = interaction.client.get_cog("Tickets")  # type: ignore[assignment]
        if cog is None:
            await interaction.response.send_message("Ticket system unavailable.", ephemeral=True)
            return

        # Same permission gate used to manage tickets everywhere else
        # (dashboard actions, /ticket setup, etc.) — previously this callback
        # had NO permission check at all, so anyone (including the ticket's
        # own opener) could claim.
        if not await cog.permission_service.has_permission(interaction.user, "manage_tickets"):
            await interaction.response.send_message(
                "You don't have permission to claim tickets.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        errors = await cog.ticket_service.claim_ticket(
            self._ticket_id, interaction.guild, interaction.user
        )
        if errors:
            await interaction.followup.send(errors[0], ephemeral=True)
        else:
            await interaction.followup.send("Ticket claimed.", ephemeral=True)


class TicketVerifyControlView(discord.ui.View):
    """Verify & Close / Claim / Close buttons inside a verify-purpose ticket channel.

    Posted instead of TicketControlView when the ticket's panel has
    purpose == "verify" (see TicketService.open_ticket).
    """

    def __init__(self, ticket_id: int, claimed_by_label: str | None = None):
        super().__init__(timeout=None)
        self._ticket_id = ticket_id

        verify_close_btn = discord.ui.Button(
            label="Verify & Close",
            style=discord.ButtonStyle.success,
            custom_id=f"ticket_verify_close:{ticket_id}",
            emoji="✅",
        )
        verify_close_btn.callback = self._verify_close_callback
        self.add_item(verify_close_btn)

        if claimed_by_label:
            claim_btn = discord.ui.Button(
                label=f"Claimed by {claimed_by_label}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"ticket_claim:{ticket_id}",
                emoji="✋",
                disabled=True,
            )
        else:
            claim_btn = discord.ui.Button(
                label="Claim",
                style=discord.ButtonStyle.secondary,
                custom_id=f"ticket_claim:{ticket_id}",
                emoji="✋",
            )
        claim_btn.callback = self._claim_callback
        self.add_item(claim_btn)

        close_btn = discord.ui.Button(
            label="Close",
            style=discord.ButtonStyle.danger,
            custom_id=f"ticket_close:{ticket_id}",
            emoji="🔒",
        )
        close_btn.callback = self._close_callback
        self.add_item(close_btn)

    async def _close_callback(self, interaction: discord.Interaction) -> None:
        await _staff_close(self._ticket_id, interaction)

    async def _claim_callback(self, interaction: discord.Interaction) -> None:
        cog: TicketCog | None = interaction.client.get_cog("Tickets")  # type: ignore[assignment]
        if cog is None:
            await interaction.response.send_message("Ticket system unavailable.", ephemeral=True)
            return

        # Same permission gate as TicketControlView._claim_callback — previously
        # missing here too, so anyone could claim a verify-purpose ticket.
        if not await cog.permission_service.has_permission(interaction.user, "manage_tickets"):
            await interaction.response.send_message(
                "You don't have permission to claim tickets.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        errors = await cog.ticket_service.claim_ticket(
            self._ticket_id, interaction.guild, interaction.user
        )
        if errors:
            await interaction.followup.send(errors[0], ephemeral=True)
        else:
            await interaction.followup.send("Ticket claimed.", ephemeral=True)

    async def _verify_close_callback(self, interaction: discord.Interaction) -> None:
        cog: TicketCog | None = interaction.client.get_cog("Tickets")  # type: ignore[assignment]
        if cog is None:
            await interaction.response.send_message("Ticket system unavailable.", ephemeral=True)
            return

        # Staff-only — the ticket opener clicking this must not be able to verify themselves.
        if not await cog.permission_service.has_permission(interaction.user, "verify_members"):
            await interaction.response.send_message(
                "You don't have permission to verify members.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        ticket = await cog.ticket_service.get_ticket(self._ticket_id)
        if not ticket or ticket["guild_id"] != interaction.guild.id:
            await interaction.followup.send("Ticket not found.", ephemeral=True)
            return

        member = interaction.guild.get_member(ticket["opened_by"])
        if member is None:
            try:
                member = await interaction.guild.fetch_member(ticket["opened_by"])
            except discord.HTTPException:
                member = None
        if member is None:
            await interaction.followup.send(
                "Could not find the member who opened this ticket (they may have left the server).",
                ephemeral=True,
            )
            return

        from cogs.verify import verify_member
        from database.repositories.template_repo import TemplateRepository
        from database.repositories.embed_repo import EmbedRepository
        from database.repositories.version_repo import VersionRepository
        from database.repositories.audit_repo import AuditRepository
        from services.template_service import TemplateService
        from services.audit_service import AuditService
        from core.renderer import TemplateRenderer

        bot = interaction.client
        template_service = TemplateService(
            TemplateRepository(bot.db), EmbedRepository(bot.db),
            VersionRepository(bot.db), AuditRepository(bot.db),
        )
        renderer = TemplateRenderer()
        audit = AuditService(AuditRepository(bot.db))

        result = await verify_member(
            member, interaction.user, interaction.channel, template_service, renderer, audit, source="button"
        )

        if not result.ok:
            if result.already_verified:
                summary = f"{member.mention} was already verified. Closing ticket anyway."
            else:
                summary = f"Verification failed: {result.error}"
                await interaction.followup.send(summary, ephemeral=True)
                return
        else:
            summary = f"Verified {member.mention} — {result.message_status}."

        close_errors = await cog.ticket_service.close_ticket(
            self._ticket_id, interaction.guild, interaction.user
        )
        if close_errors:
            summary += f"\nTicket close failed: {close_errors[0]}"
        else:
            # close_ticket posts the "deleted in 30 seconds" notice in the
            # channel and deletes it afterwards (restart-safe, see
            # TicketService.finish_overdue_closes).
            summary += f"\nTicket closed. The channel will be deleted in {cog.ticket_service.close_delete_delay} seconds."

        await interaction.followup.send(summary, ephemeral=True)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class TicketCog(commands.Cog, name="Tickets"):
    def __init__(self, bot: commands.Bot, ticket_service, permission_service, audit=None, settings_service=None):
        self.bot = bot
        self.ticket_service = ticket_service
        self.permission_service = permission_service
        self._audit = audit
        # Dashboard-editable settings bridge (services/settings_service.py) —
        # None-safe fallback to plain config.py/.env values if not passed
        # (e.g. a test harness constructing TicketCog directly), matching
        # this module's existing "never crash if an optional dep is
        # missing" convention elsewhere.
        self._settings = settings_service
        self.purge_archived_tickets_loop.start()
        self.finish_closes_loop.start()
        self.stale_sweep_loop.start()

    def cog_unload(self):
        self.purge_archived_tickets_loop.cancel()
        self.finish_closes_loop.cancel()
        self.stale_sweep_loop.cancel()

    async def _ticket_chat_logging_enabled(self, guild_id: int) -> bool:
        """Live-reloadable via the Settings dashboard page — see
        services/settings_service.py's module docstring for the TTL-cache
        design. Falls back to config.TICKET_CHAT_LOGGING_ENABLED (the .env
        default) if no SettingsService was wired up."""
        if self._settings is None:
            return _config.TICKET_CHAT_LOGGING_ENABLED
        cached = settings_module.get_cached("ticket_chat_logging_enabled")
        if cached is not None:
            return cached
        effective = await self._settings.get_effective(guild_id)
        value = effective["ticket_chat_logging_enabled"]
        settings_module.set_cached("ticket_chat_logging_enabled", value)
        return value

    async def _ticket_archive_purge_days(self, guild_id: int) -> int:
        """Same live-reload bridge as _ticket_chat_logging_enabled above."""
        if self._settings is None:
            return _config.TICKET_ARCHIVE_PURGE_DAYS
        cached = settings_module.get_cached("ticket_archive_purge_days")
        if cached is not None:
            return cached
        effective = await self._settings.get_effective(guild_id)
        value = effective["ticket_archive_purge_days"]
        settings_module.set_cached("ticket_archive_purge_days", value)
        return value

    # ------------------------------------------------------------------
    # Chat-log capture — plain text only, opt-in via the "Enable ticket
    # chat logging" dashboard setting (default False, config.
    # TICKET_CHAT_LOGGING_ENABLED as the .env-seeded fallback). Attachments
    # and voice messages are never captured here or anywhere else in this
    # cog.
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not await self._ticket_chat_logging_enabled(message.guild.id):
            return
        if message.type != discord.MessageType.default and message.type != discord.MessageType.reply:
            return  # skip system messages (pins, joins, etc.)

        ticket = await self.ticket_service.get_ticket_by_channel(message.channel.id)
        if not ticket or ticket["status"] != "open":
            return

        content = message.content or ""
        if not content:
            # Attachment-only / embed-only messages: explicitly out of scope
            # (images/voice capture is still pending a staff decision) — skip
            # rather than logging an empty row.
            return

        try:
            await self.ticket_service.record_ticket_message(ticket["id"], message.author.id, content)
        except Exception:
            log.exception("ticket chat-log: failed to record message for ticket %d", ticket["id"])

    # ------------------------------------------------------------------
    # Auto-archive tickets belonging to a member who leaves or is kicked —
    # nobody's left to answer in a channel gated to them.
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        try:
            archived_ids = await self.ticket_service.archive_tickets_for_departed_member(
                member.guild, member.id
            )
        except Exception:
            log.exception("ticket auto-archive: failed for departed member %d", member.id)
            return

        if archived_ids and self._audit:
            await self._audit.log(
                member.guild.id, member.id, "ticket_auto_archived",
                target_type="member", target_id=member.id,
                detail={"ticket_ids": archived_ids},
            )

    # ------------------------------------------------------------------
    # Restart-safe channel delete after Close. close_ticket starts an
    # in-process 30 s timer; this catches tickets whose timer was lost to a
    # crash or restart (first run is right after startup).
    # ------------------------------------------------------------------

    @tasks.loop(minutes=2)
    async def finish_closes_loop(self):
        for guild in self.bot.guilds:
            try:
                done = await self.ticket_service.finish_overdue_closes(guild)
                if done:
                    log.info("Ticket close recovery: deleted channels of closed ticket(s) %s", done)
            except Exception:
                log.exception("Ticket close recovery failed for guild %d", guild.id)

    @finish_closes_loop.before_loop
    async def before_finish_closes(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Weekly auto-close of open tickets idle for 7+ days. Checked hourly;
    # TicketService.run_weekly_stale_sweep only acts when the last sweep
    # (an audit_log row) is a week old, so restarts never reset the week.
    # ------------------------------------------------------------------

    @tasks.loop(hours=1)
    async def stale_sweep_loop(self):
        for guild in self.bot.guilds:
            try:
                done = await self.ticket_service.run_weekly_stale_sweep(guild)
                if done:
                    log.info("Ticket stale sweep: closed idle ticket(s) %s", done)
            except Exception:
                log.exception("Ticket stale sweep failed for guild %d", guild.id)

    @stale_sweep_loop.before_loop
    async def before_stale_sweep(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # 30-day auto-purge of archived tickets
    # ------------------------------------------------------------------

    @tasks.loop(hours=24)
    async def purge_archived_tickets_loop(self):
        try:
            # purge_old_archived_tickets has no guild filter (single global
            # purge across all tracked tickets), so there's no per-message
            # guild to key the live setting lookup off like on_message has —
            # use the bot's first cached guild (single-guild deployment in
            # practice; see services/settings_service.py's module docstring).
            # Falls back to the plain config.py default if the bot somehow
            # isn't in any guild yet.
            guild_id = self.bot.guilds[0].id if self.bot.guilds else None
            raw_days = (
                await self._ticket_archive_purge_days(guild_id)
                if guild_id is not None
                else _config.TICKET_ARCHIVE_PURGE_DAYS
            )
            # Second line of defence behind the Settings page's validation:
            # a value < 1 or not a whole number would purge EVERY archived
            # ticket, so skip the run instead.
            from services.ticket_service import parse_purge_days
            purge_days = parse_purge_days(raw_days)
            if purge_days is None:
                log.warning(
                    "Ticket auto-purge skipped: ticket_archive_purge_days=%r is not a whole number >= 1",
                    raw_days,
                )
                return
            deleted_ids = await self.ticket_service.purge_old_archived_tickets(purge_days)
            if deleted_ids:
                log.info(
                    "Ticket auto-purge: deleted %d archived ticket(s) older than %d day(s): %s",
                    len(deleted_ids), purge_days, deleted_ids,
                )
        except Exception:
            log.exception("Ticket auto-purge failed")

    @purge_archived_tickets_loop.before_loop
    async def before_purge_archived_tickets(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # /ticket group
    # ------------------------------------------------------------------

    ticket = app_commands.Group(name="ticket", description="Manage ticket panels.")

    @ticket.command(name="setup", description="Create a new ticket panel.")
    @app_commands.describe(
        channel="Channel to post the ticket button in.",
        open_category="Category for new ticket channels.",
        transcript_channel="Channel to post transcripts in (optional).",
        template="Template to display above the button (optional).",
        max_open="Max open tickets per user (default 1).",
        naming="Channel naming pattern, use {number} and/or {username} (default: ticket-{username}).",
        button_label="Label on the open button (default: Open Ticket).",
        button_emoji="Emoji on the open button (optional, e.g. 🎫).",
        purpose="Panel purpose — Verify panels get a Verify & Close button (default: Support).",
    )
    @app_commands.choices(purpose=[
        app_commands.Choice(name="Support", value="support"),
        app_commands.Choice(name="Verify", value="verify"),
    ])
    async def ticket_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        open_category: discord.CategoryChannel = None,
        transcript_channel: discord.TextChannel = None,
        template: str = None,
        max_open: int = 1,
        naming: str = "ticket-{username}",
        button_label: str = "Open Ticket",
        button_emoji: str = None,
        purpose: str = "support",
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_tickets"):
            await interaction.response.send_message(
                "You don't have permission to manage tickets.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        panel_id, errors = await self.ticket_service.create_panel(
            guild=interaction.guild,
            channel=channel,
            actor_id=interaction.user.id,
            open_category=open_category,
            transcript_channel=transcript_channel,
            template_name=template,
            max_open_per_user=max_open,
            naming_pattern=naming,
            button_label=button_label,
            button_emoji=button_emoji,
            purpose=purpose,
        )

        if errors:
            await interaction.followup.send(
                "Failed:\n" + "\n".join(f"• {e}" for e in errors), ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Panel created (ID: `{panel_id}`). "
            f"Run `/ticket post panel_id:{panel_id}` to post it in {channel.mention}.",
            ephemeral=True,
        )

    @ticket.command(name="post", description="Post a ticket panel button in its channel.")
    @app_commands.describe(panel_id="The panel ID from /ticket setup.")
    async def ticket_post(
        self,
        interaction: discord.Interaction,
        panel_id: int,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_tickets"):
            await interaction.response.send_message(
                "You don't have permission to manage tickets.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        message_id, errors = await self.ticket_service.post_panel(panel_id, interaction.guild)

        if errors:
            await interaction.followup.send(
                "Failed:\n" + "\n".join(f"• {e}" for e in errors), ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Panel posted! (message ID: `{message_id}`)", ephemeral=True
        )

    @ticket.command(name="list", description="List all ticket panels in this server.")
    async def ticket_list(self, interaction: discord.Interaction) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_tickets"):
            await interaction.response.send_message(
                "You don't have permission to view ticket panels.", ephemeral=True
            )
            return

        panels = await self.ticket_service.list_panels(interaction.guild_id)

        if not panels:
            await interaction.response.send_message(
                "No ticket panels configured.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Ticket Panels",
            color=discord.Color.blurple(),
            description=f"{len(panels)} panel(s)",
        )
        for p in panels[:25]:
            embed.add_field(
                name=f"Panel `{p['id']}` — <#{p['channel_id']}>",
                value=(
                    f"Pattern: `{p['naming_pattern']}`\n"
                    f"Max open/user: {p['max_open_per_user']}\n"
                    f"Posted: {'yes' if p.get('message_id') else 'no'}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ticket.command(name="delete", description="Delete a ticket panel and its posted message.")
    @app_commands.describe(panel_id="The panel ID to delete.")
    async def ticket_delete(
        self,
        interaction: discord.Interaction,
        panel_id: int,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_tickets"):
            await interaction.response.send_message(
                "You don't have permission to manage tickets.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        errors = await self.ticket_service.delete_panel(panel_id, interaction.guild)

        if errors:
            await interaction.followup.send(errors[0], ephemeral=True)
        else:
            await interaction.followup.send(f"Panel `{panel_id}` deleted.", ephemeral=True)

    # ------------------------------------------------------------------
    # /ticket role subgroup
    # ------------------------------------------------------------------

    role = app_commands.Group(name="role", parent=ticket, description="Manage support roles for a ticket panel.")

    @role.command(name="add", description="Grant a role access to tickets opened via a panel.")
    @app_commands.describe(panel_id="The panel ID from /ticket setup.", role="Role to grant ticket access to.")
    async def ticket_role_add(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        role: discord.Role,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_tickets"):
            await interaction.response.send_message(
                "You don't have permission to manage tickets.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        errors = await self.ticket_service.add_panel_role(panel_id, interaction.guild, role)

        if errors:
            await interaction.followup.send(errors[0], ephemeral=True)
            return

        await interaction.followup.send(
            f"{role.mention} will now be granted access to tickets opened via panel `{panel_id}`.",
            ephemeral=True,
        )
        if self._audit:
            await self._audit.log(
                interaction.guild_id, interaction.user.id, "ticket_role_add",
                target_type="panel", target_id=panel_id,
                detail={"role_id": role.id},
            )

    @role.command(name="remove", description="Revoke a role's access to tickets opened via a panel.")
    @app_commands.describe(panel_id="The panel ID from /ticket setup.", role="Role to revoke ticket access from.")
    async def ticket_role_remove(
        self,
        interaction: discord.Interaction,
        panel_id: int,
        role: discord.Role,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_tickets"):
            await interaction.response.send_message(
                "You don't have permission to manage tickets.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        errors = await self.ticket_service.remove_panel_role(panel_id, interaction.guild, role)

        if errors:
            await interaction.followup.send(errors[0], ephemeral=True)
            return

        await interaction.followup.send(
            f"{role.mention} no longer has automatic access to tickets opened via panel `{panel_id}`.",
            ephemeral=True,
        )
        if self._audit:
            await self._audit.log(
                interaction.guild_id, interaction.user.id, "ticket_role_remove",
                target_type="panel", target_id=panel_id,
                detail={"role_id": role.id},
            )

    @role.command(name="list", description="List support roles configured for a ticket panel.")
    @app_commands.describe(panel_id="The panel ID from /ticket setup.")
    async def ticket_role_list(
        self,
        interaction: discord.Interaction,
        panel_id: int,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_tickets"):
            await interaction.response.send_message(
                "You don't have permission to view ticket panels.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        roles, errors = await self.ticket_service.get_panel_roles(panel_id, interaction.guild)

        if errors:
            await interaction.followup.send(errors[0], ephemeral=True)
            return

        if not roles:
            await interaction.followup.send(
                f"No support roles configured for panel `{panel_id}`.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"Support Roles — Panel `{panel_id}`",
            color=discord.Color.blurple(),
            description="\n".join(f"• <@&{r['role_id']}>" for r in roles),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning("bot.db not available — tickets cog not loaded.")
        return

    from database.repositories.ticket_repo import TicketRepository
    from database.repositories.template_repo import TemplateRepository
    from database.repositories.audit_repo import AuditRepository
    from database.repositories.settings_repo import SettingsRepository
    from services.ticket_service import TicketService
    from services.audit_service import AuditService
    from services.settings_service import SettingsService
    from core.renderer import TemplateRenderer

    ticket_repo = TicketRepository(bot.db)
    template_repo = TemplateRepository(bot.db)
    renderer = TemplateRenderer()
    perm = bot.permission_service  # type: ignore[attr-defined]
    audit = AuditService(AuditRepository(bot.db))
    settings_service = SettingsService(SettingsRepository(bot.db))

    svc = TicketService(ticket_repo, template_repo, renderer, bot, audit=audit)

    cog = TicketCog(bot, svc, perm, audit, settings_service)
    await bot.add_cog(cog)

    # Restore persistent views for all posted panels so buttons work after restart
    try:
        panels = await ticket_repo.get_all_panels()
        for panel in panels:
            bot.add_view(
                TicketPanelView(panel["id"], panel["button_label"], panel.get("button_emoji")),
                message_id=panel["message_id"],
            )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Failed to restore ticket panel views")

    # Restore persistent views for open tickets (Close/Claim, or Verify & Close/Close for
    # verify-purpose panels) so their buttons keep working after a bot restart. These
    # views are keyed purely by custom_id (no message_id), matching how they're built
    # live in TicketService.open_ticket. list_open_tickets joins in the owning panel's
    # purpose so we know which control view a given ticket needs without an extra
    # per-ticket panel lookup. Closed tickets have no buttons (their channel is deleted
    # 30 s after close; finish_closes_loop covers a restart inside that window).
    try:
        for guild in bot.guilds:
            open_tickets = await ticket_repo.list_open_tickets(guild.id)
            for t in open_tickets:
                claimed_by_label = None
                if t.get("claimed_by"):
                    claimer = guild.get_member(t["claimed_by"])
                    claimed_by_label = claimer.display_name if claimer else str(t["claimed_by"])
                if t.get("panel_purpose") == "verify":
                    bot.add_view(TicketVerifyControlView(t["id"], claimed_by_label=claimed_by_label))
                else:
                    bot.add_view(TicketControlView(t["id"], claimed_by_label=claimed_by_label))

    except Exception:
        import logging
        logging.getLogger(__name__).exception("Failed to restore ticket control views")
