"""
Config loading for the dashboard backend (server/).

This is a SEPARATE process from the bot (main.py), possibly launched from a
different working directory (e.g. `uvicorn server.app:app` run from repo
root, or a process manager that cd's elsewhere first). We therefore do NOT
rely on python-dotenv's default CWD-relative .env discovery — we compute the
repo root explicitly from this file's location and load that exact path.

Both processes read the SAME root .env file (single source of truth for
shared values like INTERNAL_API_SECRET), but each process has its own
config module — mirroring the existing internal_api/ vs main.py pattern
of "each process reads config for itself" rather than importing the bot's
top-level config.py (which raises RuntimeError at import time if
DISCORD_TOKEN is missing — we don't want this process's startup coupled to
that check).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# repo root = parent of this file's directory (server/config.py -> repo root)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_REPO_ROOT, ".env")

load_dotenv(dotenv_path=_ENV_PATH)

# ---------------------------------------------------------------------------
# Internal API client settings — must match main.py's config.py values since
# they're reading the same .env keys (this process is a CLIENT of that API).
# ---------------------------------------------------------------------------

INTERNAL_API_HOST: str = os.environ.get("INTERNAL_API_HOST", "127.0.0.1")
INTERNAL_API_PORT: int = int(os.environ.get("INTERNAL_API_PORT") or "8766")
INTERNAL_API_SECRET: str = os.environ.get("INTERNAL_API_SECRET", "")
INTERNAL_API_BASE_URL: str = f"http://{INTERNAL_API_HOST}:{INTERNAL_API_PORT}"

# ---------------------------------------------------------------------------
# Discord OAuth2
# ---------------------------------------------------------------------------

DISCORD_CLIENT_ID: str = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET: str = os.environ.get("DISCORD_CLIENT_SECRET", "")

# NOTE: registered in the Discord Developer Portal as:
#   http://localhost:8080/api/auth/callback   (local dev)
#   https://dash.idlesys.xyz/api/auth/callback (production, via Cloudflare
#     Tunnel -> localhost:8080, see C:\Users\Keira\.cloudflared\config.yml)
# This MUST match DASHBOARD_PORT below and the redirect_uri built in
# server/auth/oauth.py exactly, or Discord will reject the callback with
# "Invalid OAuth2 redirect_uri".
DISCORD_OAUTH_REDIRECT_URI: str = os.environ.get(
    "DISCORD_OAUTH_REDIRECT_URI", "http://localhost:8080/api/auth/callback"
)
DISCORD_OAUTH_SCOPES: str = "identify guilds.members.read"

# Public ban-appeal sign-in (server/routes/appeal_auth.py): a SEPARATE redirect,
# registered in the Developer Portal too (https://dash.idlesys.xyz/... and
# http://localhost:8080/...). Default: same origin as DISCORD_OAUTH_REDIRECT_URI.
def _origin(url: str) -> str:
    parts = url.split("/", 3)
    return "/".join(parts[:3]) if len(parts) >= 3 else url


APPEAL_OAUTH_REDIRECT_URI: str = os.environ.get("APPEAL_OAUTH_REDIRECT_URI") or (
    _origin(DISCORD_OAUTH_REDIRECT_URI) + "/api/public/appeal-auth/callback"
)

# ---------------------------------------------------------------------------
# This process's own server settings
# ---------------------------------------------------------------------------

DASHBOARD_HOST: str = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT: int = int(os.environ.get("DASHBOARD_PORT") or "8080")

# Where to redirect the browser after a successful OAuth callback. Later
# steps will serve the frontend from this same process; for now this just
# needs to be a valid landing URL.
FRONTEND_BASE_URL: str = os.environ.get("FRONTEND_BASE_URL", "http://localhost:8080")

# Signing key for the session cookie value (opaque random session_id, not a
# JWT — but we still sign the cookie itself so it can't be forged/tampered
# with in transit; the actual session state lives server-side in the DB).
SESSION_COOKIE_SECRET: str = os.environ.get("SESSION_COOKIE_SECRET", "")
SESSION_COOKIE_NAME: str = "dashboard_session"
OAUTH_STATE_COOKIE_NAME: str = "dashboard_oauth_state"

# The guild this dashboard is scoped to (single-guild deployment for now —
# see setup_server/server_state.json for the live value).
DASHBOARD_GUILD_ID: int = int(os.environ.get("DASHBOARD_GUILD_ID") or os.environ.get("GUILD_ID") or "0")

# Secure flag on the session + OAuth-state cookies. DASHBOARD_COOKIE_SECURE:
#   auto (default, also when unset/empty/unrecognised): Secure whenever the
#        request arrived over HTTPS (request.url.scheme, or X-Forwarded-Proto
#        https from a loopback peer such as cloudflared), so
#        https://dash.idlesys.xyz gets Secure cookies while plain
#        http://localhost dev keeps working (browsers drop Secure cookies on http).
#   true / false: always / never (server/auth/oauth.py cookie_secure()).
def _cookie_secure_mode(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return "true"
    if value in ("0", "false", "no", "off"):
        return "false"
    return "auto"


COOKIE_SECURE_MODE: str = _cookie_secure_mode(os.environ.get("DASHBOARD_COOKIE_SECURE"))
# Kept for readers that want a bool (Diagnostics): True only for an explicit
# "true"; "auto" decides per request.
COOKIE_SECURE: bool = COOKIE_SECURE_MODE == "true"

# FastAPI's /docs, /redoc and /openapi.json list every route, including the
# hidden owner-only ones, so they are OFF unless explicitly enabled (local
# development only; never on the public tunnel).
# Retention jobs (server/services/housekeeping.py: expired sessions, old error
# reports) started by server/app.py's lifespan. Tests set
# DASHBOARD_HOUSEKEEPING=false (tests/conftest.py) so they never touch the real DB.
HOUSEKEEPING_ENABLED: bool = os.environ.get("DASHBOARD_HOUSEKEEPING", "true").strip().lower() not in ("0", "false", "no")
API_DOCS_ENABLED: bool = os.environ.get("DASHBOARD_API_DOCS", "false").strip().lower() in ("1", "true", "yes")

# Bot/server owner's Discord user ID (same root .env key the bot's config.py
# reads). Gates the hidden owner-only Debug page (permissions/guards.py's
# require_owner). 0 = unset, which disables that page for everyone.
OWNER_ID: int = int(os.environ.get("OWNER_ID") or "0")

# ---------------------------------------------------------------------------
# Sliding session expiry
# ---------------------------------------------------------------------------

# 30 days, SLIDING: a session row dies only after this long with no activity
# (last_seen_at is bumped, throttled, by touch_session). The server-side row is
# the security boundary.
SESSION_MAX_AGE_SECONDS: int = int(os.environ.get("DASHBOARD_SESSION_MAX_AGE_SECONDS") or str(60 * 60 * 24 * 30))  # 30 days
# touch_session() writes last_seen_at at most this often (not on every request).
# The browser cookie is set once at login and never re-issued, so its own
# max_age must NOT be the sliding window (it would force a re-login 30 days
# after login even for a daily user). It is just the signed opaque row id: give it
# the browser's ceiling (400 days) and let the DB row decide validity.
SESSION_COOKIE_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 400
SESSION_TOUCH_INTERVAL_SECONDS: int = int(os.environ.get("DASHBOARD_SESSION_TOUCH_INTERVAL_SECONDS") or "300")
OAUTH_STATE_MAX_AGE_SECONDS: int = 600  # 10 minutes — just needs to survive the Discord redirect round trip

# ---------------------------------------------------------------------------
# Live updates (WebSocket): this process is the WS CLIENT of the bot's internal
# API (`/ws`) and the WS SERVER for browsers (`/api/ws`). See docs/ARCHITECTURE.md.
# ---------------------------------------------------------------------------

LIVE_ENABLED: bool = os.environ.get("DASHBOARD_LIVE_ENABLED", "true").lower() in ("1", "true", "yes")
LIVE_INTERNAL_WS_URL: str = f"ws://{INTERNAL_API_HOST}:{INTERNAL_API_PORT}/ws"
# How often each open browser socket re-checks its session + permissions.
LIVE_REVALIDATE_SECONDS: float = float(os.environ.get("DASHBOARD_WS_REVALIDATE_SECONDS") or "60")
# Upstream reconnect backoff cap (seconds); retried forever.
LIVE_UPSTREAM_BACKOFF_CAP_SECONDS: float = float(os.environ.get("DASHBOARD_WS_BACKOFF_CAP_SECONDS") or "15")

# ---------------------------------------------------------------------------
# Supervisor control API (supervisor.py, loopback only). The dashboard calls it
# DIRECTLY (server/services/supervisor_client.py), not through the internal
# API, so restarts keep working while the bot is down or hung.
# ---------------------------------------------------------------------------

SUPERVISOR_CONTROL_PORT: int = int(os.environ.get("SUPERVISOR_CONTROL_PORT") or "8767")
# Same fallback as supervisor.py: a dedicated secret, else INTERNAL_API_SECRET.
SUPERVISOR_CONTROL_SECRET: str = (
    os.environ.get("SUPERVISOR_CONTROL_SECRET", "").strip() or INTERNAL_API_SECRET.strip()
)
SUPERVISOR_BASE_URL: str = f"http://127.0.0.1:{SUPERVISOR_CONTROL_PORT}"

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

DASHBOARD_DB_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.db")

# ---------------------------------------------------------------------------
# Validation — fail loudly at import time rather than mysteriously later.
# ---------------------------------------------------------------------------

if not DISCORD_CLIENT_ID:
    raise RuntimeError(
        "DISCORD_CLIENT_ID is not set. Add it to your .env file."
    )

if not DISCORD_CLIENT_SECRET:
    raise RuntimeError(
        "DISCORD_CLIENT_SECRET is not set. Add it to your .env file."
    )

if not INTERNAL_API_SECRET:
    raise RuntimeError(
        "INTERNAL_API_SECRET is not set. Add it to your .env file. "
        "This must match the value main.py's bot process uses."
    )

if not DASHBOARD_GUILD_ID:
    raise RuntimeError(
        "GUILD_ID is not set. Put your Discord server's ID in your .env file (see .env.example)."
    )
if not SESSION_COOKIE_SECRET:
    raise RuntimeError(
        "SESSION_COOKIE_SECRET is not set. Add it to your .env file "
        "(any long random string — used to sign the session cookie)."
    )
