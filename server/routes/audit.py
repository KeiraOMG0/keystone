"""
GET /api/audit           one page of the bot audit log, newest first
GET /api/audit/actions   distinct action names (+ counts) for the filter dropdown

Dashboard "Audit log" page (/dashboard/audit). Read-only, gated on the
view_audit bot permission (require_permission; staff pass it automatically),
proxying internal_api/routes/audit.py.

Query params of GET /api/audit (all optional, ANDed):
  action        exact action name (letters, digits, "_", ".", ":", "-")
  actor_id      Discord user id (string of digits; 0 = "unknown actor")
  target_id     id of the target (string of digits)
  target_type   member | role | channel | message | ...
  since, until  ISO 8601 datetimes; since inclusive, until exclusive, since < until
  before_id     cursor: the previous page's next_before_id
  limit         1..200 (default 50)
Bad input is a 422 (FastAPI validation body, or {"error": "invalid_range"}).

Errors from the bot: 400/404/422 keep their {"error", "message"} detail;
unreachable or not-ready bot -> 502 {"error": "internal_api_unavailable"}.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/audit", tags=["audit"])

MAX_LIMIT = 200
_ID_PATTERN = r"^\d{1,20}$"
_NAME_PATTERN = r"^[A-Za-z0-9_.:\-]{1,100}$"


def _translate(exc: InternalAPIError) -> HTTPException:
    code = exc.status_code
    detail = exc.detail if isinstance(exc.detail, dict) else None
    if code in (400, 404, 422) and detail is not None:
        return HTTPException(status_code=code, detail=detail)
    if code in (400, 422):
        return HTTPException(status_code=code, detail={"error": "invalid_request", "message": "Invalid filter."})
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


def _utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


@router.get("")
async def get_audit_log(
    action: str | None = Query(None, pattern=_NAME_PATTERN),
    actor_id: str | None = Query(None, pattern=_ID_PATTERN),
    target_id: str | None = Query(None, pattern=_ID_PATTERN),
    target_type: str | None = Query(None, pattern=r"^[A-Za-z0-9_]{1,50}$"),
    since: datetime.datetime | None = Query(None),
    until: datetime.datetime | None = Query(None),
    before_id: int | None = Query(None, ge=1, le=2**63 - 1),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    session: Session = Depends(require_permission("view_audit")),
) -> dict:
    for name, value in (("actor_id", actor_id), ("target_id", target_id)):
        if value is not None and int(value) > 2**63 - 1:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_id", "message": f"{name} is not a valid id."},
            )
    if since is not None and until is not None and _utc(since) >= _utc(until):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_range", "message": "The start of the date range must be before its end."},
        )
    params = {
        "action": action,
        "actor_id": actor_id,
        "target_id": target_id,
        "target_type": target_type,
        "since": _utc(since).isoformat() if since is not None else None,
        "until": _utc(until).isoformat() if until is not None else None,
        "before_id": before_id,
        "limit": limit,
    }
    try:
        return await internal_client.get_audit_log(config.DASHBOARD_GUILD_ID, params)
    except InternalAPIError as exc:
        raise _translate(exc) from exc


@router.get("/actions")
async def get_audit_actions(
    session: Session = Depends(require_permission("view_audit")),
) -> dict:
    try:
        return await internal_client.get_audit_actions(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _translate(exc) from exc
