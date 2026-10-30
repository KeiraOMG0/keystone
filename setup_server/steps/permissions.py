"""
Applies permission overwrites from config.py to categories and channels.

Runs last, after roles/categories/channels are all guaranteed to exist, so
every role-name reference in an `overwrites` dict resolves to a real ID.
Permission overwrites are declarative in the Discord API (a full
PermissionOverwrite object per target), so applying them is naturally
idempotent — but we still diff against the live overwrites before calling
.edit() so a clean re-run makes zero API calls instead of re-writing
everything on every invocation.

Channel-level overwrites are layered on top of (not merged with) the
category's overwrites in config.py, matching normal Discord permission
inheritance — a channel only needs an overwrites entry for a role if it
should differ from what the category already grants.
"""

from __future__ import annotations

import discord

from setup_server.config import CATEGORIES, CHANNELS, EVERYONE
from setup_server.state import ServerState
from setup_server.steps.categories import resolve_category
from setup_server.steps.roles import resolve_role


def _resolve_target(guild: discord.Guild, state: ServerState, name: str):
    if name == EVERYONE:
        return guild.default_role
    return resolve_role(guild, state, name)


def _build_overwrites(guild: discord.Guild, state: ServerState, spec_overwrites: dict) -> dict:
    result = {}
    for role_name, kwargs in spec_overwrites.items():
        target = _resolve_target(guild, state, role_name)
        if target is None:
            continue
        result[target] = discord.PermissionOverwrite(**kwargs)
    return result


def _overwrites_match(current: dict, desired: dict) -> bool:
    if set(current.keys()) != set(desired.keys()):
        return False
    return all(current[target] == overwrite for target, overwrite in desired.items())


async def apply_permissions(guild: discord.Guild, state: ServerState) -> list[str]:
    actions: list[str] = []

    for spec in CATEGORIES:
        category = resolve_category(guild, state, spec.name)
        if category is None:
            actions.append(f"SKIPPED permissions for category '{spec.name}': not found")
            continue
        overwrites = _build_overwrites(guild, state, spec.overwrites)
        if _overwrites_match(category.overwrites, overwrites):
            continue
        await category.edit(overwrites=overwrites, reason="setup_server: applying desired permissions")
        actions.append(f"applied permissions on category '{spec.name}'")

    for spec in CHANNELS:
        ch_id = state.channels.get(spec.name)
        channel = guild.get_channel(ch_id) if ch_id else None
        if channel is None:
            actions.append(f"SKIPPED permissions for channel '{spec.name}': not found")
            continue
        if not spec.overwrites:
            # No channel-specific overwrites -> sync to category so it doesn't
            # keep stale per-channel overwrites from a previous config.
            if channel.category is not None and not channel.permissions_synced:
                await channel.edit(sync_permissions=True, reason="setup_server: syncing to category")
                actions.append(f"synced channel '{spec.name}' permissions to category")
            continue
        overwrites = _build_overwrites(guild, state, spec.overwrites)
        if _overwrites_match(channel.overwrites, overwrites):
            continue
        await channel.edit(overwrites=overwrites, reason="setup_server: applying desired permissions")
        actions.append(f"applied permissions on channel '{spec.name}'")

    return actions
