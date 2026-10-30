"""
Defense-in-depth CSRF protection for state-changing routes.

Primary protection is SameSite=Lax on the session cookie (chosen over
Strict specifically because Strict would drop the cookie on Discord's
top-level cross-site redirect back to /api/auth/callback). Lax still blocks
the cookie being sent on cross-site POST/PUT/PATCH/DELETE requests from
another site's JS, but as defense-in-depth per the approved design we also
check Origin/Referer on non-GET requests.

There aren't many state-changing routes yet in this step (auth/callback is
a GET per the OAuth spec) — this middleware mainly future-proofs the
pattern for later steps (ticket/moderation/etc. routes) to inherit for
free by being mounted on the app.
"""

from __future__ import annotations

from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from server import config

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _allowed_origins() -> set[str]:
    origins = set()
    for url in (config.FRONTEND_BASE_URL, f"http://localhost:{config.DASHBOARD_PORT}", f"http://127.0.0.1:{config.DASHBOARD_PORT}"):
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            origins.add(f"{parsed.scheme}://{parsed.netloc}")
    return origins


def allowed_origins() -> set[str]:
    """Public accessor: the single allow-list used by the CSRF middleware AND
    the browser websocket handshake (server/live/endpoint.py)."""
    return _allowed_origins()


class CSRFOriginCheckMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            source = origin or referer

            if source:
                parsed = urlparse(source)
                source_origin = f"{parsed.scheme}://{parsed.netloc}"
                if source_origin not in _allowed_origins():
                    return JSONResponse(
                        status_code=403,
                        content={"error": "csrf_check_failed"},
                    )
            # No Origin/Referer header at all: same-site browser requests
            # normally include one of these for non-GET requests; we don't
            # hard-fail its total absence here (some legitimate same-origin
            # requests, e.g. certain non-browser clients, may omit it), but
            # SameSite=Lax already prevents the cookie from being attached
            # in the cross-site case that matters.

        return await call_next(request)
