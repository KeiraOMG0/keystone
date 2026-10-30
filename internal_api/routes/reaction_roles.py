"""
GET    /guilds/{guild_id}/reaction-roles/panels
GET    /guilds/{guild_id}/reaction-roles/panels/{panel_id}
POST   /guilds/{guild_id}/reaction-roles/panels
PATCH  /guilds/{guild_id}/reaction-roles/panels/{panel_id}
DELETE /guilds/{guild_id}/reaction-roles/panels/{panel_id}
POST   /guilds/{guild_id}/reaction-roles/panels/{panel_id}/post
POST   /guilds/{guild_id}/reaction-roles/panels/{panel_id}/entries
DELETE /guilds/{guild_id}/reaction-roles/panels/{panel_id}/entries/{entry_id}
GET    /guilds/{guild_id}/channels
GET    /guilds/{guild_id}/reaction-roles/roles?actor_id=   (role picker + blocked_reason)

Thin wrapper over services.reaction_role_service.ReactionRoleService — the
SAME service cogs/reaction_roles.py's `/reactionrole` slash commands call,
so dashboard-issued panel edits and Discord-command-issued edits are
indistinguishable in storage and, critically, both go through
ReactionRoleService.sync_panel_message so the live Discord panel message
never goes stale relative to the DB (see that service's module docstring
for the full explanation of the desync problem this solves).

`/channels` is a small guild-metadata helper added here (not its own route
module) purely so the dashboard's panel-creation form can offer a channel
picker — no other feature has needed one yet. Only text channels are
listed (reaction role panels can only be posted to text channels).

Self-role safety: POST .../entries REQUIRES `actor_id` (the dashboard session
user, sent by server/routes/reaction_roles.py, never taken from the browser)
and resolves it to a live Member; the role then goes through
ReactionRoleService.entry_block_reason (services/self_role_policy.py). A block
is 409 {"error": "role_not_allowed", "message": <reason>}; an unknown actor
is 409 {"error": "actor_not_found", ...}. GET .../reaction-roles/roles returns
every role with `blocked_reason` (null = allowed) for that actor so the
dashboard picker can disable ineligible roles; the POST check is what counts.
"""

from __future__ import annotations

import discord
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from internal_api.auth import require_internal_secret
from internal_api.deps import get_guild, get_member, get_reaction_role_service
from internal_api.snowflakes import stringify_snowflakes

router = APIRouter(dependencies=[Depends(require_internal_secret)])


def _raise_for_errors(errors: list[str]) -> None:
    if not errors:
        return
    if any("not found" in e.lower() for e in errors):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=errors)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_request", "message": errors[0]},
    )


async def _resolve_actor(actor_id: int, guild: discord.Guild) -> discord.Member:
    try:
        return await get_member(actor_id, guild)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "actor_not_found", "message": "Couldn't find you in the server."},
            )
        raise


@router.get("/guilds/{guild_id}/reaction-roles/panels")
async def list_panels(
    guild: discord.Guild = Depends(get_guild),
    rr_service=Depends(get_reaction_role_service),
) -> list[dict]:
    panels = await rr_service.list_panels(guild.id)
    out = []
    for panel in panels:
        entries = await rr_service.get_entries(panel["id"])
        out.append({**panel, "entries": entries})
    return stringify_snowflakes(out)


@router.get("/guilds/{guild_id}/reaction-roles/panels/{panel_id}")
async def get_panel(
    panel_id: int,
    guild: discord.Guild = Depends(get_guild),
    rr_service=Depends(get_reaction_role_service),
) -> dict:
    result = await rr_service.get_panel_with_entries(panel_id)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found.")
    panel, entries = result
    if panel["guild_id"] != guild.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found.")
    return stringify_snowflakes({**panel, "entries": entries})


class CreatePanelRequest(BaseModel):
    channel_id: int
    created_by: int
    title: str = "React for a role!"
    exclusive: bool = False


@router.post("/guilds/{guild_id}/reaction-roles/panels", status_code=status.HTTP_201_CREATED)
async def create_panel(
    body: CreatePanelRequest,
    guild: discord.Guild = Depends(get_guild),
    rr_service=Depends(get_reaction_role_service),
) -> dict:
    channel = guild.get_channel(body.channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channel_id must be a text channel in this guild.",
        )
    panel_id = await rr_service.create_panel(
        guild_id=guild.id,
        channel_id=body.channel_id,
        created_by=body.created_by,
        title=body.title,
        exclusive=body.exclusive,
    )
    return {"panel_id": panel_id}


class UpdatePanelRequest(BaseModel):
    title: str | None = None
    exclusive: bool | None = None


@router.patch("/guilds/{guild_id}/reaction-roles/panels/{panel_id}")
async def update_panel(
    panel_id: int,
    body: UpdatePanelRequest,
    guild: discord.Guild = Depends(get_guild),
    rr_service=Depends(get_reaction_role_service),
) -> dict:
    panel = await rr_service.get_panel(panel_id)
    if not panel or panel["guild_id"] != guild.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found.")
    errors = await rr_service.update_panel(
        panel_id, guild=guild, title=body.title, exclusive=body.exclusive,
    )
    # update_panel's only error path ("Panel not found.") can't happen here
    # since we already checked above, but keep the same 404/400 mapping for
    # consistency with every other route in this file.
    _raise_for_errors(errors)
    return {"success": True}


