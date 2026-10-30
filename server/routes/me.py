"""
GET /api/me

The route the frontend calls first to figure out what to render — so it
uses require_session (NOT require_permission), since permission-gating this
route would create a chicken-and-egg problem (you'd need to already know
your permissions to find out your permissions).

Response states:
  - No session                          -> 401 {"error": "unauthenticated"}
  - Session but not a guild member      -> 403 {"error": "not_a_member"}
    (also when the bot answers 404 "Member ... not found": left/kicked/banned
    since login; any other bot failure is the transient 502
    internal_api_unavailable, which never logs the user out)
  - Authenticated + guild member        -> 200 with identity + role_ids +
                                            a bulk permission check against
                                            services.permission_service.VALID_PERMISSIONS.

Design choice: rather than have the frontend make N separate calls to
/api/dashboard/... routes just to find out what nav items to show, /api/me
eagerly resolves the full permission set in one internal-API round trip
(check-bulk) using the bot's own VALID_PERMISSIONS list as the canonical
set of permission strings that exist. This keeps permission-string
ownership in one place (services/permission_service.py) instead of
duplicating that list in the dashboard backend.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from server import config
from server.auth.dependencies import require_session
from server.db.models import Session
from server.permissions.guards import NOT_A_MEMBER_DETAIL, is_owner_session, member_gone
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api", tags=["me"])

# Mirrors services/permission_service.py's VALID_PERMISSIONS. Duplicated
# here (as a plain string list, not an import) because server/ deliberately
# has no dependency on the bot's Python package (cogs/, services/) — the
# internal API is the only sanctioned boundary between the two processes.
_ALL_PERMISSIONS = [
    "manage_templates",
    "manage_events",
    "manage_tickets",
    "manage_schedules",
    "manage_autorole",
    "manage_reaction_roles",
    "manage_qotd",
    "view_audit",
    "kick_members",
    "staff_base",
    "verify_members",
    "moderate_members",
    "ban_members",
    "manage_messages_bulk",
    "manage_birthdays",
    "control_music",
]


@router.get("/me")
async def get_me(session: Session = Depends(require_session)) -> dict:
    if not session.is_guild_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "not_a_member"},
        )

    try:
        permissions = await internal_client.check_permissions_bulk(
            config.DASHBOARD_GUILD_ID, session.discord_user_id, _ALL_PERMISSIONS
        )
    except InternalAPIError as exc:
        if member_gone(exc):
            # The bot is up and says this user left / was kicked / was banned:
            # a verdict (the frontend's not-a-member screen), never "bot offline".
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_MEMBER_DETAIL)
        # Anything else stays the transient 502 (the frontend never logs out on it).
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        )

    # Staff = owner or a STAFF_ROLE_IDS role: the same check require_staff makes.
    # Unknown (the is-staff call failed) counts as false; it only hides nav.
    try:
        is_staff = await internal_client.check_is_staff(config.DASHBOARD_GUILD_ID, session.discord_user_id) is True
    except InternalAPIError:
        is_staff = False
    is_staff = is_staff or is_owner_session(session)

    return {
        "user": {
            "id": str(session.discord_user_id),
            "username": session.discord_username,
            "avatar": session.discord_avatar,
        },
        "guild_id": str(config.DASHBOARD_GUILD_ID),
        "role_ids": [str(r) for r in session.cached_role_ids],
        "permissions": permissions,
        # Owner-only Debug page: the frontend shows its nav link/route only when
        # true; server/routes/debug.py enforces it (404 for everyone else).
        "is_owner": is_owner_session(session),
        # Staff (owner or STAFF_ROLE_IDS, same check as require_staff): the
        # frontend hides staff-only nav (Permissions, Settings, Roles,
        # Diagnostics) for everyone else. The routes still enforce it.
        "is_staff": is_staff,
    }
