"""
GET    /api/reaction-roles/panels
GET    /api/reaction-roles/panels/{panel_id}
POST   /api/reaction-roles/panels
PATCH  /api/reaction-roles/panels/{panel_id}
DELETE /api/reaction-roles/panels/{panel_id}
POST   /api/reaction-roles/panels/{panel_id}/post
POST   /api/reaction-roles/panels/{panel_id}/entries
DELETE /api/reaction-roles/panels/{panel_id}/entries/{entry_id}
GET    /api/channels
GET    /api/reaction-roles/roles

Thin, permission-gated client of internal_api/routes/reaction_roles.py,
following the exact same shape as server/routes/tickets.py: require_permission
first, then delegate to services.internal_client, translating InternalAPIError
into the same {"error": "internal_api_unavailable"} 502 body used elsewhere,
and 404s into {"error": "panel_not_found"} / {"error": "entry_not_found"}.

Gated on "manage_reaction_roles" — the exact permission string
cogs/reaction_roles.py's own /reactionrole slash commands already require
(services/permission_service.py's VALID_PERMISSIONS), so dashboard access
and Discord-command access to this feature require the same grant. No new
permission was invented for this page.

POST /panels populates `created_by` from session.discord_user_id — never
from the request body — matching moderation.py/tickets.py's convention of
deriving the acting user from the authenticated session. POST .../entries
likewise sends the session user as `actor_id`: the bot refuses roles that
user may not make self-assignable (services/self_role_policy.py) with 409
{"error": "role_not_allowed", "message": ...}, passed through unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api", tags=["reaction-roles"])

_PERMISSION = "manage_reaction_roles"


def _error_response(exc: InternalAPIError, not_found_key: str) -> HTTPException:
    """4xx from the bot pass through with their {"error", "message"} body
    (e.g. 409 role_not_allowed from the self-role escalation check); only an
    unreachable / failing bot is a 502."""
    detail = exc.detail if isinstance(exc.detail, dict) else None
    if exc.status_code == 404:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": not_found_key})
    if exc.status_code in (400, 422):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail or {"error": "invalid_request"})
    if exc.status_code == 409:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail or {"error": "role_not_allowed", "message": "That role can't be a self-role."},
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"error": "internal_api_unavailable"},
    )


@router.get("/reaction-roles/panels")
async def list_panels(
    session: Session = Depends(require_permission(_PERMISSION)),
) -> list[dict]:
    try:
        return await internal_client.list_reaction_role_panels(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _error_response(exc, "panel_not_found")


@router.get("/reaction-roles/panels/{panel_id}")
async def get_panel(
    panel_id: int,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.get_reaction_role_panel(config.DASHBOARD_GUILD_ID, panel_id)
    except InternalAPIError as exc:
        raise _error_response(exc, "panel_not_found")


class CreatePanelBody(BaseModel):
    channel_id: int
    title: str = "React for a role!"
    exclusive: bool = False


@router.post("/reaction-roles/panels", status_code=status.HTTP_201_CREATED)
async def create_panel(
    body: CreatePanelBody,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.create_reaction_role_panel(
            config.DASHBOARD_GUILD_ID, body.channel_id, session.discord_user_id, body.title, body.exclusive,
        )
    except InternalAPIError as exc:
        raise _error_response(exc, "panel_not_found")


class UpdatePanelBody(BaseModel):
    title: str | None = None
    exclusive: bool | None = None


@router.patch("/reaction-roles/panels/{panel_id}")
async def update_panel(
    panel_id: int,
    body: UpdatePanelBody,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.update_reaction_role_panel(
            config.DASHBOARD_GUILD_ID, panel_id, title=body.title, exclusive=body.exclusive,
        )
    except InternalAPIError as exc:
        raise _error_response(exc, "panel_not_found")


@router.delete("/reaction-roles/panels/{panel_id}")
async def delete_panel(
    panel_id: int,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        await internal_client.delete_reaction_role_panel(config.DASHBOARD_GUILD_ID, panel_id)
    except InternalAPIError as exc:
        raise _error_response(exc, "panel_not_found")
    return {"removed": True}


@router.post("/reaction-roles/panels/{panel_id}/post")
async def post_panel(
    panel_id: int,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    """Sends (or re-sends) the panel message to Discord. Ordinary edits
    (title/exclusive/entries) do NOT need this — they auto-sync the live
    message in place via internal_api's update/entry routes. This is only
    needed for the very first post, or an explicit staff-initiated re-post."""
    try:
        return await internal_client.post_reaction_role_panel(config.DASHBOARD_GUILD_ID, panel_id)
    except InternalAPIError as exc:
        raise _error_response(exc, "panel_not_found")


class AddEntryBody(BaseModel):
    emoji: str
    role_id: int
    label: str | None = None


@router.post("/reaction-roles/panels/{panel_id}/entries", status_code=status.HTTP_201_CREATED)
async def add_entry(
    panel_id: int,
    body: AddEntryBody,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.add_reaction_role_entry(
            config.DASHBOARD_GUILD_ID, panel_id, body.emoji, body.role_id, body.label,
            actor_id=session.discord_user_id,
        )
    except InternalAPIError as exc:
        raise _error_response(exc, "panel_not_found")


@router.delete("/reaction-roles/panels/{panel_id}/entries/{entry_id}")
async def remove_entry(
    panel_id: int,
    entry_id: int,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        await internal_client.remove_reaction_role_entry(config.DASHBOARD_GUILD_ID, panel_id, entry_id)
    except InternalAPIError as exc:
        raise _error_response(exc, "entry_not_found")
    return {"removed": True}


@router.get("/channels")
async def list_channels(
    session: Session = Depends(require_permission(_PERMISSION)),
) -> list[dict]:
    """Text-channel picker for the panel-creation form. Gated on the same
    permission as the rest of this file since it's only used here today —
    if another page needs it later, this can be broadened to "staff_base"."""
    try:
        return await internal_client.list_guild_channels(config.DASHBOARD_GUILD_ID)
    except InternalAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        )


@router.get("/reaction-roles/roles")
async def list_roles(
    session: Session = Depends(require_permission(_PERMISSION)),
) -> list[dict]:
    """Role picker for the entry-editor form (which role an emoji maps to).

    Deliberately its OWN endpoint rather than reusing GET /api/permissions/roles
    (server/routes/permissions.py) — that one is gated by require_staff (full
    staff only, see its docstring on why: it's the permission-GRANTS page,
    which can create new staff-equivalent power). A moderator holding only
    "manage_reaction_roles" (and not full staff) should still be able to use
    this page, so this hits the same internal_api GET /guilds/{id}/roles data
    (services.internal_client.list_guild_roles — no DB query, straight off
    the live discord.Guild.roles) but gated on manage_reaction_roles instead.
    """
    try:
        # Each role carries `blocked_reason` for THIS user (null = may be
        # added as a self-role); the picker disables the others.
        return await internal_client.list_self_role_candidates(config.DASHBOARD_GUILD_ID, session.discord_user_id)
    except InternalAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        )
