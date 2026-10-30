"""
services/self_role_policy.py - who may make a role self-assignable, and may a
self-role click / autorole still hand it out.

Thin async layer over services.hierarchy.role_grant_block_reason that adds the
DB-backed part hierarchy.py deliberately leaves to callers: the set of
"privileged" roles that must never be self-assignable, namely

    config.STAFF_ROLE_IDS  plus  every role with ANY row in permission_grants

(read through PermissionService.list_grants, so a grant made on the dashboard
Permissions page takes effect immediately).

Used at CONFIGURE time (with the configuring member as `actor`):
    /reactionrole add, /autorole add, dashboard Self-roles "Add role"
    (internal_api/routes/reaction_roles.py, actor = the dashboard session user)
and at GRANT time (no actor): reaction-role button clicks
(cogs/reaction_roles.py) and every autorole application
(services/autorole_service.py).

Fail closed: if the grants cannot be read, a reason is returned (the role is
NOT handed out / NOT configured).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

import config
from services.hierarchy import role_grant_block_reason

log = logging.getLogger(__name__)

GRANTS_UNAVAILABLE = "Couldn't check the bot's permission grants right now, so nothing was changed. Try again."


def is_owner(member: Any, guild: Any) -> bool:
    """config.OWNER_ID or the guild owner."""
    member_id = int(getattr(member, "id", 0) or 0)
    if not member_id:
        return False
    owner_id = int(getattr(config, "OWNER_ID", 0) or 0)
    if owner_id and member_id == owner_id:
        return True
    return member_id == int(getattr(guild, "owner_id", 0) or 0)


async def privileged_role_ids(permission_service: Any, guild_id: int) -> set[int]:
    """STAFF_ROLE_IDS plus every role holding any bot permission grant.
    Raises if the grants cannot be read (callers fail closed)."""
    ids = {int(r) for r in (getattr(config, "STAFF_ROLE_IDS", None) or ())}
    if permission_service is not None:
        grants = await permission_service.list_grants(guild_id)
        ids |= {int(g["role_id"]) for g in grants or () if g.get("role_id") is not None}
    return ids


async def configure_block_reason(role: Any, guild: Any, actor: Any, permission_service: Any) -> str | None:
    """Why `actor` may not make `role` self-assignable, or None."""
    if actor is None:
        return "Couldn't find you in the server."
    try:
        privileged = await privileged_role_ids(permission_service, guild.id)
    except Exception:
        log.exception("self-roles: could not read permission grants")
        return GRANTS_UNAVAILABLE
    return role_grant_block_reason(
        role, getattr(guild, "me", None),
        actor=actor, actor_is_owner=is_owner(actor, guild), privileged_role_ids=privileged,
    )


async def grant_block_reason(role: Any, guild: Any, permission_service: Any) -> str | None:
    """Click-time / autorole re-check (no actor): why the bot must not hand
    `role` out right now, or None."""
    try:
        privileged = await privileged_role_ids(permission_service, guild.id)
    except Exception:
        log.exception("self-roles: could not read permission grants")
        return GRANTS_UNAVAILABLE
    return role_grant_block_reason(role, getattr(guild, "me", None), privileged_role_ids=privileged)


def block_reasons(roles: Iterable[Any], guild: Any, actor: Any, privileged: set[int]) -> dict[int, str | None]:
    """role id -> reason (or None) for every role, for the dashboard picker."""
    bot_member = getattr(guild, "me", None)
    owner = is_owner(actor, guild) if actor is not None else False
    return {
        int(r.id): role_grant_block_reason(
            r, bot_member, actor=actor, actor_is_owner=owner, privileged_role_ids=privileged,
        )
        for r in roles
    }
