"""
GET /api/dashboard/stats

Thin passthrough proving the full chain works end to end:
  browser -> this route -> require_permission("staff_base")
          -> services.internal_client.get_guild_stats
          -> Step 1's internal API (internal_api/routes/stats.py)
          -> live bot.py Guild cache

No transformation of the response — returned as-is from the internal API.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
async def get_stats(session: Session = Depends(require_permission("staff_base"))) -> dict:
    try:
        return await internal_client.get_guild_stats(config.DASHBOARD_GUILD_ID)
    except InternalAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        )


@router.get("/server-info")
async def get_server_info(session: Session = Depends(require_permission("staff_base"))) -> dict:
    """The `/serverinfo` facts (owner, created, boosts, channel/role counts)
    for the Overview page. Same gate as /stats."""
    try:
        return await internal_client.get_server_info(config.DASHBOARD_GUILD_ID)
    except InternalAPIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "internal_api_unavailable"},
        )
