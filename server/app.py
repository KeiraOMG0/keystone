"""
FastAPI application factory for the dashboard backend.

A SEPARATE process from internal_api/ (which is embedded in the bot
process). This app:
  - Serves Discord OAuth2 login/callback (server/auth/oauth.py).
  - Serves /api/me and /api/dashboard/stats as thin, session/permission
    gated clients of Step 1's internal API.
  - Same-origin design: the built frontend (server/frontend/dist, produced
    by `npm run build` in server/frontend/) is mounted as static files by
    THIS SAME process, so there is no CORS to configure — one process, one
    port (server/config.py's DASHBOARD_PORT, default 8080).

Run with: uvicorn server.app:app --host 127.0.0.1 --port 8080 (from repo root)

Local frontend development (before a production build exists) still works
by running Vite's own dev server separately (`npm run dev` in
server/frontend/, typically port 5173) — vite.config.ts proxies its /api/*
requests to this backend. But the deliverable this step verifies against is
the PRODUCTION build served here, since that's what proves the "one
process, one port" design actually works end to end.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, PlainTextResponse, Response

from server import config
from server.auth.oauth import limiter, router as auth_router
from server.csrf import CSRFOriginCheckMiddleware
from server.live import endpoint as live_endpoint
from server.live.hub import hub as live_hub
from server.permissions.guards import HiddenRouteNotFound  # owner-only Debug page 404 (see create_app)
from server.routes import appeal_auth, appeals, diagnostics, honeypot, me, moderation, permissions, qotd, reaction_roles, resolve, settings, stats, status, templates, ticket_panels, tickets
from server.routes import role_admin  # dashboard Roles page (/api/roles)
from server.routes import debug, fortnite  # Fortnite shop card (/api/fortnite) + owner-only Debug page (/api/debug)
from server.routes import supervisor  # restart controls via supervisor.py (/api/supervisor), calls the supervisor directly
from server.routes import audit  # Audit log page (/api/audit, view_audit)
from server.routes import automation  # Automation page (/api/automation: events, welcome/boost, schedules, autoroles, managed)
from server.routes import members  # member profile + verify (/api/members)
from server.routes import birthdays  # Birthdays page (/api/birthdays, manage_birthdays)
from server.routes import music  # Music page (/api/music, control_music; staff for status details/debugurl)
from server.routes import client_errors  # public browser crash reports (/api/public/client-errors)
from server.services.error_reports import ErrorCaptureMiddleware  # unhandled exceptions -> Debug > Errors

# server/app.py -> server/ -> server/frontend/dist
_FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"

_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

# Browser/Cloudflare caching of the built frontend. Vite puts a content hash in
# every /assets file name, so those never change and may be cached for a year
# (Cloudflare also keeps them at the edge). index.html is the one file that
# changes on every build: "no-cache" makes browsers check for a new one on every
# load, so nobody keeps an old index.html pointing at deleted chunks. Other
# files at the dist root (favicon etc.) get a day. Errors are never cached.
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_INDEX = "no-cache"
CACHE_ROOT_FILE = "public, max-age=86400"
CACHE_NONE = "no-store"


# --------------------------------------------------------------------------
# Security headers (security audit 2026-10-30). Set on every HTTP response:
# no framing (clickjacking), no MIME sniffing, no cross-site Referer (an
# /appeal/<token> URL must not leak to Discord when its sign-in link is
# followed), no camera/mic/location, and HSTS when the browser is on HTTPS.
# No script CSP: it would need a full inventory of the built bundle first.
# --------------------------------------------------------------------------
SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"content-security-policy": b"frame-ancestors 'none'",
    b"referrer-policy": b"same-origin",
    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
}
HSTS_VALUE = b"max-age=15552000"
_LOOPBACK_PEERS = {"127.0.0.1", "::1", "localhost"}


def _is_https(scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    client = scope.get("client") or ("", 0)
    if client[0] in _LOOPBACK_PEERS:  # cloudflared runs on this machine
        for key, value in scope.get("headers") or []:
            if key == b"x-forwarded-proto":
                return value.split(b",")[0].strip().lower() == b"https"
    return False


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = _is_https(scope)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                for key, value in SECURITY_HEADERS.items():
                    if key not in present:
                        headers.append((key, value))
                if https and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", HSTS_VALUE))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# uvicorn's access log writes the full request line, including OAuth `code` /
# `state` query values and raw appeal-link tokens in paths, to
# logs/supervisor/dashboard.log (kept up to 28 days). Scrub them at the source.
_ACCESS_QUERY_SECRET_RE = re.compile(r"([?&](?:code|state|token|access_token|refresh_token)=)[^&#\s]+", re.IGNORECASE)
_ACCESS_APPEAL_TOKEN_RE = re.compile(r"(/(?:api/public/appeals|appeal)/)(?!for-user\b|submit\b)[A-Za-z0-9_\-]{8,128}")


def scrub_access_path(path: str) -> str:
    path = _ACCESS_QUERY_SECRET_RE.sub(lambda m: m.group(1) + "<redacted>", path)
    return _ACCESS_APPEAL_TOKEN_RE.sub(lambda m: m.group(1) + "<redacted>", path)


class _AccessLogScrubber(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = args[:2] + (scrub_access_path(args[2]),) + args[3:]
        return True


_access_logger = logging.getLogger("uvicorn.access")
if not any(isinstance(f, _AccessLogScrubber) for f in _access_logger.filters):
    _access_logger.addFilter(_AccessLogScrubber())


class _HashedAssets(StaticFiles):
    """/assets: long-lived cache headers on hits, no-store on misses (so a
    missing chunk is never cached as a 404 by the browser or Cloudflare)."""

    async def get_response(self, path: str, scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:  # StaticFiles raises for a missing file
            return PlainTextResponse("Not Found" if exc.status_code == 404 else "", status_code=exc.status_code,
                                     headers={"Cache-Control": CACHE_NONE})
        response.headers["Cache-Control"] = CACHE_IMMUTABLE if response.status_code in (200, 304) else CACHE_NONE
        return response


def _api_not_found() -> Response:
    """THE answer for an unknown /api/* path (any method) and for a hidden
    owner-only route seen by anyone else (HiddenRouteNotFound)."""
    return JSONResponse(status_code=404, content={"error": "not_found"})


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Live updates: start the upstream websocket client (this process is the
    # client of the bot's /ws) and cancel it cleanly on shutdown. Disabled with
    # DASHBOARD_LIVE_ENABLED=false; browsers then simply stay on polling.
    await live_hub.start(app)
    # Retention: expired sessions + old error reports (server/services/housekeeping.py).
    from server.services import housekeeping

    housekeeping_task = (
        asyncio.create_task(housekeeping.loop(), name="dashboard-housekeeping")
        if config.HOUSEKEEPING_ENABLED else None
    )
    try:
        yield
    finally:
        if housekeeping_task is not None:
            housekeeping_task.cancel()
            await asyncio.gather(housekeeping_task, return_exceptions=True)
        await live_hub.stop()
        from server.services import http_pool

        await http_pool.close_all()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Dashboard Backend",
        description="Discord OAuth2 login, sessions, and a thin client for the internal bot API. Also serves the built frontend.",
        version="0.1.0",
        lifespan=_lifespan,
        # /docs, /redoc and /openapi.json would list every route, including the
        # hidden owner-only ones: off unless DASHBOARD_API_DOCS=true (dev only).
        docs_url="/docs" if config.API_DOCS_ENABLED else None,
        redoc_url="/redoc" if config.API_DOCS_ENABLED else None,
        openapi_url="/openapi.json" if config.API_DOCS_ENABLED else None,
    )

    app.state.limiter = limiter

    async def _rate_limited(request: Request, exc: RateLimitExceeded) -> Response:
        # Public appeal routes get the flat, non-leaky error shape the
        # appeal page expects; everything else keeps slowapi's default.
        if request.url.path.startswith("/api/public/"):
            return JSONResponse(
                status_code=429, content={"error": "rate_limited"}, headers={"Retry-After": "60"}
            )
        return _rate_limit_exceeded_handler(request, exc)

    app.add_exception_handler(RateLimitExceeded, _rate_limited)

    # Owner-only Debug page: non-owners get exactly what an unknown /api/*
    # path returns (the SPA catch-all below, or FastAPI's default 404 when no
    # frontend build exists), so the hidden routes are indistinguishable.
    serves_frontend = _FRONTEND_DIST.is_dir()

    async def _hidden_not_found(request: Request, exc: HiddenRouteNotFound) -> Response:
        return _api_not_found()

    app.add_exception_handler(HiddenRouteNotFound, _hidden_not_found)

    app.add_middleware(CSRFOriginCheckMiddleware)
    # Unhandled exceptions -> "server" error reports (owner-only Debug > Errors),
    # then re-raised so the response is the normal 500.
    app.add_middleware(ErrorCaptureMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    # Outermost (added last): per-route request counts/latency for the
    # staff-only Diagnostics page. Records route templates, never raw paths.
    app.add_middleware(diagnostics.RequestMetricsMiddleware)

    # -----------------------------------------------------------------
    # API routes FIRST — everything below (the static mount + SPA
    # fallback) only ever runs for requests that don't match one of these,
    # so /api/* and /health always take priority over the frontend files.
    # -----------------------------------------------------------------
    app.include_router(auth_router)
    app.include_router(me.router)
    app.include_router(stats.router)
    app.include_router(moderation.router)
    app.include_router(tickets.router)
    app.include_router(permissions.router)
    app.include_router(reaction_roles.router)
    app.include_router(resolve.router)
    app.include_router(templates.router)
    app.include_router(ticket_panels.router)
    app.include_router(qotd.router)
    app.include_router(settings.router)
    app.include_router(status.router)
    app.include_router(honeypot.router)
    app.include_router(diagnostics.router)
    app.include_router(appeals.router)
    app.include_router(appeal_auth.router)  # public appeal sign-in with Discord (/api/public/appeal-auth)
    app.include_router(role_admin.router)  # dashboard Roles page (/api/roles)
    app.include_router(fortnite.router)  # Fortnite shop settings (/api/fortnite)
    app.include_router(debug.router)  # hidden owner-only Debug page (/api/debug, 404 for non-owners)
    app.include_router(supervisor.router)  # /api/supervisor (staff; works while the bot is down)
    app.include_router(audit.router)  # /api/audit (+ /actions), view_audit
    app.include_router(automation.router)  # /api/automation/* (per-tab permission gates)
    app.include_router(members.router)  # /api/members/* (profile staff_base, verify verify_members)
    app.include_router(birthdays.router)  # /api/birthdays/* (manage_birthdays)
    app.include_router(music.router)  # /api/music/* (control_music; staff status/debugurl)
    app.include_router(client_errors.router)  # POST /api/public/client-errors (public, rate limited)
    app.include_router(live_endpoint.router)  # WS /api/ws (browser live updates)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # -----------------------------------------------------------------
    # Frontend static files + SPA fallback.
    #
    # Mounted at /assets so hashed build assets (dist/assets/*.js, *.css)
    # are served directly and cacheably. A catch-all route below handles
    # everything else: known static files at the dist root (e.g.
    # favicon.svg) are served as-is, and any other non-/api path (e.g.
    # /dashboard hit directly via URL/browser refresh) falls back to
    # index.html so React Router's client-side routing can take over.
    #
    # If dist/ doesn't exist yet (frontend hasn't been built), this is
    # skipped entirely rather than crashing app startup — the API still
    # works standalone, matching how Steps 1/2 were verified before a
    # frontend existed.
    # -----------------------------------------------------------------
    if serves_frontend:
        assets_dir = _FRONTEND_DIST / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", _HashedAssets(directory=assets_dir), name="frontend-assets")

        index_path = _FRONTEND_DIST / "index.html"

        @app.get("/{full_path:path}")
        async def serve_frontend(full_path: str, request: Request) -> Response:
            if full_path.startswith("api/"):
                # Shouldn't be reachable (API routes are registered above
                # and take priority), but guard explicitly rather than ever
                # silently returning HTML for an unmatched /api/* path.
                return JSONResponse(status_code=404, content={"error": "not_found"})

            candidate = (_FRONTEND_DIST / full_path).resolve()
            if (
                full_path
                and candidate.is_file()
                and _FRONTEND_DIST in candidate.parents
            ):
                cache = CACHE_INDEX if candidate.name == "index.html" else CACHE_ROOT_FILE
                return FileResponse(candidate, headers={"Cache-Control": cache})

            # SPA fallback: any other path (e.g. /dashboard on a hard
            # refresh) serves index.html so React Router can render it.
            return FileResponse(index_path, headers={"Cache-Control": CACHE_INDEX})

    # -----------------------------------------------------------------
    # LAST: every /api/* request no route above fully matched (unknown path,
    # wrong method on a known path, trailing-slash variant) gets the same
    # bare 404 as a hidden owner-only route, so method (405 + Allow header)
    # and redirect_slashes (307) cannot reveal that a hidden route exists.
    # Registered after everything so it never shadows a real route; new
    # routers go in the "API routes" block above.
    # -----------------------------------------------------------------
    @app.api_route("/api/{rest:path}", methods=_ALL_METHODS, include_in_schema=False)
    async def api_not_found(rest: str) -> Response:
        return _api_not_found()

    return app


app = create_app()
