"""
GET  /api/fortnite/status     config, last posted, next post time, cache, wishlist counts
PUT  /api/fortnite/config     channel / enabled / jam tracks / ping role
POST /api/fortnite/post-now   post today's shop to the configured channel (dedupe untouched)
GET  /api/fortnite/channels   text-channel picker
GET  /api/fortnite/roles      role picker (ping role)

Fortnite shop settings card on the dashboard. Gated server-side on
`manage_schedules`, the same grant cogs/fortnite_shop.py's /fortnite config
commands check (staff/owner bypass as everywhere). Thin client of
internal_api/routes/fortnite.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/fortnite", tags=["fortnite"])

_PERMISSION = "manage_schedules"
_SAFE_DETAILS = {"channel_not_found", "role_not_found", "role_not_pingable", "channel_not_set", "discord_forbidden"}


def _error(exc: InternalAPIError) -> HTTPException:
    detail = exc.detail if isinstance(exc.detail, str) else None
    if exc.status_code in (400, 403) and detail in _SAFE_DETAILS:
        return HTTPException(status_code=exc.status_code, detail={"error": detail})
    if exc.status_code == 502 and detail and detail.startswith("shop_unavailable"):
        return HTTPException(status_code=502, detail={"error": "shop_unavailable", "message": detail})
    if exc.status_code == 503:
        return HTTPException(status_code=503, detail={"error": "feature_unavailable"})
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


@router.get("/status")
async def get_status(session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    try:
        return await internal_client.get_fortnite_status(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _error(exc)


class FortniteConfigBody(BaseModel):
    # Snowflakes arrive as strings from the browser (never Number()).
    channel_id: str | None = None
    clear_channel: bool = False
    enabled: bool | None = None
    include_jam_tracks: bool | None = None
    ping_role_id: str | None = None
    clear_ping_role: bool = False


def _snowflake(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    if not value.isdigit():
        raise HTTPException(status_code=400, detail={"error": "invalid_request"})
    return int(value)


@router.put("/config")
async def put_config(body: FortniteConfigBody, session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    values = {
        "channel_id": _snowflake(body.channel_id),
        "clear_channel": body.clear_channel,
        "enabled": body.enabled,
        "include_jam_tracks": body.include_jam_tracks,
        "ping_role_id": _snowflake(body.ping_role_id),
        "clear_ping_role": body.clear_ping_role,
    }
    try:
        return await internal_client.update_fortnite_config(config.DASHBOARD_GUILD_ID, values)
    except InternalAPIError as exc:
        raise _error(exc)


@router.post("/post-now")
async def post_now(session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    try:
        return await internal_client.fortnite_post_now(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _error(exc)


@router.get("/channels")
async def channels(session: Session = Depends(require_permission(_PERMISSION))) -> list[dict]:
    try:
        return await internal_client.list_guild_channels(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _error(exc)


@router.get("/roles")
async def roles(session: Session = Depends(require_permission(_PERMISSION))) -> list[dict]:
    try:
        return await internal_client.list_guild_roles(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _error(exc)
