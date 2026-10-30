"""
POST /guilds/{guild_id}/permissions/check
POST /guilds/{guild_id}/permissions/check-bulk

Wraps services.permission_service.PermissionService.has_permission() for the
dashboard. Always resolves a LIVE discord.Member via the get_member
dependency before checking — has_permission() reads member.roles, so a bare
ID is not sufficient.

The bulk variant exists to avoid a dashboard page making N separate
round-trips to check N permission strings for the same user on page load.

---------------------------------------------------------------------------
Permission-grants management (staff permission-grants dashboard page)
---------------------------------------------------------------------------
POST   /guilds/{guild_id}/permissions/is-staff              -- is_staff() check
GET    /guilds/{guild_id}/permission-grants                 -- list all grants
POST   /guilds/{guild_id}/permission-grants                 -- grant
DELETE /guilds/{guild_id}/permission-grants                 -- revoke {role_id, permission, actor_id?}
GET    /guilds/{guild_id}/roles                              -- live Discord roles

These manage the permission-GRANT system itself (which roles have which
fine-grained permission strings), so they are intentionally gated stricter
than has_permission()-based checks: the caller must be actual is_staff()
(owner or a DEFAULT_STAFF_ROLE_IDS role), NOT merely someone holding a
granted permission (e.g. a hypothetical "manage_permissions" grant). Reason:
has_permission()-gating here would let any role holding that grant escalate
its own privileges (grant itself more permissions, or grant other roles
permissions) with no ceiling — self-service privilege escalation. is_staff()
cannot be escalated via the grants table at all (it only ever checks
OWNER_ID / DEFAULT_STAFF_ROLE_IDS, both fixed at deploy/config time), so it's
the appropriate trust boundary for a page whose entire purpose is managing
that grants table.

/permissions/is-staff is a separate endpoint (not reusing /permissions/check)
because is_staff() is a different, stricter check than has_permission() —
conflating them here would make it easy for a future caller to accidentally
gate something is_staff-sensitive on the wrong function.
"""

from __future__ import annotations

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild, get_member
from internal_api.snowflakes import stringify_snowflakes
from services.permission_service import VALID_PERMISSIONS

router = APIRouter(dependencies=[Depends(require_internal_secret)])


class PermissionCheckRequest(BaseModel):
    user_id: int
    permission: str


class PermissionCheckBulkRequest(BaseModel):
    user_id: int
    permissions: list[str]


@router.post("/guilds/{guild_id}/permissions/check")
async def check_permission(
    body: PermissionCheckRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    member = await get_member(body.user_id, guild)
    allowed = await bot.permission_service.has_permission(member, body.permission)
    return {"allowed": allowed}


@router.post("/guilds/{guild_id}/permissions/check-bulk")
async def check_permissions_bulk(
    body: PermissionCheckBulkRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    member = await get_member(body.user_id, guild)
    results: dict[str, bool] = {}
    for permission in body.permissions:
        results[permission] = await bot.permission_service.has_permission(member, permission)
    return results


# ---------------------------------------------------------------------------
# is_staff() check — the gate for the grant-management routes below.
# ---------------------------------------------------------------------------

class IsStaffCheckRequest(BaseModel):
    user_id: int


@router.post("/guilds/{guild_id}/permissions/is-staff")
async def check_is_staff(
    body: IsStaffCheckRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    member = await get_member(body.user_id, guild)
    return {"is_staff": bot.permission_service.is_staff(member)}


# ---------------------------------------------------------------------------
# Live Discord role list — name/id/color, for the dashboard's grants matrix
# to show real role names instead of bare IDs. No new DB query needed: pulled
# straight off the live discord.Guild object (guild.roles), per deps.py's
# get_guild dependency.
# ---------------------------------------------------------------------------

@router.get("/guilds/{guild_id}/roles")
async def list_roles(
    guild: discord.Guild = Depends(get_guild),
) -> dict:
    from services.permission_service import DEFAULT_STAFF_ROLE_IDS

    # "id" here is the role's own Discord snowflake, not a DB row id — this
    # route has no DB row at all, it's a live discord.Role object dump, so
    # it must be stringified (see internal_api/snowflakes.py's module
    # docstring for why bare "id" is excluded from the shared allowlist).
    roles = [
        {
            "id": str(role.id),
            "name": role.name,
            "color": f"#{role.color.value:06x}" if role.color.value else None,
            "position": role.position,
            "managed": role.managed,
            # True if this role is in DEFAULT_STAFF_ROLE_IDS
            # (services/permission_service.py) — such roles already bypass
            # every has_permission() check via is_staff(), regardless of
            # explicit grants. Surfaced so the grants-matrix UI can mark/
            # disable these rows rather than implying grants control them.
            "is_default_staff": role.id in DEFAULT_STAFF_ROLE_IDS,
        }
        for role in guild.roles
        if role.name != "@everyone"
    ]
    # Highest role first, matching Discord's own role-list ordering.
    roles.sort(key=lambda r: r["position"], reverse=True)
    return stringify_snowflakes({"roles": roles})


# ---------------------------------------------------------------------------
# Permission-grants CRUD — thin wrapper over
# services.permission_service.PermissionService.grant/revoke/list_grants.
# PermissionService writes the permission_grant / permission_revoke audit rows
# (actor = granted_by / actor_id), so this route doesn't log them itself.
# ---------------------------------------------------------------------------

class GrantRequest(BaseModel):
    role_id: int
    permission: str
    granted_by: int


class RevokeRequest(BaseModel):
    role_id: int
    permission: str
    # Who revoked it (the dashboard session user), for the permission_revoke
    # audit row. Optional for older callers; the row then records actor 0.
    actor_id: int | None = None


@router.get("/guilds/{guild_id}/permission-grants")
async def list_permission_grants(
    guild_id: int,
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    grants = await bot.permission_service.list_grants(guild_id)
    return stringify_snowflakes({"grants": grants, "valid_permissions": sorted(VALID_PERMISSIONS)})


@router.post("/guilds/{guild_id}/permission-grants", status_code=status.HTTP_201_CREATED)
async def grant_permission(
    guild_id: int,
    body: GrantRequest,
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    errors = await bot.permission_service.grant(guild_id, body.role_id, body.permission, body.granted_by)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_grant", "message": errors[0], "errors": errors},
        )
    return {"granted": True}


@router.delete("/guilds/{guild_id}/permission-grants")
async def revoke_permission(
    guild_id: int,
    body: RevokeRequest,
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    revoked = await bot.permission_service.revoke(guild_id, body.role_id, body.permission, actor_id=body.actor_id)
    return {"revoked": revoked}
