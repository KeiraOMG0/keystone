import discord
from discord.ext import commands

import config

OWNER_ID = config.OWNER_ID
STAFF_ROLE_IDS = config.STAFF_ROLE_IDS


def is_staff(member: discord.Member) -> bool:
    if member.id == OWNER_ID:
        return True
    return bool({r.id for r in member.roles} & STAFF_ROLE_IDS)


def is_staff_ctx(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        return ctx.author.id == OWNER_ID
    return is_staff(ctx.author)


def is_staff_interaction(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return interaction.user.id == OWNER_ID
    return is_staff(interaction.user)
