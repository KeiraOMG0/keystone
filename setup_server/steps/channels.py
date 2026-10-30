"""
Reconciles guild channels against config.CHANNELS.

Idempotent by (name, category) lookup rather than name alone, since Discord
allows duplicate channel names across different categories. state.json is
checked first; the repair path falls back to scanning guild.channels for a
name+category match.
"""

from __future__ import annotations

import discord

from setup_server.config import CHANNELS
from setup_server.state import ServerState
from setup_server.steps.categories import resolve_category


def _find_in_guild(
    guild: discord.Guild, name: str, category: discord.CategoryChannel | None
) -> discord.abc.GuildChannel | None:
    for ch in guild.channels:
        if ch.name == name and ch.category_id == (category.id if category else None):
            return ch
    return None


async def ensure_channels(guild: discord.Guild, state: ServerState) -> list[str]:
    actions: list[str] = []

    for spec in CHANNELS:
        category = resolve_category(guild, state, spec.category) if spec.category else None
        if spec.category and category is None:
            actions.append(f"SKIPPED channel '{spec.name}': category '{spec.category}' not found")
            continue

        ch_id = state.channels.get(spec.name)
        channel = guild.get_channel(ch_id) if ch_id else None
        if channel is not None and channel.category_id != (category.id if category else None):
            # state points at a channel that's since moved categories elsewhere; don't trust it blindly
            channel = None

        if channel is None:
            channel = _find_in_guild(guild, spec.name, category)

        if channel is None:
            if spec.type == discord.ChannelType.voice:
                channel = await guild.create_voice_channel(
                    name=spec.name,
                    category=category,
                    position=spec.position,
                    reason="setup_server: creating channel from desired state",
                )
            else:
                channel = await guild.create_text_channel(
                    name=spec.name,
                    category=category,
                    topic=spec.topic,
                    nsfw=spec.nsfw,
                    position=spec.position,
                    reason="setup_server: creating channel from desired state",
                )
            actions.append(f"created channel '{spec.name}' ({channel.id})")

        state.channels[spec.name] = channel.id

    return actions
