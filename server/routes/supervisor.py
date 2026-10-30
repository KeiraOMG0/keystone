"""
/api/supervisor/*  (STAFF ONLY) - restart controls for the Diagnostics page.

  GET  /api/supervisor/status            supervisor + per-service state
  POST /api/supervisor/restart           {"target": bot|dashboard|tunnel|all, "reason"?}
  GET  /api/supervisor/logs?service=&lines=   in-memory output tail (scrubbed)
  POST /api/supervisor/rotate-logs       {"reason"?}  OWNER ONLY (require_owner: the
                                         unknown-/api 404 to everyone else, whatever the
                                         method or body); 202 {"queued", "message"},
                                         the result shows up in /status log_rotation.last_run

DELIBERATE EXCEPTION to "the dashboard only calls the internal API": these
routes call supervisor.py's loopback control API directly
(server/services/supervisor_client.py), so restarting works while the bot is
down or hung. For the same reason the gate is diagnostics'
`require_staff_even_if_bot_down`: normal require_staff (live check through the
bot), falling back to the bot's own staff rule (OWNER_ID / STAFF_ROLE_IDS) on
the roles cached at login ONLY when the bot cannot answer (connection error or
timeout, a 5xx, or the guild not cached yet during startup). A member the bot
reports as gone (kicked/banned since login) gets 403, not the fallback.

requested_by always comes from the session ("username (discord id)"), source is
"dashboard". The CSRF Origin check (server/csrf.py, global middleware) covers
the POST. Restarting the dashboard itself is safe: the supervisor queues the
request with a short delay, so this response is sent before the kill.

Errors: supervisor not reachable -> 503 {"error": "supervisor_unavailable"};
429 cooldown and 409 conflicts pass through with the supervisor's own short
message; anything else -> 502 {"error": "supervisor_error"}.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from server.db.models import Session
from server.permissions.guards import owner_body, require_owner
from server.routes.diagnostics import require_staff_even_if_bot_down, scrub
from server.services import supervisor_client
from server.services.supervisor_client import SupervisorError, SupervisorUnavailable

router = APIRouter(prefix="/api/supervisor", tags=["supervisor"])

_CONFLICT_ERRORS = {"restart_in_progress", "not_managed", "rotation_in_progress"}


class RestartBody(BaseModel):
    target: Literal["bot", "dashboard", "tunnel", "all"]
    reason: str | None = Field(default=None, max_length=300)


class RotateLogsBody(BaseModel):
    reason: str | None = Field(default=None, max_length=300)


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, SupervisorUnavailable):
        return HTTPException(status_code=503, detail={
            "error": "supervisor_unavailable",
            "message": "Not running under supervisor.py (or it is an older copy without the control API).",
        })
    assert isinstance(exc, SupervisorError)
    body = exc.body or {}
    message = str(body.get("message") or "")[:400]
    if exc.status_code == 429:
        try:
            retry = max(1, int(body.get("retry_after_s") or 1))
        except (TypeError, ValueError):
            retry = 30
        return HTTPException(
            status_code=429,
            detail={"error": "cooldown", "retry_after_s": retry, "message": message or f"Try again in {retry}s."},
            headers={"Retry-After": str(retry)},
        )
    if exc.status_code == 409:
        error = body.get("error") if body.get("error") in _CONFLICT_ERRORS else "conflict"
        return HTTPException(status_code=409, detail={"error": error, "message": message})
    if exc.status_code == 404:
        return HTTPException(status_code=404, detail={"error": "unknown_service"})
    if exc.status_code == 400:
        return HTTPException(status_code=400, detail={"error": "invalid_request", "message": message})
    return HTTPException(status_code=502, detail={"error": "supervisor_error"})


@router.get("/status")
async def get_status(session: Session = Depends(require_staff_even_if_bot_down)) -> dict:
    try:
        return scrub(await supervisor_client.get_status())
    except (SupervisorUnavailable, SupervisorError) as exc:
        raise _translate(exc)


@router.post("/restart", status_code=202)
async def restart(body: RestartBody, session: Session = Depends(require_staff_even_if_bot_down)) -> dict:
    requested_by = f"{session.discord_username or 'unknown'} ({session.discord_user_id})"
    try:
        result = await supervisor_client.request_restart(
            body.target, requested_by=requested_by, source="dashboard", reason=(body.reason or "").strip() or None
        )
    except (SupervisorUnavailable, SupervisorError) as exc:
        raise _translate(exc)
    return {
        "accepted": list(result.get("accepted") or []),
        "skipped": [
            {"name": s.get("name"), "error": s.get("error"), "message": s.get("message")}
            for s in (result.get("skipped") or []) if isinstance(s, dict)
        ],
        "delay_s": result.get("delay_s"),
        "eta_s": result.get("eta_s"),
    }


@router.get("/logs")
async def get_logs(
    service: Literal["bot", "dashboard", "tunnel"],
    lines: int = Query(default=80, ge=1, le=200),
    session: Session = Depends(require_staff_even_if_bot_down),
) -> dict:
    try:
        return scrub(await supervisor_client.get_logs(service, lines))
    except (SupervisorUnavailable, SupervisorError) as exc:
        raise _translate(exc)


@router.post("/rotate-logs", status_code=202)
async def rotate_logs(request: Request, session: Session = Depends(require_owner)) -> dict:
    """Owner only. require_owner raises HiddenRouteNotFound (a bare 404, the same
    bytes as an unknown /api/* path) for everyone else. The body is NOT declared
    as a parameter: FastAPI would parse it before any dependency and answer a
    malformed one with 422 for everybody, so it is read raw and validated only
    after require_owner (owner_body; invalid -> 422 for the owner only). A wrong
    method (GET, PUT, ...) falls through to server/app.py's /api catch-all, the
    same bare 404. Needs no bot call, so it works while the bot is down. Like
    /restart, the record is supervisor.log's "accepted log rotation (requested
    by ...)" line (the supervisor flattens reason/requested_by to one line)."""
    body = await owner_body(request, RotateLogsBody)
    assert isinstance(body, RotateLogsBody)
    requested_by = f"{session.discord_username or 'unknown'} ({session.discord_user_id})"
    reason = (body.reason or "").strip() or None
    try:
        result = await supervisor_client.rotate_logs(requested_by=requested_by, source="dashboard", reason=reason)
    except (SupervisorUnavailable, SupervisorError) as exc:
        raise _translate(exc)
    return {"queued": bool(result.get("queued", True)), "message": str(result.get("message") or "")[:400]}
