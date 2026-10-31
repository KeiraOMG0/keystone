"""
Member hub (/hub): the signed-in person's OWN view of the server.

GET  /api/hub                                         require_session (any Discord account)
POST /api/hub/self-roles {"panel_id", "role_id", "assign"}   require_session, 20/min

Sign-in is required (the page shows a "Continue with Discord" gate; the login
comes back to /hub via /api/auth/login?next=/hub). Membership is NOT required:
the bot decides it live on every request, so the hub can tell someone who isn't
in the server why (removed by the trap channel -> their appeal status, banned,
or not a member). `user_id` sent to the bot is always the session user, never a
value from the request, so nobody can look at another person's hub.

What the bot returns is described in services/member_hub_service.py (own
profile, warning count without reasons, self-roles, birthday, open tickets,
events, QOTD, music). Refusals from the bot are {"error", "message"} details
passed through with their status; 502 internal_api_unavailable only when the
bot can't answer.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from server import config
from server.auth.dependencies import require_session
from server.auth.oauth import limiter
from server.db.models import Session
from server.routes.appeals import client_ip
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/hub", tags=["hub"])

_SNOWFLAKE = dict(ge=1, lt=2**63)


def _translate(exc: InternalAPIError) -> HTTPException:
    detail = exc.detail if isinstance(exc.detail, dict) else None
    if detail is not None and exc.status_code in (403, 404, 409, 503):
        return HTTPException(status_code=exc.status_code, detail=detail)
    if detail is not None and exc.status_code == 502 and detail.get("error") == "discord_error":
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


@router.get("")
async def hub(session: Session = Depends(require_session)) -> dict:
    try:
        return await internal_client.get_member_hub(config.DASHBOARD_GUILD_ID, session.discord_user_id)
    except InternalAPIError as exc:
        raise _translate(exc)


class ToggleBody(BaseModel):
    panel_id: int = Field(ge=1)
    role_id: int = Field(**_SNOWFLAKE)
    assign: bool


@router.post("/self-roles")
@limiter.limit("20/minute", key_func=client_ip)
async def toggle_self_role(
    request: Request, body: ToggleBody, session: Session = Depends(require_session),
) -> dict:
    try:
        return await internal_client.toggle_hub_self_role(
            config.DASHBOARD_GUILD_ID, session.discord_user_id, body.panel_id, body.role_id, body.assign,
        )
    except InternalAPIError as exc:
        raise _translate(exc)
