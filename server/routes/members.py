"""
GET  /api/members/search?q=&limit=1..25          staff_base
GET  /api/members/verify-channels                 verify_members
GET  /api/members/{user_id}/profile               staff_base
POST /api/members/{user_id}/verify                verify_members   {"channel_id"}

The dashboard member profile (a richer `/userinfo`) and `!verify`.

Gates match the bot:
  - The profile (account facts, membership, roles, timeout state) is
    staff_base. Its records follow the Discord/website gates: warnings
    (`!warnings`) and the honeypot record need moderate_members, the
    mod-action history any of moderate_members / kick_members / ban_members
    (same as GET /api/moderation/history). Withheld sections are null and
    named in `restricted`.
  - Verify needs `verify_members` (same as `!verify` and the ticket "Verify &
    Close" button).
The actions on the profile (warn, timeout, kick, ban, unban, remove warning)
use the existing /api/moderation/* routes with their own gates.

The acting user is always the session user. The bot's {"error", "message"}
refusals pass through with their status (server/routes/moderation._translate);
502 internal_api_unavailable only when the bot cannot answer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.routes.moderation import MEMBER_READ_PERMISSIONS, _translate
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/members", tags=["members"])

_SNOWFLAKE = dict(ge=1, lt=2**63)


def _guild() -> int:
    return config.DASHBOARD_GUILD_ID


@router.get("/search")
async def search(
    q: str = Query("", max_length=100),
    limit: int = Query(20, ge=1, le=25),
    session: Session = Depends(require_permission("staff_base")),
) -> dict:
    try:
        return await internal_client.search_members(_guild(), q, limit)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.get("/verify-channels")
async def verify_channels(session: Session = Depends(require_permission("verify_members"))) -> dict:
    try:
        return await internal_client.get_verify_channels(_guild())
    except InternalAPIError as exc:
        raise _translate(exc)


@router.get("/{user_id}/profile")
async def profile(
    user_id: int = Path(**_SNOWFLAKE),
    session: Session = Depends(require_permission("staff_base")),
) -> dict:
    try:
        perms = await internal_client.check_permissions_bulk(
            _guild(), session.discord_user_id, list(MEMBER_READ_PERMISSIONS)
        )
        can_moderate = bool(perms.get("moderate_members"))
        data = await internal_client.get_member_profile(
            _guild(), user_id, actor_id=session.discord_user_id, include_honeypot=can_moderate,
        )
    except InternalAPIError as exc:
        raise _translate(exc)
    # Records follow the Discord gates: warnings (`!warnings`) and the
    # honeypot record need moderate_members; the mod-action history any of
    # moderate/kick/ban (same as GET /api/moderation/history). Withheld
    # sections are null and listed in `restricted`.
    restricted = []
    if not can_moderate:
        data["warnings"] = None
        data["honeypot"] = None
        restricted += ["warnings", "honeypot"]
    if not any(perms.get(p) for p in MEMBER_READ_PERMISSIONS):
        data["history"] = None
        restricted.append("history")
    data["restricted"] = restricted
    return data


class VerifyBody(BaseModel):
    channel_id: int = Field(**_SNOWFLAKE)


@router.post("/{user_id}/verify")
async def verify(
    body: VerifyBody,
    user_id: int = Path(**_SNOWFLAKE),
    session: Session = Depends(require_permission("verify_members")),
) -> dict:
    try:
        return await internal_client.verify_member(
            _guild(), user_id, actor_id=session.discord_user_id, channel_id=body.channel_id,
        )
    except InternalAPIError as exc:
        raise _translate(exc)

