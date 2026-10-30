"""
core/bot_factory.py - builds the commands.Bot used by main.py, with the
bot-wide safety defaults (unit-tested in tests/unit/test_bot_safety.py).

* DEFAULT_ALLOWED_MENTIONS: @everyone/@here never ping unless a send passes
  its own allowed_mentions. User, role and reply pings stay on, so features
  that ping a member or a configured role keep working. Sends that echo user
  text or third-party titles (music) additionally pass
  AllowedMentions.none().
* GuildOnlyCommandTree: every slash command is guild-only. Synced commands are
  registered with contexts=[guild] (hidden in DMs), and interaction_check
  refuses any app-command interaction without a guild (stale clients, or
  before the next sync) with an ephemeral reply instead of the
  AttributeError a cog would hit on interaction.guild being None.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

PREFIX = "!"

DEFAULT_ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=True, users=True, replied_user=True)

GUILD_ONLY_CONTEXTS = app_commands.AppCommandContext(guild=True, dm_channel=False, private_channel=False)

DM_REFUSAL = "This command only works in the server."


class GuildOnlyCommandTree(app_commands.CommandTree):
    def __init__(self, client, **kwargs) -> None:
        kwargs.setdefault("allowed_contexts", GUILD_ONLY_CONTEXTS)
        super().__init__(client, **kwargs)

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.guild_id is not None and interaction.guild is not None:
            return True
        try:
            if interaction.type == discord.InteractionType.autocomplete:
                await interaction.response.autocomplete([])
            elif not interaction.response.is_done():
                await interaction.response.send_message(DM_REFUSAL, ephemeral=True)
        except discord.HTTPException as exc:
            log.debug("guild-only refusal could not be sent: %s", exc)
        return False


def create_bot(intents: discord.Intents) -> commands.Bot:
    return commands.Bot(
        command_prefix=PREFIX,
        intents=intents,
        allowed_mentions=DEFAULT_ALLOWED_MENTIONS,
        tree_cls=GuildOnlyCommandTree,
    )
