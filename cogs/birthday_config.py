# SPDX-License-Identifier: AGPL-3.0-only
# Python port of BirthdayBot by NoiTheCat (https://github.com/NoiTheCat/BirthdayBot),
# licensed under the GNU AGPL v3.0. See CREDITS.md and LICENSES/AGPL-3.0.txt.
"""
Birthday config cog — /config commands (bot permission `manage_birthdays`;
staff bypass). Requires bot.birthday_core to be set by cogs.birthdays first.
The dashboard Birthdays page (/dashboard/birthdays) edits the same settings
through the same BirthdayService / BirthdayCore and the same checks.
"""

import discord
from discord import app_commands, ui
from discord.ext import commands

from core.logging_setup import get_logger
from services.birthday_service import (
    ANNOUNCE_MESSAGE_MAX,
    birthday_role_block_reason,
    channel_status,
    everyone_block_reason,
    hardcoded_mentions,
    role_status,
)

log = get_logger("birthday_config")

# send_test_announcement error code -> the reply /config announce test gives.
TEST_ERRORS = {
    "channel_not_set": ":x: Unable to send a birthday message. The announcement channel is not configured.",
    "channel_missing": "The configured channel no longer exists.",
}


class AnnounceMessageModal(ui.Modal, title="Edit announcement message"):
    single = ui.TextInput(
        label="Single - Message for one birthday",
        style=discord.TextStyle.paragraph,
        placeholder="e.g.\n🎂 happy birthday {mention}!\n✦ today we celebrate {user}\n✦ wishing you a lovely day",
        required=False,
        max_length=ANNOUNCE_MESSAGE_MAX,
    )
    plural = ui.TextInput(
        label="Multi - Message for multiple birthdays",
        style=discord.TextStyle.paragraph,
        placeholder="Leave blank to fall back to the single message\nUse {mention} for pings, {user} for display names",
        required=False,
        max_length=ANNOUNCE_MESSAGE_MAX,
    )

    def __init__(
        self,
        core,
        guild_id: int,
        existing_single: str | None,
        existing_plural: str | None,
    ):
        super().__init__()
        self._core = core
        self._svc = core.svc
        self._guild_id = guild_id
        self._existing_single = existing_single
        self._existing_plural = existing_plural
        if existing_single:
            self.single.default = existing_single
        if existing_plural:
            self.plural.default = existing_plural

    async def on_submit(self, interaction: discord.Interaction):
        single_val = self.single.value.strip() or None
        plural_val = self.plural.value.strip() or None

        hardcoded_hits = hardcoded_mentions(single_val, plural_val)

        # %e (@everyone) may only be saved by full staff (see everyone_block_reason).
        blocked = everyone_block_reason(
            self._existing_single, self._existing_plural, single_val, plural_val,
            self._core.bot.permission_service.is_staff(interaction.user),
        )
        if blocked:
            await interaction.response.send_message(f":x: {blocked}", ephemeral=True)
            return

        log.info(
            "Announcement template edited: guild=%s editor_id=%s editor_name=%s "
            "old_single=%r new_single=%r old_plural=%r new_plural=%r hardcoded_mentions=%s",
            self._guild_id, interaction.user.id, str(interaction.user),
            self._existing_single, single_val, self._existing_plural, plural_val,
            hardcoded_hits,
        )

        await self._svc.set_announce_messages(self._guild_id, single_val, plural_val)
        await self._core.audit(
            self._guild_id, interaction.user.id, "birthday_config_update", None, None,
            {"fields": ["announce_message", "announce_message_pl"], "source": "discord"},
        )

        msg = "✓ Announcement messages have been updated."
        if hardcoded_hits:
            msg += (
                f"\n⚠ Warning: found a hardcoded mention ({', '.join(hardcoded_hits)}) in your message. "
                "Use `{mention}` instead so it pings the actual birthday person, not a fixed user."
            )
        await interaction.response.send_message(msg, ephemeral=True)


