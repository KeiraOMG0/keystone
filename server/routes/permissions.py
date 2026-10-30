"""
GET    /api/permissions/roles
GET    /api/permissions/grants
POST   /api/permissions/grants
DELETE /api/permissions/grants

Thin, permission-gated client of Step 1's internal API permission-grants
routes (internal_api/routes/permissions.py), following the exact same shape
as server/routes/moderation.py: require_*(...) first, then delegate to
services.internal_client, translating InternalAPIError into a 502 with the
same {"error": "internal_api_unavailable"} body used elsewhere.

Gating: require_staff (server/permissions/guards.py), NOT require_permission.
This page manages the permission-GRANT system itself — which Discord roles
hold which of the 14 VALID_PERMISSIONS strings (services/permission_service.
py). Gating it on a granted permission (e.g. a "manage_permissions" string)
would let any role holding that grant self-escalate: grant itself more
permissions, or grant other roles permissions, with no ceiling. require_staff
instead requires the caller to be actual is_staff() — the server owner or a
DEFAULT_STAFF_ROLE_IDS role, both fixed at deploy/config time and NOT
editable through this page's grants table. See guards.py's require_staff
docstring for the full reasoning.

POST populates `granted_by` and DELETE populates `actor_id` from
`session.discord_user_id` — same "acting user always comes from the session,
never the request body" convention as server/routes/moderation.py's
moderator_id. The bot's PermissionService turns them into the actor of the
permission_grant / permission_revoke audit_log rows (Audit log page).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server import config
from server.db.models import Session
from server.permissions.guards import require_staff
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/permissions", tags=["permissions"])


@router.get("/roles")
async def get_roles(
    session: Session = Depends(require_staff),
) -> dict:
    try:
        roles = await internal_client.list_guild_roles(config.DASHBOARD_GUILD_ID)
    except InternalAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        )
    return {"roles": roles}


@router.get("/grants")
async def get_grants(
    session: Session = Depends(require_staff),
) -> dict:
    try:
        return await internal_client.list_permission_grants(config.DASHBOARD_GUILD_ID)
    except InternalAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        )


class GrantBody(BaseModel):
    role_id: int
    permission: str


@router.post("/grants", status_code=status.HTTP_201_CREATED)
async def add_grant(
    body: GrantBody,
    session: Session = Depends(require_staff),
) -> dict:
    try:
        return await internal_client.grant_permission(
            config.DASHBOARD_GUILD_ID, body.role_id, body.permission, session.discord_user_id
        )
    except InternalAPIError as exc:
        if exc.status_code == 400:
            # Bot-side validation (e.g. unknown permission): keep its readable
            # {"error", "message"} detail, never a 502.
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "invalid_grant", "message": str(detail.get("message") or "Invalid grant.")},
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        )


class RevokeBody(BaseModel):
    role_id: int
    permission: str


@router.delete("/grants")
async def remove_grant(
    body: RevokeBody,
    session: Session = Depends(require_staff),
) -> dict:
    try:
        return await internal_client.revoke_permission(
            config.DASHBOARD_GUILD_ID, body.role_id, body.permission, actor_id=session.discord_user_id
        )
    except InternalAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        )
