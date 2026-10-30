"""
POST /api/public/client-errors   a visitor's browser reports a crash / uncaught error

PUBLIC (no login needed: a crash can happen before or without one) and
UNVERIFIED, so: rate limited per client IP, body capped at MAX_BODY_BYTES, the
CSRF Origin check applies (server/csrf.py), and the answer never echoes
anything (204 when stored, flat {"error": ...} otherwise). If the visitor is
signed in, their dashboard user id/username is attached from the session (never
from the body). Stored and scrubbed by server/services/error_reports.py; read on
the owner-only Debug page's Errors tab (server/routes/debug.py).

Sent by src/lib/errorReport.ts (AppErrorBoundary, window "error" /
"unhandledrejection", Vite "preloadError").
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from server.auth.dependencies import get_current_session
from server.auth.oauth import limiter
from server.db.models import Session
from server.routes.appeals import client_ip
from server.services import error_reports

router = APIRouter(prefix="/api/public/client-errors", tags=["public-client-errors"])

MAX_BODY_BYTES = 32 * 1024


class ClientErrorBody(BaseModel):
    kind: str = Field(..., max_length=32)
    name: str = Field("Error", max_length=1000)
    message: str = Field("", max_length=10000)
    stack: str | None = Field(None, max_length=20000)
    component_stack: str | None = Field(None, max_length=10000)
    path: str | None = Field(None, max_length=2000)


def _bad(code: str, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code})


@router.post("")
@limiter.limit("20/minute", key_func=client_ip)
async def report_client_error(request: Request, session: Session | None = Depends(get_current_session)):
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return _bad("too_large", 413)
    try:
        body = ClientErrorBody.model_validate(json.loads(raw or b"null"))
    except (ValueError, ValidationError):
        return _bad("invalid_request", 400)
    if body.kind not in error_reports.BROWSER_KINDS:
        return _bad("invalid_request", 400)
    await error_reports.record(
        source="browser",
        kind=body.kind,
        name=body.name,
        message=body.message,
        stack=body.stack,
        component_stack=body.component_stack,
        path=body.path,
        user_agent=request.headers.get("user-agent"),
        user_id=session.discord_user_id if session else None,
        username=session.discord_username if session else None,
    )
    return Response(status_code=204)
