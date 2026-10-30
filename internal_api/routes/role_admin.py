"""
Role management for the staff-only dashboard Roles page.

GET    /guilds/{guild_id}/role-admin/roles                               -> overview
POST   /guilds/{guild_id}/role-admin/roles                               -> create
PATCH  /guilds/{guild_id}/role-admin/roles/{role_id}                     -> edit
POST   /guilds/{guild_id}/role-admin/roles/{role_id}/move                -> reorder
POST   /guilds/{guild_id}/role-admin/roles/{role_id}/duplicate           -> copy
DELETE /guilds/{guild_id}/role-admin/roles/{role_id}                     -> delete (body: confirm_name)
GET    /guilds/{guild_id}/role-admin/roles/{role_id}/members             -> paginated members
POST   /guilds/{guild_id}/role-admin/roles/{role_id}/members/{user_id}   -> add role to member
DELETE /guilds/{guild_id}/role-admin/roles/{role_id}/members/{user_id}   -> remove role from member

Bearer secret only (no user auth here): the dashboard backend gates every
route with require_staff and passes the acting user's id and username from the
session. All rules (bot AND acting-user hierarchy, bot/actor-held
permissions, managed roles) are enforced in services/role_admin_service.py. Errors come back as
HTTPException(detail={"error": code, "message": ...}):
  400 validation / administrator_needs_confirm, 403 discord_forbidden / permission_not_held /
  bot_missing_manage_roles / actor_hierarchy / actor_permission_not_held / actor_not_member /
  self_assign_forbidden,
  404 role_not_found / member_not_found, 409 role_not_manageable / role_limit,
  502 discord_error (any other Discord HTTP failure).

The `/role-admin/` prefix keeps these apart from the existing read-only
GET /guilds/{guild_id}/roles (permissions.py) used by other pages.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from internal_api.snowflakes import stringify_snowflakes
from services.role_admin_service import Actor, RoleAdminError, RoleAdminService

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_internal_secret)])

_BASE = "/guilds/{guild_id}/role-admin/roles"


def _service(guild: discord.Guild = Depends(get_guild), bot: commands.Bot = Depends(get_bot)) -> RoleAdminService:
    audit = None
    db = getattr(bot, "db", None)
    if db is not None:
        try:
            from database.repositories.audit_repo import AuditRepository
            from services.audit_service import AuditService

            audit = AuditService(AuditRepository(db))
        except ImportError:  # pragma: no cover
            audit = None
    return RoleAdminService(bot, guild, audit)


async def _run(coro):
    """Await a service call, mapping failures to clean JSON errors."""
    try:
        return stringify_snowflakes(await coro)
    except RoleAdminError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.as_detail())
    except discord.Forbidden as exc:
        raise HTTPException(
            status_code=403,
            detail={"error": "discord_forbidden", "message": f"Discord refused the change: {exc.text or 'missing permissions'}."},
        )
    except discord.NotFound:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Discord could not find that role or member."})
    except discord.HTTPException as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "discord_error", "message": f"Discord rejected the request: {exc.text or exc.status}."},
        )


class ActorFields(BaseModel):
    actor_id: int
    actor_name: str | None = Field(default=None, max_length=100)

    def actor(self) -> Actor:
        return Actor(self.actor_id, self.actor_name)


class CreateRoleBody(ActorFields):
    name: str = Field(max_length=200)
    color: str | None = None
    hoist: bool = False
    mentionable: bool = False
    permissions: list[str] = []
    position: int | None = Field(default=None, ge=1)
    # Alternative to `position`: place the new role directly above this role
    # (the guild id = @everyone = bottom). Resolved after the create, against
    # Discord's fresh ordering.
    above_role_id: int | None = None
    unicode_emoji: str | None = None
    icon_base64: str | None = Field(default=None, max_length=400_000)
    internal_permissions: list[str] = []
    confirm_administrator: bool = False


class UpdateRoleBody(ActorFields):
    name: str | None = Field(default=None, max_length=200)
    color: str | None = None
    hoist: bool | None = None
    mentionable: bool | None = None
    permissions: list[str] | None = None
    unicode_emoji: str | None = None
    icon_base64: str | None = Field(default=None, max_length=400_000)
    clear_icon: bool = False
    internal_permissions: list[str] | None = None
    confirm_administrator: bool = False


class MoveBody(ActorFields):
    position: int = Field(ge=1)


class DuplicateBody(ActorFields):
    name: str | None = Field(default=None, max_length=200)
    confirm_administrator: bool = False


class DeleteBody(ActorFields):
    confirm_name: str = Field(max_length=200)


@router.get(_BASE)
async def overview(
    actor_id: int | None = Query(None, description="Session user: also apply their role hierarchy to `manageable`"),
    svc: RoleAdminService = Depends(_service),
) -> dict:
    return await _run(svc.overview(actor_id))


@router.post(_BASE, status_code=201)
async def create_role(body: CreateRoleBody, svc: RoleAdminService = Depends(_service)) -> dict:
    return await _run(
        svc.create_role(
            body.actor(),
            name=body.name,
            color=body.color,
            hoist=body.hoist,
            mentionable=body.mentionable,
            permissions=body.permissions,
            position=body.position,
            unicode_emoji=body.unicode_emoji,
            icon_base64=body.icon_base64,
            internal_permissions=body.internal_permissions,
            confirm_administrator=body.confirm_administrator,
            above_role_id=body.above_role_id,
        )
    )


@router.patch(_BASE + "/{role_id}")
async def update_role(role_id: int, body: UpdateRoleBody, svc: RoleAdminService = Depends(_service)) -> dict:
    fields = body.model_dump(
        include={"name", "color", "hoist", "mentionable", "permissions", "unicode_emoji", "icon_base64", "clear_icon"}
    )
    return await _run(
        svc.update_role(
            body.actor(),
            role_id,
            fields=fields,
            internal_permissions=body.internal_permissions,
            confirm_administrator=body.confirm_administrator,
        )
    )


@router.post(_BASE + "/{role_id}/move")
async def move_role(role_id: int, body: MoveBody, svc: RoleAdminService = Depends(_service)) -> dict:
    return await _run(svc.move_role(body.actor(), role_id, body.position))


@router.post(_BASE + "/{role_id}/duplicate", status_code=201)
async def duplicate_role(role_id: int, body: DuplicateBody, svc: RoleAdminService = Depends(_service)) -> dict:
    return await _run(
        svc.duplicate_role(body.actor(), role_id, body.name, confirm_administrator=body.confirm_administrator)
    )


@router.delete(_BASE + "/{role_id}")
async def delete_role(role_id: int, body: DeleteBody, svc: RoleAdminService = Depends(_service)) -> dict:
    return await _run(svc.delete_role(body.actor(), role_id, body.confirm_name))


@router.get(_BASE + "/{role_id}/members")
async def list_members(
    role_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    q: str | None = Query(None, max_length=100),
    svc: RoleAdminService = Depends(_service),
) -> dict:
    async def _call():
        return svc.list_members(role_id, offset, limit, q)

    return await _run(_call())


@router.post(_BASE + "/{role_id}/members/{user_id}")
async def add_member(role_id: int, user_id: int, body: ActorFields, svc: RoleAdminService = Depends(_service)) -> dict:
    return await _run(svc.add_member(body.actor(), role_id, user_id))


@router.delete(_BASE + "/{role_id}/members/{user_id}")
async def remove_member(role_id: int, user_id: int, body: ActorFields, svc: RoleAdminService = Depends(_service)) -> dict:
    return await _run(svc.remove_member(body.actor(), role_id, user_id))
