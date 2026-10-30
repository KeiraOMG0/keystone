"""
PUBLIC, UNAUTHENTICATED ban-appeal routes (banned people cannot log in):

GET  /api/public/appeals/{token}  -> {user_id, username, status, submitted_at, reviewed_at,
                                      rejoin_invite_url (approved only), rejoined}
POST /api/public/appeals          -> always 403 {"error": "signin_required"}

Submitting an appeal REQUIRES Discord sign-in (2026-10-29): it goes through
server/routes/appeal_auth.py (`POST /api/public/appeal-auth/submit`, identity
proven by Discord). This POST is retired: the typed-User-ID form and the
DM-link token can no longer file an appeal. The GET lookup stays, read-only, so
the DM link still shows the person their own status/outcome.

Safety properties (see docs/honeypot_contract.md):
  - No session/permission dependency by design. The CSRF Origin check still
    applies to the POST via server/csrf.py (same-origin from
    FRONTEND_BASE_URL or localhost).
  - Rate limited per client IP (CF-Connecting-IP when behind the
    Cloudflare Tunnel, else the socket address): 5/min POST, 30/min GET.
    Exceeding it yields {"error": "rate_limited"} (see app.py's handler).
  - The token is shape-checked before it reaches the bot (malformed ->
    not_eligible, the same answer as an unknown token).
  - Internal errors/detail are NEVER echoed. Failures map to one of
    {"error": "not_eligible" | "already_submitted" | "rate_limited" |
    "unavailable" | "signin_required"} (plus "invalid_request" for malformed
    input). The GET lookup answers not_eligible identically for malformed,
    unknown and used tokens.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server import config
from server.auth.oauth import limiter
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/public/appeals", tags=["public-appeals"])

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")

_ERROR_STATUS = {
    "not_eligible": 404,
    "already_submitted": 409,
    "rate_limited": 429,
    "invalid_request": 422,
    "unavailable": 503,
    "signin_required": 403,
}


def public_error(code: str) -> JSONResponse:
    return JSONResponse(status_code=_ERROR_STATUS[code], content={"error": code})


def client_ip(request: Request) -> str:
    """Rate-limit key: Cloudflare's real-client header if present, else the
    socket peer. (Only reachable via the tunnel/localhost, so the header is
    trusted; a direct caller spoofing it only gets its own bucket.)"""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()[:64]
    return request.client.host if request.client else "unknown"


def _map_internal(exc: InternalAPIError) -> JSONResponse:
    if exc.status_code == 404:
        return public_error("not_eligible")
    if exc.status_code == 409:
        return public_error("already_submitted")
    if exc.status_code == 429:
        return public_error("rate_limited")
    return public_error("unavailable")


@router.get("/{token}")
@limiter.limit("30/minute", key_func=client_ip)
async def lookup(token: str, request: Request):
    if not _TOKEN_RE.match(token):
        return public_error("not_eligible")
    try:
        data = await internal_client.lookup_appeal_token(config.DASHBOARD_GUILD_ID, token)
    except InternalAPIError as exc:
        return _map_internal(exc)
    # Whitelist fields - never pass through anything extra. The token is the person's
    # own DM link, so their own outcome may be shown (never the reviewer or staff note).
    return {
        "user_id": str(data.get("user_id")),
        "username": data.get("username"),
        "status": data.get("status"),
        "submitted_at": data.get("submitted_at"),
        "reviewed_at": data.get("reviewed_at"),
        "rejoin_invite_url": data.get("rejoin_invite_url") if data.get("status") == "approved" else None,
        "rejoined": bool(data.get("rejoined")),
    }


@router.post("")
@limiter.limit("5/minute", key_func=client_ip)
async def submit(request: Request):
    """Retired (2026-10-29): appeals require Discord sign-in, so nothing is
    accepted here - not the typed-User-ID form, not the DM-link token. Every
    body gets the same 403 {"error": "signin_required"} (no oracle, nothing
    reaches the bot); the page sends people to /api/public/appeal-auth/login and
    submits through POST /api/public/appeal-auth/submit. Kept as a route so an
    old cached page gets a clear answer instead of the generic 404."""
    return public_error("signin_required")
