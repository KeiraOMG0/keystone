"""
cogs/advanced_commands.py — Phase 5 slash commands.

    /bot permissions grant/revoke/list
    /schedule add/remove/list/toggle
    /autorole add/remove/list
"""
from __future__ import annotations

import json
import discord
from discord import app_commands
from discord.ext import commands

from services.permission_service import VALID_PERMISSIONS
from ui.paginator import Paginator

# Built from the single source of truth so the slash-command choices can't
# drift from PermissionService (tests/unit/test_permission_list_parity.py).
PERMISSION_CHOICES = [app_commands.Choice(name=p, value=p) for p in sorted(VALID_PERMISSIONS)]


class AdvancedCog(commands.Cog, name="Advanced"):
    def __init__(
        self,
        bot: commands.Bot,
        permission_service,
        scheduler_service,
        autorole_service,
        audit_repo,
    ):
        self.bot = bot
        self.permission_service = permission_service
        self.scheduler_service = scheduler_service
        self.autorole_service = autorole_service
        self._audit = audit_repo

    # ==================================================================
    # /bot permissions
    # ==================================================================

    _bot_group = app_commands.Group(name="bot", description="Bot configuration.")
    _perms_group = app_commands.Group(name="permissions", description="Manage bot permissions.", parent=_bot_group)

    @_perms_group.command(name="grant", description="Grant a permission to a role.")
    @app_commands.describe(
        role="The role to grant the permission to.",
        permission="The permission to grant.",
    )
    @app_commands.choices(permission=PERMISSION_CHOICES)
    async def perms_grant(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        permission: str,
    ) -> None:
        if not self.permission_service.is_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        errors = await self.permission_service.grant(
            interaction.guild_id, role.id, permission, interaction.user.id
        )
        if errors:
            await interaction.response.send_message(errors[0], ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Granted **{permission}** to {role.mention}.", ephemeral=True
            )

    @_perms_group.command(name="revoke", description="Revoke a permission from a role.")
    @app_commands.describe(role="The role.", permission="The permission to revoke.")
    @app_commands.choices(permission=PERMISSION_CHOICES)
    async def perms_revoke(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        permission: str,
    ) -> None:
        if not self.permission_service.is_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        removed = await self.permission_service.revoke(interaction.guild_id, role.id, permission, actor_id=interaction.user.id)
        if removed:
            await interaction.response.send_message(
                f"Revoked **{permission}** from {role.mention}.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{role.mention} did not have **{permission}**.", ephemeral=True
            )

    @_perms_group.command(name="list", description="List all permission grants in this server.")
    async def perms_list(self, interaction: discord.Interaction) -> None:
        if not self.permission_service.is_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        grants = await self.permission_service.list_grants(interaction.guild_id)
        if not grants:
            await interaction.response.send_message(
                "No permission grants configured.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Permission Grants",
            color=discord.Color.blurple(),
            description=f"{len(grants)} grant(s)",
        )
        for g in grants[:25]:
            embed.add_field(
                name=f"**{g['permission']}**",
                value=f"<@&{g['role_id']}> — granted by <@{g['granted_by']}>",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ==================================================================
    # /bot audit
    # ==================================================================

    @_bot_group.command(name="audit", description="View the bot audit log.")
    @app_commands.describe(limit="Number of entries to fetch (max 200, default 50).")
    async def bot_audit(
        self,
        interaction: discord.Interaction,
        limit: int = 50,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "view_audit"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        limit = max(1, min(limit, 200))
        entries = await self._audit.get_recent(interaction.guild_id, limit)

        if not entries:
            await interaction.response.send_message("No audit log entries found.", ephemeral=True)
            return

        def formatter(slice_: list, page: int, max_page: int) -> discord.Embed:
            embed = discord.Embed(
                title="Audit Log",
                color=discord.Color.og_blurple(),
                description=f"Page {page + 1}/{max_page + 1} — {len(entries)} entries",
            )
            for e in slice_:
                detail = ""
                if e.get("detail_json"):
                    try:
                        d = json.loads(e["detail_json"])
                        detail = f"\n`{d}`"
                    except Exception:
                        pass
                embed.add_field(
                    name=f"{e.get('created_at', '?')} — **{e['action']}**",
                    value=(
                        f"By: <@{e['actor_id']}>"
                        + (f" | {e['target_type']} `{e['target_id']}`" if e.get("target_type") else "")
                        + detail
                    ),
                    inline=False,
                )
            return embed

        paginator = Paginator(items=entries, page_size=8, formatter=formatter)
        await interaction.response.send_message(
            embed=paginator.render_embed(), view=paginator, ephemeral=True
        )

    # ==================================================================
    # /schedule
    # ==================================================================

    schedule_group = app_commands.Group(name="schedule", description="Manage scheduled embeds.")

    @schedule_group.command(name="add", description="Schedule a template to post on a cron schedule.")
    @app_commands.describe(
        template="Template name to post.",
        channel="Channel to post in.",
        cron="Cron expression (UTC). E.g. '0 9 * * 1' = every Monday 9am.",
        label="Optional label to identify this schedule.",
    )
    async def schedule_add(
        self,
        interaction: discord.Interaction,
        template: str,
        channel: discord.TextChannel,
        cron: str,
        label: str = None,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_schedules"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        schedule_id, errors = await self.scheduler_service.add(
            guild_id=interaction.guild_id,
            channel_id=channel.id,
            template_name=template,
            cron_expr=cron,
            created_by=interaction.user.id,
            label=label,
        )
        if errors:
            await interaction.followup.send(errors[0], ephemeral=True)
        else:
            await interaction.followup.send(
                f"Schedule `{schedule_id}` created. Template **{template}** will post to {channel.mention} on `{cron}` (UTC).",
                ephemeral=True,
            )

    @schedule_group.command(name="remove", description="Remove a scheduled send.")
    @app_commands.describe(schedule_id="The schedule ID from /schedule list.")
    async def schedule_remove(
        self,
        interaction: discord.Interaction,
        schedule_id: int,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_schedules"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        removed = await self.scheduler_service.remove(
            interaction.guild_id, schedule_id, actor_id=interaction.user.id
        )
        if removed:
            await interaction.response.send_message(f"Schedule `{schedule_id}` removed.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Schedule `{schedule_id}` not found.", ephemeral=True)

    @schedule_group.command(name="toggle", description="Enable or disable a schedule.")
    @app_commands.describe(
        schedule_id="The schedule ID.",
        enabled="True to enable, False to disable.",
    )
    async def schedule_toggle(
        self,
        interaction: discord.Interaction,
        schedule_id: int,
        enabled: bool,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_schedules"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        ok = await self.scheduler_service.toggle(
            interaction.guild_id, schedule_id, enabled, actor_id=interaction.user.id
        )
        if ok:
            state = "enabled" if enabled else "disabled"
            await interaction.response.send_message(f"Schedule `{schedule_id}` {state}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Schedule `{schedule_id}` not found.", ephemeral=True)

    @schedule_group.command(name="list", description="List all scheduled sends in this server.")
    async def schedule_list(self, interaction: discord.Interaction) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_schedules"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        schedules = await self.scheduler_service.list_schedules(interaction.guild_id)
        if not schedules:
            await interaction.response.send_message("No schedules configured.", ephemeral=True)
            return

        def formatter(slice_: list, page: int, max_page: int) -> discord.Embed:
            embed = discord.Embed(
                title="Scheduled Sends",
                color=discord.Color.blurple(),
                description=f"Page {page + 1}/{max_page + 1}",
            )
            for s in slice_:
                state = "on" if s["enabled"] else "off"
                label = s.get("label") or "—"
                next_run = s.get("next_run_at") or "—"
                embed.add_field(
                    name=f"`{s['id']}` {label} [{state}]",
                    value=(
                        f"Template: **{s.get('template_name', '?')}**\n"
                        f"Channel: <#{s['channel_id']}>\n"
                        f"Cron: `{s['cron_expr']}`\n"
                        f"Next run: {next_run} UTC"
                    ),
                    inline=False,
                )
            return embed

        paginator = Paginator(items=schedules, page_size=5, formatter=formatter)
        await interaction.response.send_message(
            embed=paginator.render_embed(), view=paginator, ephemeral=True
        )

    # ==================================================================
    # /autorole
    # ==================================================================

    autorole_group = app_commands.Group(name="autorole", description="Manage automatic role assignment.")

    @autorole_group.command(name="add", description="Add an autorole rule.")
    @app_commands.describe(
        trigger="When to assign the role.",
        role="Role to assign.",
        delay="Delay in seconds before assigning (default 0).",
        trigger_role="For role_add trigger: the role whose addition triggers this rule.",
    )
    @app_commands.choices(trigger=[
        app_commands.Choice(name="join — when a member joins", value="join"),
        app_commands.Choice(name="boost — when a member boosts", value="boost"),
        app_commands.Choice(name="role_add — when a specific role is added", value="role_add"),
    ])
    async def autorole_add(
        self,
        interaction: discord.Interaction,
        trigger: str,
        role: discord.Role,
        delay: int = 0,
        trigger_role: discord.Role = None,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_autorole"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        rule_id, errors = await self.autorole_service.add_rule(
            guild_id=interaction.guild_id,
            trigger_type=trigger,
            role_id=role.id,
            created_by=interaction.user.id,
            delay_seconds=delay,
            trigger_role_id=trigger_role.id if trigger_role else None,
            guild=interaction.guild,
            actor=interaction.user,
        )
        if errors:
            await interaction.response.send_message(errors[0], ephemeral=True)
        else:
            delay_str = f" after {delay}s" if delay else ""
            trigger_str = f" when {trigger_role.mention} is added" if trigger_role else f" on {trigger}"
            await interaction.response.send_message(
                f"Rule `{rule_id}` created: assign {role.mention}{trigger_str}{delay_str}.",
                ephemeral=True,
            )

    @autorole_group.command(name="remove", description="Remove an autorole rule.")
    @app_commands.describe(rule_id="The rule ID from /autorole list.")
    async def autorole_remove(
        self,
        interaction: discord.Interaction,
        rule_id: int,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_autorole"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        removed = await self.autorole_service.remove_rule(
            interaction.guild_id, rule_id, actor_id=interaction.user.id
        )
        if removed:
            await interaction.response.send_message(f"Rule `{rule_id}` removed.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Rule `{rule_id}` not found.", ephemeral=True)

    @autorole_group.command(name="list", description="List all autorole rules in this server.")
    async def autorole_list(self, interaction: discord.Interaction) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_autorole"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        rules = await self.autorole_service.list_rules(interaction.guild_id)
        if not rules:
            await interaction.response.send_message("No autorole rules configured.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Autorole Rules",
            color=discord.Color.blurple(),
            description=f"{len(rules)} rule(s)",
        )
        for r in rules[:25]:
            trigger_str = r["trigger_type"]
            if r.get("trigger_role_id"):
                trigger_str += f" (<@&{r['trigger_role_id']}>)"
            delay_str = f" — {r['delay_seconds']}s delay" if r.get("delay_seconds") else ""
            state = "on" if r["enabled"] else "off"
            embed.add_field(
                name=f"`{r['id']}` [{state}] {trigger_str}",
                value=f"Assign <@&{r['role_id']}>{delay_str}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning("bot.db not available — advanced_commands cog not loaded.")
        return

    from database.repositories.advanced_repo import ScheduleRepository, AutoroleRepository
    from database.repositories.template_repo import TemplateRepository
    from database.repositories.audit_repo import AuditRepository
    from services.audit_service import AuditService
    from services.scheduler_service import SchedulerService
    from services.autorole_service import AutoroleService
    from core.renderer import TemplateRenderer

    schedule_repo = ScheduleRepository(bot.db)
    autorole_repo = AutoroleRepository(bot.db)
    template_repo = TemplateRepository(bot.db)
    audit_repo = AuditRepository(bot.db)
    renderer = TemplateRenderer()

    perm_svc = bot.permission_service  # type: ignore[attr-defined]
    audit_svc = AuditService(audit_repo)
    scheduler_svc = SchedulerService(schedule_repo, template_repo, renderer, bot, audit=audit_svc)
    autorole_svc = AutoroleService(autorole_repo, bot, perm_svc, audit=audit_svc)

    scheduler_svc.start()

    # Attach autorole service to bot so gateway can reach it; both services are
    # also reached by the internal API (dashboard Automation page).
    bot.autorole_service = autorole_svc  # type: ignore[attr-defined]
    bot.scheduler_service = scheduler_svc  # type: ignore[attr-defined]

    await bot.add_cog(AdvancedCog(bot, perm_svc, scheduler_svc, autorole_svc, audit_repo))
