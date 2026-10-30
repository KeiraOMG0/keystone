"""
cogs/honeypot.py — honeypot ("do not type") trap channel.

Slash commands (group /honeypot, gated on the `ban_members` grant / staff):
    /honeypot setup   — create the category + channel and start catching
    /honeypot status  — config + totals
    /honeypot stats   — the stats embed
    /honeypot action  — ban | softban | kick
    /honeypot enable / disable
    /honeypot appeals — list pending ban appeals
    /honeypot scramble — rename channel + category to gibberish now, or turn the
                         automatic scramble on/off and set its interval

All logic lives in services/honeypot_service.py; this cog only wires the
listeners, the scramble scheduler (`scramble_check`, every few minutes; it only
acts when scrambling is on and the interval has passed, timed from
`last_scrambled_at` so restarts don't reset it) and formats replies. The internal API reaches the same service via
`bot.get_cog("Honeypot").honeypot_service` (internal_api.deps.get_honeypot_service).
"""
from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.repositories.honeypot_repo import HoneypotRepository
from services.honeypot_service import (
    SCRAMBLE_MAX_HOURS, SCRAMBLE_MIN_HOURS, HoneypotError, HoneypotService, scramble_error_text,
)

log = logging.getLogger(__name__)

_NO_PERMISSION = "You don't have permission to manage the honeypot."
SCRAMBLE_CHECK_MINUTES = 5


def _code(text: str | None) -> str:
    """A channel name shown inline, safe from markdown and mentions."""
    return "`" + (text or "?").replace("`", "'") + "`"


