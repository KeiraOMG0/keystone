"""
Minimal Discord login helper for the setup utility.

Deliberately does not import anything from the main bot (cogs/, services/,
database/, core/). The only thing shared with the main bot is the
DISCORD_TOKEN value, read independently here via python-dotenv the same way
config.py does at the project root.
"""

from __future__ import annotations

import os

import discord

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def get_token() -> str:
    token = os.environ.get("DISCORD_TOKEN", "")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. Add it to your .env file or export it "
            "as an environment variable before running setup_server."
        )
    return token


async def fetch_guild(client: discord.Client, guild_id: int) -> discord.Guild:
    guild = client.get_guild(guild_id)
    if guild is not None:
        return guild
    return await client.fetch_guild(guild_id)


def make_client() -> discord.Client:
    intents = discord.Intents.default()
    intents.members = True
    return discord.Client(intents=intents)
