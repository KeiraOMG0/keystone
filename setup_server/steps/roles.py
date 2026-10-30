"""
Reconciles guild roles against config.ROLES.

Idempotent by name lookup: state.json is checked first (fast path), then
falls back to scanning guild.roles by exact name match (repair path, used
when state.json is missing/stale or a role was created manually). Never
recreates a role that already exists; never deletes or renames roles that
aren't in config.py, so unrelated roles (Admin, bot roles, etc.) are left
alone.
"""

from __future__ import annotations

import discord

from setup_server.config import ROLES, RoleSpec
from setup_server.state import ServerState


async def ensure_roles(guild: discord.Guild, state: ServerState) -> list[str]:
    """Create any missing roles. Returns a list of human-readable actions taken."""
    actions: list[str] = []
    existing_by_name = {role.name: role for role in guild.roles}

    for spec in ROLES:
        role_id = state.roles.get(spec.name)
        role = guild.get_role(role_id) if role_id else None

        if role is None:
            role = existing_by_name.get(spec.name)

        if role is None:
            role = await guild.create_role(
                name=spec.name,
                color=spec.color,
                hoist=spec.hoist,
                mentionable=spec.mentionable,
                permissions=spec.permissions,
                reason="setup_server: creating role from desired state",
            )
            actions.append(f"created role '{spec.name}' ({role.id})")

        state.roles[spec.name] = role.id

    return actions


def resolve_role(guild: discord.Guild, state: ServerState, name: str) -> discord.Role | None:
    """Look up a role by name via state first, falling back to a live scan."""
    role_id = state.roles.get(name)
    role = guild.get_role(role_id) if role_id else None
    if role is not None:
        return role
    return discord.utils.get(guild.roles, name=name)
