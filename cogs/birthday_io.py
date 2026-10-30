# SPDX-License-Identifier: AGPL-3.0-only
# Python port of BirthdayBot by NoiTheCat (https://github.com/NoiTheCat/BirthdayBot),
# licensed under the GNU AGPL v3.0. See CREDITS.md and LICENSES/AGPL-3.0.txt.
"""
Birthday import/export cog — /birthdays export / import (bot permission
`manage_birthdays`; staff bypass). Requires bot.birthday_core to be set by
cogs.birthdays first. The file builders and the import parser live in
services/birthday_service.py, shared with the dashboard Birthdays page.
"""

import io

import discord
from discord import app_commands
from discord.ext import commands

from services.birthday_service import IMPORT_MAX_BYTES, build_export
from core.logging_setup import get_logger

log = get_logger("birthday_io")


class BirthdayIOCog(commands.Cog, name="BirthdayIO"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def core(self):
        return self.bot.birthday_core  # type: ignore[attr-defined]

    birthday_io_group = app_commands.Group(
        name="birthdays",
        description="Import or export birthday data.",
    )

    @birthday_io_group.command(
        name="export",
        description="Generate a file with all known birthdays in this server.",
    )
    @app_commands.describe(format="File format to export")
    @app_commands.choices(format=[
        app_commands.Choice(name="Plain text (.txt)", value="plaintext"),
        app_commands.Choice(name="CSV (.csv)", value="csv"),
        app_commands.Choice(name="iCalendar (.ics)", value="ics"),
    ])
    async def cmd_export(self, interaction: discord.Interaction, format: str = "plaintext"):
        if not await self.core.manage_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        rows = await self.core.svc.get_all_for_guild(interaction.guild.id)
        if not rows:
            await interaction.followup.send("No birthdays have been set in this server.")
            return

        member_map: dict[int, discord.Member] = {m.id: m for m in interaction.guild.members}
        filename, content = build_export(
            format if format in ("csv", "ics") else "plaintext",
            interaction.guild.id, interaction.guild.name, rows, member_map,
        )
        file_bytes = io.BytesIO(content.encode("utf-8"))
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_export", None, None,
            {"format": format, "count": len(rows), "source": "discord"},
        )

        await interaction.followup.send(
            f"✓ Exported {len(rows)} birthdays to file.",
            file=discord.File(file_bytes, filename=filename),
        )

    @birthday_io_group.command(
        name="import",
        description="Upload a BirthdayBot export .txt file to import all birthdays.",
    )
    @app_commands.describe(file="The .txt export file from BirthdayBot")
    async def cmd_import(self, interaction: discord.Interaction, file: discord.Attachment):
        if not await self.core.manage_check(interaction):
            return
        if not file.filename.endswith(".txt"):
            await interaction.response.send_message("Please upload a `.txt` file.", ephemeral=True)
            return
        if file.size > IMPORT_MAX_BYTES:
            await interaction.response.send_message("File too large (max 2 MB).", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            raw = await file.read()
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            await interaction.followup.send(f"Failed to read file: {e}")
            return

        imported, skipped = await self.core.svc.import_from_text(interaction.guild.id, text)
        await self.core.schedule_guild(interaction.guild.id)
        scheduled = self.core.scheduled_count(interaction.guild.id)
        await self.core.audit(
            interaction.guild.id, interaction.user.id, "birthday_import", None, None,
            {"imported": imported, "skipped": skipped, "filename": file.filename[:100], "source": "discord"},
        )
        await interaction.followup.send(
            f"✓ Import complete: **{imported}** entries added/updated.\n"
            f"Scheduler refreshed — **{scheduled}** birthday(s) queued in the next 14 days."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayIOCog(bot))