class Honeypot(commands.Cog):
    def __init__(self, bot: commands.Bot, honeypot_service: HoneypotService) -> None:
        self.bot = bot
        self.honeypot_service = honeypot_service

    async def cog_load(self) -> None:
        self.scramble_check.start()

    async def cog_unload(self) -> None:
        self.scramble_check.cancel()
        await self.honeypot_service.close()

    @tasks.loop(minutes=SCRAMBLE_CHECK_MINUTES)
    async def scramble_check(self) -> None:
        for guild in list(self.bot.guilds):
            await self.honeypot_service.scramble_if_due(guild)  # never raises

    @scramble_check.before_loop
    async def _before_scramble_check(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Listeners — both are no-raise by construction (service catches everything).
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Bot/app/webhook messages are passed on too: the service catches the
        # member behind an app reply and removes webhook/bot posts in the trap.
        if message.guild is None:
            return
        await self.honeypot_service.handle_message(message, self.bot.permission_service)  # type: ignore[attr-defined]

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.honeypot_service.handle_channel_deleted(channel)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _allowed(self, interaction: discord.Interaction, permission: str = "ban_members") -> bool:
        """Mutations need ban_members; read-only views (status/stats/appeals) pass
        moderate_members, the same gates as the dashboard's honeypot routes."""
        if interaction.guild is None:
            return False
        if await self.bot.permission_service.has_permission(interaction.user, permission):  # type: ignore[attr-defined]
            return True
        await interaction.response.send_message(_NO_PERMISSION, ephemeral=True)
        return False

    @staticmethod
    def _config_lines(cfg: dict) -> str:
        channel = f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "not set up"
        if cfg["configured"] and not cfg["channel_exists"]:
            channel += " (**deleted** - run `/honeypot setup` again)"
        log_ch = f"<#{cfg['log_channel_id']}>" if cfg["log_channel_id"] else "moderation-log channel"
        if cfg.get("scramble_enabled"):
            nxt = cfg.get("next_scramble_at")
            when = (f"<t:{int(datetime.fromisoformat(nxt).timestamp())}:R>" if nxt
                    else f"within {SCRAMBLE_CHECK_MINUTES} minutes")
            scramble = f"every {cfg['scramble_interval_hours']}h (next {when})"
        else:
            scramble = "off"
        return (
            f"Channel: {channel}\n"
            f"Enabled: {'yes' if cfg['enabled'] else 'no'}\n"
            f"Action: **{cfg['action']}**\n"
            f"DM before acting: {'yes' if cfg['dm_before'] else 'no'}\n"
            f"Appeals: {'on' if cfg['appeal_enabled'] else 'off'}\n"
            f"Delete message history: {cfg['delete_message_days']} day(s)\n"
            f"Catch log: {log_ch}\n"
            f"Name scrambling: {scramble}"
        )

    async def _respond(self, interaction: discord.Interaction, text: str | None = None, embed: discord.Embed | None = None) -> None:
        await interaction.followup.send(content=text, embed=embed, ephemeral=True)

    async def _change(self, interaction: discord.Interaction, **changes) -> None:
        if not await self._allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            cfg = await self.honeypot_service.update_config(interaction.guild, changes)
        except HoneypotError as exc:
            await self._respond(interaction, f"Couldn't update: {exc.detail}")
            return
        await self._respond(interaction, "Updated.\n" + self._config_lines(cfg))

    # ------------------------------------------------------------------
    # /honeypot group
    # ------------------------------------------------------------------
    honeypot = app_commands.Group(
        name="honeypot", description="Manage the honeypot trap channel.", guild_only=True,
    )

    @honeypot.command(name="setup", description="Create the honeypot category + channel and start catching.")
    @app_commands.describe(
        category_name="Name of the new category (default: Do Not Type).",
        channel_name="Name of the trap channel (default: do-not-type).",
        action="What to do to anyone who posts there (default: ban).",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Ban", value="ban"),
        app_commands.Choice(name="Softban (ban + unban, wipes messages)", value="softban"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def hp_setup(
        self,
        interaction: discord.Interaction,
        category_name: str | None = None,
        channel_name: str | None = None,
        action: str | None = None,
    ) -> None:
        if not await self._allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            cfg, warnings = await self.honeypot_service.setup(
                interaction.guild, interaction.user.id,
                category_name=category_name, channel_name=channel_name, action=action,
            )
        except HoneypotError as exc:
            msg = ("The honeypot is already set up: " if exc.code == "already_configured" else "Setup failed: ")
            await self._respond(interaction, msg + (exc.detail if exc.code != "already_configured" else "use `/honeypot status`."))
            return
        text = f"Honeypot created in <#{cfg['channel_id']}> and enabled.\n" + self._config_lines(cfg)
        if warnings:
            text += "\n\n**Warnings**\n" + "\n".join(f"- {w}" for w in warnings)
        await self._respond(interaction, text)

    @honeypot.command(name="status", description="Show the honeypot configuration and totals.")
    async def hp_status(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction, "moderate_members"):
            return
        await interaction.response.defer(ephemeral=True)
        cfg = await self.honeypot_service.get_config(interaction.guild.id)
        stats = await self.honeypot_service.get_stats(interaction.guild.id)
        t = stats["totals"]
        embed = discord.Embed(title="Honeypot status", description=self._config_lines(cfg), color=discord.Color.dark_gold())
        embed.add_field(
            name="Caught",
            value=f"All time: **{t['all']}**\nLast 24h: {t['last_24h']}\nLast 7d: {t['last_7d']}\nLast 30d: {t['last_30d']}",
            inline=False,
        )
        await self._respond(interaction, embed=embed)

    @honeypot.command(name="stats", description="Show detailed honeypot statistics.")
    async def hp_stats(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction, "moderate_members"):
            return
        await interaction.response.defer(ephemeral=True)
        s = await self.honeypot_service.get_stats(interaction.guild.id)

        def pct(v) -> str:
            return "n/a" if v is None else f"{v}%"

        embed = discord.Embed(title="Honeypot statistics", color=discord.Color.dark_gold())
        t = s["totals"]
        embed.add_field(name="Caught", value=f"All: **{t['all']}** | 24h: {t['last_24h']} | 7d: {t['last_7d']} | 30d: {t['last_30d']}", inline=False)
        a = s["by_action"]
        r = s["by_result"]
        embed.add_field(name="Actions", value=f"ban {a['ban']} / softban {a['softban']} / kick {a['kick']}", inline=True)
        embed.add_field(name="Results", value=f"ok {r['ok']} / partial {r['partial']} / failed {r['failed']}", inline=True)
        age = s["account_age"]
        embed.add_field(
            name="Account age at catch",
            value=f"median {age['median_days'] if age['median_days'] is not None else 'n/a'}d | under 7d: {pct(age['under_7d_pct'])} | under 30d: {pct(age['under_30d_pct'])}",
            inline=False,
        )
        ttc = s["time_to_catch"]
        embed.add_field(
            name="Time from join to catch",
            value=f"median {ttc['median_seconds'] if ttc['median_seconds'] is not None else 'n/a'}s | under 60s: {pct(ttc['under_60s_pct'])} | under 1h: {pct(ttc['under_1h_pct'])}",
            inline=False,
        )
        sg = s["signals"]
        embed.add_field(
            name="Signals",
            value=(f"default avatar {pct(sg['default_avatar_pct'])}, links {pct(sg['link_pct'])}, invites {pct(sg['invite_pct'])}, "
                   f"@everyone {pct(sg['everyone_pct'])}, attachments {pct(sg['attachment_pct'])}, DM delivered {pct(sg['dm_delivered_pct'])}"),
            inline=False,
        )
        ap = s["appeals"]
        embed.add_field(
            name="Appeals",
            value=f"pending {ap['pending']} | approved {ap['approved']} | denied {ap['denied']} | false positives {pct(ap['false_positive_pct'])}",
            inline=False,
        )
        if s["top_messages"]:
            embed.add_field(
                name="Top repeated messages",
                value="\n".join(f"{m['count']}x `{m['preview'].replace('`', chr(39))[:80]}`" for m in s["top_messages"]),
                inline=False,
            )
        await self._respond(interaction, embed=embed)

    @honeypot.command(name="action", description="Set what happens to anyone who posts in the honeypot.")
    @app_commands.choices(action=[
        app_commands.Choice(name="Ban", value="ban"),
        app_commands.Choice(name="Softban (ban + unban, wipes messages)", value="softban"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    async def hp_action(self, interaction: discord.Interaction, action: str) -> None:
        await self._change(interaction, action=action)

    @honeypot.command(name="enable", description="Turn the honeypot on.")
    async def hp_enable(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, enabled=True)

    @honeypot.command(name="disable", description="Turn the honeypot off (nobody gets punished while off).")
    async def hp_disable(self, interaction: discord.Interaction) -> None:
        await self._change(interaction, enabled=False)

    @honeypot.command(name="scramble", description="Rename the honeypot channel + category to gibberish, or set the timer.")
    @app_commands.describe(
        mode="Scramble now (default), or turn automatic scrambling on/off.",
        every_hours=f"Hours between automatic scrambles ({SCRAMBLE_MIN_HOURS}-{SCRAMBLE_MAX_HOURS}).",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Scramble now", value="now"),
        app_commands.Choice(name="Turn automatic scrambling on", value="on"),
        app_commands.Choice(name="Turn automatic scrambling off", value="off"),
    ])
    async def hp_scramble(
        self,
        interaction: discord.Interaction,
        mode: str = "now",
        every_hours: app_commands.Range[int, SCRAMBLE_MIN_HOURS, SCRAMBLE_MAX_HOURS] | None = None,
    ) -> None:
        if not await self._allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        changes: dict = {}
        if mode in ("on", "off"):
            changes["scramble_enabled"] = mode == "on"
        if every_hours is not None:
            changes["scramble_interval_hours"] = every_hours
        lines: list[str] = []
        try:
            if changes:
                await self.honeypot_service.update_config(interaction.guild, changes)
            if mode == "now":
                res = await self.honeypot_service.scramble_names(
                    interaction.guild, actor_id=interaction.user.id, trigger="manual", timeout=60,
                )
                lines.append(f"Channel: {_code(res['channel']['old'])} -> {_code(res['channel']['new'])}")
                if res["category"]:
                    lines.append(f"Category: {_code(res['category']['old'])} -> {_code(res['category']['new'])}")
                lines += [f"- {w}" for w in res["warnings"]]
        except HoneypotError as exc:
            await self._respond(interaction, f"Couldn't do that: {scramble_error_text(exc)}")
            return
        cfg = await self.honeypot_service.get_config(interaction.guild.id)
        lines.append(self._config_lines(cfg).splitlines()[-1])
        await self._respond(interaction, "\n".join(lines))

    @honeypot.command(name="appeals", description="List pending ban appeals.")
    async def hp_appeals(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction, "moderate_members"):
            return
        await interaction.response.defer(ephemeral=True)
        pending = await self.honeypot_service.list_appeals(interaction.guild.id, "pending")
        if not pending:
            await self._respond(interaction, "No pending appeals.")
            return
        lines = []
        for ap in pending[:15]:
            msg = (ap["message"] or "").replace("\n", " ")[:120]
            lines.append(f"**#{ap['id']}** <@{ap['user_id']}> ({ap['name']}): {msg or '(no message)'}")
        extra = f"\n...and {len(pending) - 15} more." if len(pending) > 15 else ""
        await self._respond(
            interaction,
            "**Pending appeals** (review them on the dashboard)\n" + "\n".join(lines) + extra,
        )


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        log.warning("bot.db not available - honeypot cog not loaded.")
        return

    from database.repositories.audit_repo import AuditRepository
    from database.repositories.mod_action_repo import ModActionRepository
    from services.audit_service import AuditService
    from services.moderation_service import ModerationService

    # Reuse the ModerationService owned by cogs/moderation.py (load order puts it first);
    # fall back to an equivalent instance so honeypot bans still land in mod_actions.
    audit = AuditService(AuditRepository(bot.db))
    mod_cog = bot.get_cog("ModerationCog")
    moderation_service = getattr(mod_cog, "moderation_service", None)
    if moderation_service is None:
        moderation_service = ModerationService(ModActionRepository(bot.db), audit)

    service = HoneypotService(bot, HoneypotRepository(bot.db), moderation_service, audit=audit)
    # Migration 030 stores only SHA-256 hashes of appeal-link tokens; convert older rows
    # before the cog (and with it the internal API's lookups) is available.
    await service.hash_legacy_tokens()
    await bot.add_cog(Honeypot(bot, service))
