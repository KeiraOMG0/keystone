"""
Reconciles guild categories against config.CATEGORIES.

Same idempotent-by-name-lookup pattern as roles.py. Permission overwrites
are NOT applied here — that's steps/permissions.py's job, run after every
role/category/channel is guaranteed to exist so role-name -> ID resolution
always succeeds.
"""

from __future__ import annotations

import discord

from setup_server.config import CATEGORIES
from setup_server.state import ServerState


async def ensure_categories(guild: discord.Guild, state: ServerState) -> list[str]:
    actions: list[str] = []
    existing_by_name = {cat.name: cat for cat in guild.categories}

    for spec in CATEGORIES:
        cat_id = state.categories.get(spec.name)
        category = guild.get_channel(cat_id) if cat_id else None
        if category is not None and not isinstance(category, discord.CategoryChannel):
            category = None

        if category is None:
            category = existing_by_name.get(spec.name)

        if category is None:
            category = await guild.create_category(
                name=spec.name,
                position=spec.position,
                reason="setup_server: creating category from desired state",
            )
            actions.append(f"created category '{spec.name}' ({category.id})")

        state.categories[spec.name] = category.id

    return actions


def resolve_category(guild: discord.Guild, state: ServerState, name: str) -> discord.CategoryChannel | None:
    cat_id = state.categories.get(name)
    category = guild.get_channel(cat_id) if cat_id else None
    if isinstance(category, discord.CategoryChannel):
        return category
    found = discord.utils.get(guild.categories, name=name)
    return found
