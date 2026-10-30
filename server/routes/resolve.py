"""
POST /api/resolve

Thin, permission-gated client of internal_api/routes/resolve.py, following
the exact same shape as server/routes/reaction_roles.py: require_permission
first, then delegate to services.internal_client, translating
InternalAPIError into the same {"error": "internal_api_unavailable"} 502
body used elsewhere.

Gated on "holds ANY bot permission" (one check-bulk call over the full
permission list via require_any_bot_permission, staff pass): resolving IDs to names is read-only metadata
needed by nearly every dashboard page (Moderation = moderate_members,
Tickets = manage_tickets, SelfRoles, QOTD, ...). It used to require
staff_base, which 403'd a role holding only e.g. moderate_members, so those
pages showed raw IDs (permission review finding, 2026-10-28).

Ids are deduped here and each list is capped at MAX_IDS_PER_LIST (400
{"error": "invalid_request"} beyond it, before any bot call); the bot side
resolves cache-first and limits its own Discord fetches (see
internal_api/routes/resolve.py).

No 404 case to translate: a partial miss (e.g. a user who left the guild)
comes back as a null entry in the response body, not an HTTP error — see
internal_api/routes/resolve.py's docstring.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from server import config
from server.db.models import Session
from server.permissions.guards import require_any_bot_permission, require_staff
from server.routes.me import _ALL_PERMISSIONS
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api", tags=["resolve"])

# Any one of the bot's permission strings (server/routes/me.py mirrors VALID_PERMISSIONS).
_REQUIRE_ANY = require_any_bot_permission(_ALL_PERMISSIONS)
MAX_IDS_PER_LIST = 100  # matches the internal API's _MAX_BATCH


def _clean(ids: list[str]) -> list[int]:
    """Dedupe (order kept) and parse; ValueError on a non-numeric id or too many."""
    out = list(dict.fromkeys(int(str(i).strip()) for i in ids))
    if len(out) > MAX_IDS_PER_LIST:
        raise ValueError("too many ids")
    return out


class ResolveBody(BaseModel):
    # max_length only bounds parsing; the real cap (after dedupe) is MAX_IDS_PER_LIST.
    user_ids: list[str] = Field(default_factory=list, max_length=1000)
    role_ids: list[str] = Field(default_factory=list, max_length=1000)
    channel_ids: list[str] = Field(default_factory=list, max_length=1000)


@router.post("/resolve")
async def resolve_ids(
    body: ResolveBody,
    session: Session = Depends(_REQUIRE_ANY),
) -> dict:
    try:
        user_ids, role_ids, channel_ids = _clean(body.user_ids), _clean(body.role_ids), _clean(body.channel_ids)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_request"})
    try:
        return await internal_client.resolve_ids(config.DASHBOARD_GUILD_ID, user_ids, role_ids, channel_ids)
    except InternalAPIError as exc:
        if exc.status_code == 400:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_request"})
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


# ---------------------------------------------------------------------------
# User cache (remembered profiles of departed users; internal_api/routes/resolve.py)
#   GET    /api/user-cache/{user_id}  any bot permission (same read-only metadata as /api/resolve)
#   DELETE /api/user-cache/{user_id}  require_staff (forget one user; audited bot-side
#                                     as user_cache_forget with the session user as actor)
# ---------------------------------------------------------------------------

_SNOWFLAKE_MAX = 2**63 - 1


def _user_id(raw: str) -> int:
    raw = raw.strip()
    if not raw.isdigit() or not (0 < int(raw) <= _SNOWFLAKE_MAX):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "invalid_request"})
    return int(raw)


def _user_cache_error(exc: InternalAPIError) -> HTTPException:
    if exc.status_code == 404:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_cached"})
    if exc.status_code == 503 and isinstance(exc.detail, dict) and exc.detail.get("error") == "user_cache_unavailable":
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "user_cache_unavailable", "message": "The user cache is off or not loaded."},
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


@router.get("/user-cache/{user_id}")
async def get_cached_user(
    user_id: str,
    session: Session = Depends(_REQUIRE_ANY),
) -> dict:
    uid = _user_id(user_id)
    try:
        return await internal_client.get_cached_user(config.DASHBOARD_GUILD_ID, uid)
    except InternalAPIError as exc:
        raise _user_cache_error(exc)


@router.delete("/user-cache/{user_id}")
async def forget_cached_user(
    user_id: str,
    session: Session = Depends(require_staff),
) -> dict:
    uid = _user_id(user_id)
    try:
        return await internal_client.forget_cached_user(config.DASHBOARD_GUILD_ID, uid, session.discord_user_id)
    except InternalAPIError as exc:
        raise _user_cache_error(exc)
