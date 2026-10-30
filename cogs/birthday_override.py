# SPDX-License-Identifier: AGPL-3.0-only
# Python port of BirthdayBot by NoiTheCat (https://github.com/NoiTheCat/BirthdayBot),
# licensed under the GNU AGPL v3.0. See CREDITS.md and LICENSES/AGPL-3.0.txt.
"""
Birthday override cog — /override commands (bot permission `manage_birthdays`;
staff bypass). Requires bot.birthday_core to be set by cogs.birthdays first.
The dashboard Birthdays page does the same through BirthdayService.
"""

import discord
from discord import app_commands
from discord.ext import commands

from core.logging_setup import get_logger

log = get_logger("birthday_override")


class BirthdayOverrideCog(commands.Cog, name="BirthdayOverride"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def core(self):
        return self.bot.birthday_core  # type: ignore[attr-defined]

    override_group = app_commands.Group(
        name="override",
        description="Set birthday options for other users.",
    )

    @override_group.command(name="set-birthday", description="Set or update another member's birthday.")
    @app_commands.describe(
        member="The member to update",
        day="Day of the month (e.g. 14)",
        month="Month name or number (e.g. February or 2)",
        timezone="Their timezone — skipping removes any previously set timezone",
    )
    async def ov_set_birthday(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        day: str,
        month: str,
        timezone: str | None = None,
    ):
        if not await self.core.manage_check(interaction):
            return
        ok, msg = await self.core.svc.set_birthday(
            interaction.guild.id, member.id, day, month, tz_str=timezone, override=True
        )
        if ok:
            await self.core.schedule_guild(interaction.guild.id)
            row = await self.core.svc.get_birthday(interaction.guild.id, member.id) or {}
            await self.core.audit(
                interaction.guild.id, interaction.user.id, "birthday_set", "member", member.id,
                {"month": row.get("birth_month"), "day": row.get("birth_day"),
                 "timezone": row.get("user_timezone"), "source": "discord"},
            )
        display = discord.utils.escape_markdown(member.display_name)
        await interaction.response.send_message(
            f"✓ {display}'s {msg}" if ok else f":x: {msg}",
            ephemeral=not ok,
        )

    @ov_set_birthday.autocomplete("timezone")
    async def ov_tz_ac(self, interaction: discord.Interaction, current: str):
        low = current.lower()
        from zoneinfo import available_timezones
        return [
            app_commands.Choice(name=tz, value=tz)
            for tz in sorted(available_timezones())
            if low in tz.lower()
        ][:25]

    @override_group.command(name="set-timezone", description="Set or update another member's timezone.")
    @app_commands.describe(member="The member to update", timezone="Their timezone")
    async def ov_set_timezone(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        timezone: str,
    ):
        if not await self.core.manage_check(interaction):
            return
        ok, msg = await self.core.svc.set_timezone(
            interaction.guild.id, member.id, timezone, override=True
        )
        if ok:
            await self.core.schedule_guild(interaction.guild.id)
            await self.core.audit(
                interaction.guild.id, interaction.user.id, "birthday_set", "member", member.id,
                {"timezone": timezone, "source": "discord"},
            )
        display = discord.utils.escape_markdown(member.display_name)
        await interaction.response.send_message(
            f"✓ {display}'s {msg}" if ok else f":x: {msg}",
            ephemeral=not ok,
        )

    @ov_set_timezone.autocomplete("timezone")
    async def ov_tz_ac2(self, interaction: discord.Interaction, current: str):
        low = current.lower()
        from zoneinfo import available_timezones
        return [
            app_commands.Choice(name=tz, value=tz)
            for tz in sorted(available_timezones())
            if low in tz.lower()
        ][:25]

    @override_group.command(name="remove-birthday", description="Remove another member's birthday.")
    @app_commands.describe(member="The member whose birthday to remove")
    async def ov_remove_birthday(self, interaction: discord.Interaction, member: discord.Member):
        if not await self.core.manage_check(interaction):
            return
        removed = await self.core.svc.remove_birthday(interaction.guild.id, member.id)
        self.core.cancel_scheduled(interaction.guild.id, member.id)
        if removed:
            await self.core.audit(
                interaction.guild.id, interaction.user.id, "birthday_remove", "member", member.id,
                {"source": "discord"},
            )
        display = discord.utils.escape_markdown(member.display_name)
        if removed:
            await interaction.response.send_message(
                f"✓ {display}'s birthday in this server has been removed.", ephemeral=False
            )
        else:
            await interaction.response.send_message(
                f"✓ {display}'s birthday is not registered.", ephemeral=False
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayOverrideCog(bot))