class BirthdayConfigCog(commands.Cog, name="BirthdayConfig"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def core(self):
        return self.bot.birthday_core  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    # /config                                                              #
    # ------------------------------------------------------------------ #

    config_group = app_commands.Group(
        name="config",
        description="Configure birthday settings for this server.",
    )

    @config_group.command(name="check", description="Report the bot's current birthday configuration.")
    async def cfg_check(self, interaction: discord.Interaction):
        if not await self.core.manage_check(interaction):
            return
        settings = await self.core.svc.get_settings(interaction.guild.id)
        role_id = settings.get("birthday_role_id")
        ch_id = settings.get("announce_channel_id")
        scheduled = self.core.scheduled_count(interaction.guild.id)
        all_bdays = await self.core.svc.get_all_for_guild(interaction.guild.id)

        # Role permission check
        rstat = role_status(interaction.guild, role_id)
        if rstat["exists"]:
            role_val = f"<@&{role_id}> {'✓' if rstat['usable'] else '⚠ bot cannot assign'}"
        else:
            role_val = "Not set"

        # Channel permission check
        cstat = channel_status(interaction.guild, ch_id)
        if cstat["exists"]:
            ch_val = f"<#{ch_id}> {'✓' if cstat['can_send'] else '⚠ missing Send Messages'}"
        else:
            ch_val = "Not set"

        guild_tz = settings.get("guild_timezone") or "UTC"

        embed = discord.Embed(title="Birthday Configuration", color=discord.Color.pink())
        embed.add_field(name="Birthdays registered", value=str(len(all_bdays)), inline=True)
        embed.add_field(name="Scheduled (next 14d)", value=str(scheduled), inline=True)
        embed.add_field(name="Server Timezone", value=guild_tz, inline=True)
        embed.add_field(name="Birthday Role", value=role_val, inline=True)
        embed.add_field(name="Announce Channel", value=ch_val, inline=True)
        embed.add_field(name="Ping in announcement", value="Mention" if settings.get("announce_ping") else "Display name", inline=True)
        embed.add_field(name="Ping Birthday User", value="Yes" if settings.get("ping_user") else "No", inline=True)
        embed.add_field(name="Add-Only Mode", value="Yes" if settings.get("add_only") else "No", inline=True)
        embed.add_field(name="Private Confirms", value="Yes" if settings.get("private_confirms") else "No", inline=True)

        single_msg = settings.get("announce_message") or "_default_"
        plural_msg = settings.get("announce_message_pl") or "_falls back to single_"
        embed.add_field(name="Announce Message (single)", value=single_msg[:1024], inline=False)
        embed.add_field(name="Announce Message (plural)", value=plural_msg[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config_group.command(name="add-only", description="Restrict users from editing their birthday after adding it.")
    @app_commands.describe(enabled="True to lock birthdays after first set, False to allow edits")
    async def cfg_add_only(self, interaction: discord.Interaction, enabled: bool):
        if not await self.core.manage_check(interaction):
            return
        await self.core.svc.set_add_only(interaction.guild.id, enabled)
        log.info(
            "add-only changed: guild=%s editor_id=%s editor_name=%s enabled=%s",
            interaction.guild.id, interaction.user.id, str(interaction.user), enabled,
        )
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_config_update", None, None,
            {"fields": ["add_only"], "add_only": enabled, "source": "discord"},
        )
        await interaction.response.send_message(
            f"✓ Add-only mode has been **{'enabled' if enabled else 'disabled'}**.", ephemeral=False
        )

    @config_group.command(name="private-confirms", description="Make confirmation messages visible only to the user.")
    @app_commands.describe(enabled="True to make confirmations ephemeral")
    async def cfg_private_confirms(self, interaction: discord.Interaction, enabled: bool):
        if not await self.core.manage_check(interaction):
            return
        await self.core.svc.set_private_confirms(interaction.guild.id, enabled)
        log.info(
            "private-confirms changed: guild=%s editor_id=%s editor_name=%s enabled=%s",
            interaction.guild.id, interaction.user.id, str(interaction.user), enabled,
        )
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_config_update", None, None,
            {"fields": ["private_confirms"], "private_confirms": enabled, "source": "discord"},
        )
        await interaction.response.send_message(
            f"✓ Private confirmation messages **{'enabled' if enabled else 'disabled'}**.", ephemeral=False
        )

    @config_group.command(name="birthday-role", description="Set the role given to users having a birthday.")
    @app_commands.describe(role="Role to assign on birthdays — leave blank to clear")
    async def cfg_birthday_role(self, interaction: discord.Interaction, role: discord.Role | None = None):
        if not await self.core.manage_check(interaction):
            return
        if role:
            # Same rules as the dashboard: the birthday role is handed to anyone
            # who registers today's date, so it follows the self-role policy.
            reason = await birthday_role_block_reason(
                role, interaction.guild, interaction.user, self.bot.permission_service
            )
            if reason:
                await interaction.response.send_message(reason, ephemeral=True)
                return
        await self.core.svc.set_birthday_role(interaction.guild.id, role.id if role else None)
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_config_update", "role", role.id if role else None,
            {"fields": ["birthday_role_id"], "birthday_role_id": role.id if role else None, "source": "discord"},
        )
        log.info(
            "birthday-role changed: guild=%s editor_id=%s editor_name=%s role_id=%s role_name=%s",
            interaction.guild.id, interaction.user.id, str(interaction.user),
            role.id if role else None, role.name if role else None,
        )
        msg = f"✓ The birthday role has been set to **{role.name}**." if role else "✓ The birthday role has been unset."
        await interaction.response.send_message(msg, ephemeral=False)

    @config_group.command(name="set-timezone", description="Set the server's default timezone.")
    @app_commands.describe(timezone="Default timezone for members who haven't set their own — leave blank to unset")
    async def cfg_timezone(self, interaction: discord.Interaction, timezone: str | None = None):
        if not await self.core.manage_check(interaction):
            return
        if timezone:
            err = await self.core.svc.set_guild_timezone(interaction.guild.id, timezone)
            if err:
                await interaction.response.send_message(f":x: {err}", ephemeral=True)
                return
            await self.core.schedule_guild(interaction.guild.id)
            log.info(
                "guild timezone changed: guild=%s editor_id=%s editor_name=%s timezone=%s",
                interaction.guild.id, interaction.user.id, str(interaction.user), timezone,
            )
            await self.core.audit(
                interaction.guild.id, interaction.user.id, "birthday_config_update", None, None,
                {"fields": ["guild_timezone"], "guild_timezone": timezone, "source": "discord"},
            )
            await interaction.response.send_message(
                f"✓ The server's time zone has been set to **{timezone}**.", ephemeral=False
            )
        else:
            await self.core.svc.set_guild_timezone(interaction.guild.id, None)
            await self.core.schedule_guild(interaction.guild.id)
            log.info(
                "guild timezone unset: guild=%s editor_id=%s editor_name=%s",
                interaction.guild.id, interaction.user.id, str(interaction.user),
            )
            await self.core.audit(
                interaction.guild.id, interaction.user.id, "birthday_config_update", None, None,
                {"fields": ["guild_timezone"], "guild_timezone": None, "source": "discord"},
            )
            await interaction.response.send_message("✓ The server's time zone has been unset.", ephemeral=False)

    @cfg_timezone.autocomplete("timezone")
    async def cfg_tz_ac(self, interaction: discord.Interaction, current: str):
        low = current.lower()
        from zoneinfo import available_timezones
        return [
            app_commands.Choice(name=tz, value=tz)
            for tz in sorted(available_timezones())
            if low in tz.lower()
        ][:25]

    # /config announce subgroup
    announce_group = app_commands.Group(
        name="announce",
        description="Settings regarding birthday announcements.",
        parent=config_group,
    )

    @announce_group.command(name="help", description="Show how announcement configuration works.")
    async def ann_help(self, interaction: discord.Interaction):
        if not await self.core.manage_check(interaction):
            return
        embed = discord.Embed(
            title="Announcement Configuration",
            color=discord.Color.pink(),
            description=(
                "Use `/config announce set-channel` to set where announcements are sent.\n"
                "Use `/config announce set-message` to open an editor — supports newlines and bullet characters.\n"
                "Use `/config announce set-ping` to toggle whether `{mention}` pings or shows a display name.\n"
                "Use `/config announce ping-user` to send a separate @mention notification to the birthday person.\n"
                "Use `/config announce test` to preview the announcement.\n"
                "Use `/config announce timers-reset` to re-trigger today's birthdays.\n\n"
                "**Placeholders:**\n"
                "`{mention}` — @mention the birthday person (pings them)\n"
                "`{user}` — display name (no ping)\n"
                "`{username}` — username\n"
                "`{server}` — server name\n"
                "`{membercount}` — number of members\n"
                "`%e` — @everyone ping (only full staff can save a message containing it)\n\n"
                "**Tips:**\n"
                "• Use newlines in the editor for multi-line / bulleted messages\n"
                "• Use `{user}` in your styled message + enable *Ping Birthday User* to notify them separately"
            ),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @announce_group.command(name="set-channel", description="Set or clear the announcement channel.")
    @app_commands.describe(channel="Channel to send birthday announcements — leave blank to clear")
    async def ann_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ):
        if not await self.core.manage_check(interaction):
            return
        await self.core.svc.set_announce_channel(interaction.guild.id, channel.id if channel else None)
        log.info(
            "announce channel changed: guild=%s editor_id=%s editor_name=%s channel_id=%s channel_name=%s",
            interaction.guild.id, interaction.user.id, str(interaction.user),
            channel.id if channel else None, channel.name if channel else None,
        )
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_config_update", "channel", channel.id if channel else None,
            {"fields": ["announce_channel_id"], "announce_channel_id": channel.id if channel else None, "source": "discord"},
        )
        msg = f"✓ The announcement channel has been set to **{channel.name}**." if channel else "✓ The announcement channel has been unset."
        await interaction.response.send_message(msg, ephemeral=False)

    @announce_group.command(name="set-message", description="Edit the announcement message in a text editor. Use {mention}, {user}, etc.")
    async def ann_message(self, interaction: discord.Interaction):
        if not await self.core.manage_check(interaction):
            return
        settings = await self.core.svc.get_settings(interaction.guild.id)
        await interaction.response.send_modal(
            AnnounceMessageModal(
                self.core,
                interaction.guild.id,
                settings.get("announce_message"),
                settings.get("announce_message_pl"),
            )
        )

    @announce_group.command(name="set-ping", description="Toggle whether {mention} in announcements @mentions or shows display name.")
    @app_commands.describe(enabled="True to ping users via {mention}, False to use display name")
    async def ann_ping(self, interaction: discord.Interaction, enabled: bool):
        if not await self.core.manage_check(interaction):
            return
        await self.core.svc.set_announce_ping(interaction.guild.id, enabled)
        log.info(
            "announce-ping changed: guild=%s editor_id=%s editor_name=%s enabled=%s",
            interaction.guild.id, interaction.user.id, str(interaction.user), enabled,
        )
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_config_update", None, None,
            {"fields": ["announce_ping"], "announce_ping": enabled, "source": "discord"},
        )
        await interaction.response.send_message(
            f"✓ Ping members in announcements: **{'enabled' if enabled else 'disabled'}**.", ephemeral=False
        )

    @announce_group.command(name="ping-user", description="Send a separate @mention ping to the birthday person before the announcement.")
    @app_commands.describe(enabled="True to send a separate ping so the birthday person gets notified")
    async def ann_ping_user(self, interaction: discord.Interaction, enabled: bool):
        if not await self.core.manage_check(interaction):
            return
        await self.core.svc.set_ping_user(interaction.guild.id, enabled)
        log.info(
            "ping-user changed: guild=%s editor_id=%s editor_name=%s enabled=%s",
            interaction.guild.id, interaction.user.id, str(interaction.user), enabled,
        )
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_config_update", None, None,
            {"fields": ["ping_user"], "ping_user": enabled, "source": "discord"},
        )
        await interaction.response.send_message(
            f"✓ Birthday user ping **{'enabled' if enabled else 'disabled'}**.", ephemeral=False
        )

    @announce_group.command(name="test", description="Send a test birthday announcement.")
    @app_commands.describe(
        member="Member to use as the birthday person in the test",
        member2="Optional second member",
        member3="Optional third member",
        member4="Optional fourth member",
        member5="Optional fifth member",
    )
    async def ann_test(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        member2: discord.Member | None = None,
        member3: discord.Member | None = None,
        member4: discord.Member | None = None,
        member5: discord.Member | None = None,
    ):
        if not await self.core.manage_check(interaction):
            return
        members = [m for m in [member, member2, member3, member4, member5] if m is not None]
        log.info(
            "announce test triggered: guild=%s editor_id=%s editor_name=%s member_ids=%s",
            interaction.guild.id, interaction.user.id, str(interaction.user),
            [m.id for m in members],
        )
        settings = await self.core.svc.get_settings(interaction.guild.id)
        channel = interaction.guild.get_channel(settings.get("announce_channel_id") or 0)
        error = await self.core.send_test_announcement(interaction.guild, members)
        if error == "channel_no_permission":
            await interaction.response.send_message(
                f":x: Unable to send a birthday message. Insufficient permissions to send to {channel.mention}.", ephemeral=True
            )
            return
        if error:
            await interaction.response.send_message(TEST_ERRORS.get(error, f":x: {error}"), ephemeral=True)
            return
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_test_announcement", "channel", channel.id,
            {"user_ids": [str(m.id) for m in members], "source": "discord"},
        )
        await interaction.response.send_message(
            f"✓ An announcement test will be sent to {channel.mention}.", ephemeral=True
        )

    @announce_group.command(name="timers-reset", description="Reset internal timers to re-trigger today's birthday announcements.")
    async def ann_timers_reset(self, interaction: discord.Interaction):
        if not await self.core.manage_check(interaction):
            return
        log.info(
            "timers-reset triggered: guild=%s editor_id=%s editor_name=%s",
            interaction.guild.id, interaction.user.id, str(interaction.user),
        )
        count = await self.core.reset_timers(interaction.guild.id)
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_timers_reset", None, None,
            {"reset": count, "source": "discord"},
        )
        await interaction.response.send_message(
            "✓ Internal timers for all users have been reset. If there are birthdays, it may take a moment before a new announcement is made.",
            ephemeral=False,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayConfigCog(bot))