@router.delete("/guilds/{guild_id}/reaction-roles/panels/{panel_id}")
async def delete_panel(
    panel_id: int,
    guild: discord.Guild = Depends(get_guild),
    rr_service=Depends(get_reaction_role_service),
) -> dict:
    panel = await rr_service.get_panel(panel_id)
    if not panel or panel["guild_id"] != guild.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found.")
    errors = await rr_service.delete_panel(panel_id, guild=guild)
    _raise_for_errors(errors)
    return {"success": True}


@router.post("/guilds/{guild_id}/reaction-roles/panels/{panel_id}/post")
async def post_panel(
    panel_id: int,
    guild: discord.Guild = Depends(get_guild),
    rr_service=Depends(get_reaction_role_service),
) -> dict:
    """First post OR explicit re-post (deletes+resends). See
    ReactionRoleService.post_panel's docstring for why this is kept
    separate from the auto-sync every other edit endpoint below performs."""
    panel = await rr_service.get_panel(panel_id)
    if not panel or panel["guild_id"] != guild.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found.")
    message_id, errors = await rr_service.post_panel(panel_id, guild)
    _raise_for_errors(errors)
    return stringify_snowflakes({"message_id": message_id})


class AddEntryRequest(BaseModel):
    emoji: str
    role_id: int
    label: str | None = None
    # The configuring member (dashboard session user). Required: the
    # self-role escalation check needs someone to check against.
    actor_id: int


@router.post("/guilds/{guild_id}/reaction-roles/panels/{panel_id}/entries", status_code=status.HTTP_201_CREATED)
async def add_entry(
    panel_id: int,
    body: AddEntryRequest,
    guild: discord.Guild = Depends(get_guild),
    rr_service=Depends(get_reaction_role_service),
) -> dict:
    panel = await rr_service.get_panel(panel_id)
    if not panel or panel["guild_id"] != guild.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found.")
    role = guild.get_role(body.role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "role_not_found", "message": "That role no longer exists."},
        )
    actor = await _resolve_actor(body.actor_id, guild)
    reason = await rr_service.entry_block_reason(guild, role, actor)
    if reason:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "role_not_allowed", "message": reason},
        )
    entry_id, errors = await rr_service.add_entry(
        panel_id, body.emoji, body.role_id, body.label, guild=guild, actor=actor,
    )
    _raise_for_errors(errors)
    return {"entry_id": entry_id}


@router.delete("/guilds/{guild_id}/reaction-roles/panels/{panel_id}/entries/{entry_id}")
async def remove_entry(
    panel_id: int,
    entry_id: int,
    guild: discord.Guild = Depends(get_guild),
    rr_service=Depends(get_reaction_role_service),
) -> dict:
    panel = await rr_service.get_panel(panel_id)
    if not panel or panel["guild_id"] != guild.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Panel not found.")
    errors = await rr_service.remove_entry(panel_id, entry_id, guild=guild)
    _raise_for_errors(errors)
    return {"success": True}


@router.get("/guilds/{guild_id}/channels")
async def list_channels(
    guild: discord.Guild = Depends(get_guild),
) -> list[dict]:
    """Text channels only, for the dashboard's panel-creation channel picker.

    `id` here is the channel's own Discord snowflake (NOT a DB autoincrement
    row id — this route has no DB row at all, it's a live discord.py object
    dump), so it must be stringified same as every other snowflake field.
    SNOWFLAKE_KEYS deliberately excludes bare "id" because that key means a
    safe small DB autoincrement id everywhere else (tickets, warnings,
    templates, panels) — this route is the one place "id" IS the unsafe
    kind, so it's stringified explicitly here rather than by adding "id" to
    the shared allowlist (which would incorrectly also touch every DB id).
    """
    return [
        {"id": str(c.id), "name": c.name, "position": c.position} for c in guild.text_channels
    ]


@router.get("/guilds/{guild_id}/reaction-roles/roles")
async def list_self_role_candidates(
    actor_id: int | None = None,
    guild: discord.Guild = Depends(get_guild),
    rr_service=Depends(get_reaction_role_service),
) -> dict:
    """Role picker for the Self-roles page: every role (except @everyone),
    highest first, same fields as GET /guilds/{id}/roles plus
    `blocked_reason` (None = this actor may add it as a self-role). Only a
    UI hint: POST .../entries re-checks."""
    from services import self_role_policy
    from services.permission_service import DEFAULT_STAFF_ROLE_IDS

    actor = None
    if actor_id is not None:
        try:
            actor = await get_member(actor_id, guild)
        except HTTPException:
            actor = None
    roles = [r for r in guild.roles if not r.is_default()]
    try:
        reasons = await rr_service.candidate_block_reasons(guild, roles, actor)
        if actor is None and actor_id is not None:
            reasons = {rid: (r or "Couldn't find you in the server.") for rid, r in reasons.items()}
    except Exception:
        reasons = {r.id: self_role_policy.GRANTS_UNAVAILABLE for r in roles}
    out = [
        {
            "id": str(role.id),
            "name": role.name,
            "color": f"#{role.color.value:06x}" if role.color.value else None,
            "position": role.position,
            "managed": role.managed,
            "is_default_staff": role.id in DEFAULT_STAFF_ROLE_IDS,
            "blocked_reason": reasons.get(role.id),
        }
        for role in roles
    ]
    out.sort(key=lambda r: r["position"], reverse=True)
    return {"roles": out}
