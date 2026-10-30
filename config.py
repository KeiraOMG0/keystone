"""
Central configuration module for the bot.
Reads from .env via python-dotenv, falls back to os.environ if not installed.
"""

import os
import shutil

# Attempt to load .env file via python-dotenv if available.
# If not installed, environment variables must be set in the shell manually.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; falling back to os.environ only

# ---------------------------------------------------------------------------
# Core settings
# ---------------------------------------------------------------------------

TOKEN: str = os.environ.get("DISCORD_TOKEN", "")
DB_PATH: str = os.environ.get("DB_PATH", "bot.db")
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
DEBUG: bool = os.environ.get("DEBUG", "false").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Kick cog settings
# ---------------------------------------------------------------------------

UNVERIFIED_ROLE_ID: int = int(os.environ.get("UNVERIFIED_ROLE_ID") or "0")
VERIFIED_ROLE_ID: int = int(os.environ.get("VERIFIED_ROLE_ID") or "0")
UNVERIFIED_CATEGORY_ID: int = int(os.environ.get("UNVERIFIED_CATEGORY_ID") or "0")
LOG_CHANNEL_ID: int = int(os.environ.get("LOG_CHANNEL_ID") or "0")
JOIN_LEAVE_CHANNEL_ID: int = int(os.environ.get("JOIN_LEAVE_CHANNEL_ID") or "0")
CHECK_INTERVAL_MINUTES: int = int(os.environ.get("CHECK_INTERVAL_MINUTES") or "10")
AUTO_KICK: bool = os.environ.get("AUTO_KICK", "true").lower() in ("1", "true", "yes")
VERIFY_TEMPLATE_NAME: str = os.environ.get("VERIFY_TEMPLATE_NAME", "verify")
VERIFY_TICKET_WELCOME_TEMPLATE_NAME: str = os.environ.get("VERIFY_TICKET_WELCOME_TEMPLATE_NAME", "verify-ticket-welcome")

# ---------------------------------------------------------------------------
# Ticket settings
# ---------------------------------------------------------------------------

# Plain-text-only chat-log capture for open/archived ticket channels (see
# cogs/tickets.py's on_message listener + database/migrations/sql/
# 023_ticket_claim_chatlog_purge.sql's ticket_messages table). Defaults to
# OFF — staff have not yet decided whether to log ticket content at all, and
# attachments/voice messages are explicitly never captured regardless of
# this flag.
TICKET_CHAT_LOGGING_ENABLED: bool = os.environ.get("TICKET_CHAT_LOGGING_ENABLED", "false").lower() in ("1", "true", "yes")

# How many days an archived ticket is kept before the daily purge task
# deletes its row (and any ticket_messages) permanently. Reference
# timestamp is tickets.archived_at (set by TicketService.archive_ticket).
TICKET_ARCHIVE_PURGE_DAYS: int = int(os.environ.get("TICKET_ARCHIVE_PURGE_DAYS") or "30")

# ---------------------------------------------------------------------------
# Dashboard-editable settings bridge — services/settings_service.py
# ---------------------------------------------------------------------------
#
# The fields in services/settings_service.py's SETTINGS_SCHEMA (role/category/
# channel IDs, AUTO_KICK, CHECK_INTERVAL_MINUTES, template names, the two
# TICKET_* values, MUSIC_DEFAULT_SEARCH/ALLOW_PLAYLISTS/DEFAULT_VOLUME/
# INACTIVITY_TIMEOUT) can also be set from the staff-only Settings page,
# stored in the `guild_settings` table (migration 024). Credentials are never
# in that allowlist.
#
# The values in this file are the .env DEFAULTS. At bot startup main.py calls
# SettingsService.apply_to_config() after migrations and before any cog loads,
# which overwrites the matching attributes below with the validated saved
# values (OWNER_ID is never overridden from the DB). Cogs keep reading
# config.X exactly as before, so a saved value applies on the next bot
# restart. TICKET_CHAT_LOGGING_ENABLED and TICKET_ARCHIVE_PURGE_DAYS are also
# read live by cogs/tickets.py through SettingsService (5 s TTL cache), so
# those two apply within seconds.

# ---------------------------------------------------------------------------
# Permission settings
# ---------------------------------------------------------------------------

OWNER_ID: int = int(os.environ.get("OWNER_ID") or "0")
STAFF_ROLE_IDS: set[int] = {
    int(x) for x in os.environ.get("STAFF_ROLE_IDS", "").split(",") if x.strip()
}

# ---------------------------------------------------------------------------
# Internal API settings (dashboard backend integration — embedded FastAPI app)
# ---------------------------------------------------------------------------

INTERNAL_API_HOST: str = os.environ.get("INTERNAL_API_HOST", "127.0.0.1")
INTERNAL_API_PORT: int = int(os.environ.get("INTERNAL_API_PORT") or "8766")
INTERNAL_API_SECRET: str = os.environ.get("INTERNAL_API_SECRET", "")

# Public base URL of the dashboard; honeypot ban DMs link to {APPEAL_BASE_URL}/appeal/{token}.
APPEAL_BASE_URL: str = os.environ.get("APPEAL_BASE_URL", "https://dash.idlesys.xyz").rstrip("/")

# ---------------------------------------------------------------------------
# Dashboard backend settings (server/ — separate process, Discord OAuth2)
# ---------------------------------------------------------------------------

DISCORD_CLIENT_ID: str = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET: str = os.environ.get("DISCORD_CLIENT_SECRET", "")

# ---------------------------------------------------------------------------
# Music cog settings
# ---------------------------------------------------------------------------

MUSIC_COOKIES_FILE: str = os.environ.get("MUSIC_COOKIES_FILE", "cookies.txt")

# Node.js binary path for yt-dlp-ejs. Empty/unset resolves via shutil.which()
# (PATH lookup) so this works out of the box on Mac/Linux, where node is
# normally just on PATH; only set MUSIC_NODE_PATH explicitly to override
# that lookup (e.g. a non-PATH install location).
MUSIC_NODE_PATH: str = os.environ.get("MUSIC_NODE_PATH", "") or (shutil.which("node") or "")
MUSIC_DEFAULT_SEARCH: str = os.environ.get("MUSIC_DEFAULT_SEARCH", "ytsearch7")
MUSIC_ALLOW_PLAYLISTS: bool = os.environ.get("MUSIC_ALLOW_PLAYLISTS", "true").lower() in ("1", "true", "yes")
MUSIC_DEFAULT_VOLUME: int = int(os.environ.get("MUSIC_DEFAULT_VOLUME") or "50")
MUSIC_INACTIVITY_TIMEOUT: int = int(os.environ.get("MUSIC_INACTIVITY_TIMEOUT") or "600")

# yt-dlp player clients to try, in order, when resolving a playable stream URL.
# YouTube's SABR/PO-token rollout (yt-dlp issue #12482) means many clients
# (android_vr, ios, mweb, web_embedded, plain web/web_safari) either return no
# downloadable URL or hand back a URL that 403s when ffmpeg -- not yt-dlp's own
# session -- requests it. tv_simply has been the one that reliably survives an
# actual ffmpeg fetch; tv/web are kept as fallbacks in case that changes.
MUSIC_PLAYER_CLIENTS: list[str] = [
    c.strip() for c in os.environ.get("MUSIC_PLAYER_CLIENTS", "tv_simply,tv,web,web_safari").split(",") if c.strip()
]

# PO Token provider companion server (bgutil-ytdlp-pot-provider), needed for
# videos YouTube gates behind a proof-of-origin token. Started/stopped by
# services/pot_server_service.py alongside the bot process.
MUSIC_POT_SERVER_SCRIPT: str = os.environ.get(
    "MUSIC_POT_SERVER_SCRIPT", os.path.join("pot_server", "server", "build", "main.js")
)


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    """Read an int env var; fall back to `default` (with a warning) on bad input."""
    import logging as _logging

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        _logging.getLogger(__name__).warning("Invalid %s=%r (not an integer); using default %s", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        _logging.getLogger(__name__).warning("Invalid %s=%r (must be >= %s); using default %s", name, raw, minimum, default)
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    """Read a bool env var; fall back to `default` (with a warning) on bad input."""
    import logging as _logging

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    val = raw.strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    _logging.getLogger(__name__).warning("Invalid %s=%r (expected true/false); using default %s", name, raw, default)
    return default


MUSIC_POT_SERVER_PORT: int = _env_int("MUSIC_POT_SERVER_PORT", 4416, minimum=1)
if MUSIC_POT_SERVER_PORT > 65535:
    import logging as _logging

    _logging.getLogger(__name__).warning("MUSIC_POT_SERVER_PORT=%d out of range; using 4416", MUSIC_POT_SERVER_PORT)
    MUSIC_POT_SERVER_PORT = 4416
MUSIC_POT_SERVER_ENABLED: bool = _env_bool("MUSIC_POT_SERVER_ENABLED", True)
MUSIC_POT_SERVER_HOST: str = os.environ.get("MUSIC_POT_SERVER_HOST", "").strip() or "127.0.0.1"
# Node binary for the pot server only; falls back to MUSIC_NODE_PATH / PATH lookup.
MUSIC_POT_SERVER_NODE: str = os.environ.get("MUSIC_POT_SERVER_NODE", "").strip()
MUSIC_POT_SERVER_LOG: str = os.environ.get("MUSIC_POT_SERVER_LOG", "").strip() or os.path.join("logs", "pot_server.log")
MUSIC_POT_SERVER_WATCHDOG: bool = _env_bool("MUSIC_POT_SERVER_WATCHDOG", True)
MUSIC_POT_SERVER_HEALTH_INTERVAL: int = _env_int("MUSIC_POT_SERVER_HEALTH_INTERVAL", 30, minimum=1)
MUSIC_POT_SERVER_MAX_RESTARTS: int = _env_int("MUSIC_POT_SERVER_MAX_RESTARTS", 5, minimum=0)

# yt-dlp behaviour knobs.
MUSIC_EXTRACT_TIMEOUT: int = _env_int("MUSIC_EXTRACT_TIMEOUT", 90, minimum=1)
MUSIC_YTDLP_SOCKET_TIMEOUT: int = _env_int("MUSIC_YTDLP_SOCKET_TIMEOUT", 20, minimum=1)
# Default true preserves the previous hard-coded `remote_components: ["ejs:github"]`.
MUSIC_EJS_REMOTE_COMPONENTS: bool = _env_bool("MUSIC_EJS_REMOTE_COMPONENTS", True)


def _env_port(name: str, default: int) -> int:
    """Read a TCP port env var (1-65535); fall back to `default` on bad input."""
    import logging as _logging

    value = _env_int(name, default, minimum=1)
    if value > 65535:
        _logging.getLogger(__name__).warning("%s=%d out of range; using %d", name, value, default)
        return default
    return value


def _env_list(name: str) -> list[str]:
    """Comma-separated env var -> lower-cased, stripped, non-empty items."""
    return [x.strip().lower() for x in os.environ.get(name, "").split(",") if x.strip()]


# Autoplay: when the queue empties, queue related tracks (YouTube Mix for the
# last played video). Per-guild toggle via `!autoplay`; this is the boot default.
MUSIC_AUTOPLAY_DEFAULT: bool = _env_bool("MUSIC_AUTOPLAY_DEFAULT", False)
# Max autoplay tracks in a row before it pauses until someone queues a track.
MUSIC_AUTOPLAY_MAX_CONSECUTIVE: int = _env_int("MUSIC_AUTOPLAY_MAX_CONSECUTIVE", 10, minimum=1)
# How many recently played video ids autoplay refuses to repeat.
MUSIC_AUTOPLAY_HISTORY: int = _env_int("MUSIC_AUTOPLAY_HISTORY", 25, minimum=1)

# Cookie-extension listener (services/cookie_listener_service.py). ON by default and
# API-compatible with the musicbot-cookie-sync extension (default port 8999), so the stock
# extension works unmodified. Auth: if MUSIC_COOKIE_LISTENER_SECRET is set it is
# required; if not, the request must come from a browser-extension Origin
# (optionally limited to MUSIC_COOKIE_LISTENER_EXTENSION_IDS). A busy port never
# stops the bot: it warns and retries in the background.
MUSIC_COOKIE_LISTENER_ENABLED: bool = _env_bool("MUSIC_COOKIE_LISTENER_ENABLED", True)
MUSIC_COOKIE_LISTENER_EXTENSION_IDS: list[str] = _env_list("MUSIC_COOKIE_LISTENER_EXTENSION_IDS")
MUSIC_COOKIE_LISTENER_HOST: str = os.environ.get("MUSIC_COOKIE_LISTENER_HOST", "").strip() or "127.0.0.1"
MUSIC_COOKIE_LISTENER_PORT: int = _env_port("MUSIC_COOKIE_LISTENER_PORT", 8999)
MUSIC_COOKIE_LISTENER_SECRET: str = os.environ.get("MUSIC_COOKIE_LISTENER_SECRET", "").strip()
MUSIC_COOKIE_LISTENER_MAX_BYTES: int = _env_int("MUSIC_COOKIE_LISTENER_MAX_BYTES", 524288, minimum=1024)
MUSIC_COOKIE_LISTENER_RATE_PER_MIN: int = _env_int("MUSIC_COOKIE_LISTENER_RATE_PER_MIN", 30, minimum=1)

# !debugurl: extra query-param names to mask (extends the built-in list) and
# domains/keywords whose inputs are refused outright (comma-separated).
MUSIC_DEBUGURL_MASK_PARAMS: list[str] = _env_list("MUSIC_DEBUGURL_MASK_PARAMS")
MUSIC_DEBUGURL_BLOCKED: list[str] = _env_list("MUSIC_DEBUGURL_BLOCKED")

# ---------------------------------------------------------------------------
# Fortnite item shop (services/fortnite_shop_service.py, cogs/fortnite_shop.py)
# ---------------------------------------------------------------------------

# fortnite-api.com's /v2/shop works WITHOUT a key; when set, it is sent as the
# Authorization header (the API's documented key mechanism).
FORTNITE_API_KEY: str = os.environ.get("FORTNITE_API_KEY", "").strip()
# Output language for item names (fortnite-api.com `language` query param).
FORTNITE_SHOP_LANGUAGE: str = os.environ.get("FORTNITE_SHOP_LANGUAGE", "").strip() or "en"
# Minutes after the 00:00 UTC shop rotation before the daily auto-post fires.
FORTNITE_SHOP_POST_DELAY_MINUTES: int = _env_int("FORTNITE_SHOP_POST_DELAY_MINUTES", 5, minimum=0)

# ---------------------------------------------------------------------------
# Anti-spam (services/spam_detector.py, cogs/anti_spam.py). These are the .env
# defaults; all of them are dashboard Settings fields (group "Anti-spam") read
# LIVE, so a save applies within seconds. A count of 0 turns that rule off;
# ANTISPAM_TIMEOUT_MINUTES=0 means delete the spam but don't time out.
# ---------------------------------------------------------------------------

ANTISPAM_ENABLED: bool = _env_bool("ANTISPAM_ENABLED", True)
ANTISPAM_FLOOD_MESSAGES: int = _env_int("ANTISPAM_FLOOD_MESSAGES", 6, minimum=0)
ANTISPAM_FLOOD_SECONDS: int = _env_int("ANTISPAM_FLOOD_SECONDS", 5, minimum=1)
ANTISPAM_DUPLICATE_MESSAGES: int = _env_int("ANTISPAM_DUPLICATE_MESSAGES", 5, minimum=0)
ANTISPAM_DUPLICATE_SECONDS: int = _env_int("ANTISPAM_DUPLICATE_SECONDS", 30, minimum=1)
ANTISPAM_MENTION_LIMIT: int = _env_int("ANTISPAM_MENTION_LIMIT", 6, minimum=0)
ANTISPAM_TIMEOUT_MINUTES: int = _env_int("ANTISPAM_TIMEOUT_MINUTES", 10, minimum=0)

# ---------------------------------------------------------------------------
# Moderation message log (services/message_log_service.py, cogs/message_log.py):
# every guild message (members, bots, webhooks, apps; never DMs, ticket
# channels or this bot's own messages) kept in its OWN SQLite file, not bot.db,
# for MESSAGE_LOG_RETENTION_DAYS (1-30), pruned daily. The file is sensitive.
# ---------------------------------------------------------------------------

MESSAGE_LOG_ENABLED: bool = _env_bool("MESSAGE_LOG_ENABLED", True)
MESSAGE_LOG_PATH: str = os.environ.get("MESSAGE_LOG_PATH", "").strip() or "message_log.db"
MESSAGE_LOG_RETENTION_DAYS: int = min(30, _env_int("MESSAGE_LOG_RETENTION_DAYS", 7, minimum=1))

# ---------------------------------------------------------------------------
# User cache (services/user_cache_service.py, cogs/user_cache.py): last known
# name/avatar/membership status of every member, so the dashboard can render
# users who left, were kicked or banned. Its OWN SQLite file, not bot.db.
# Departed users are kept USER_CACHE_RETENTION_DAYS (0 = forever; users still
# banned are never pruned), pruned daily. The file is sensitive.
# ---------------------------------------------------------------------------

USER_CACHE_ENABLED: bool = _env_bool("USER_CACHE_ENABLED", True)
USER_CACHE_PATH: str = os.environ.get("USER_CACHE_PATH", "").strip() or "user_cache.db"
USER_CACHE_RETENTION_DAYS: int = _env_int("USER_CACHE_RETENTION_DAYS", 365, minimum=0)

# ---------------------------------------------------------------------------
# Supervisor control API (supervisor.py, services/supervisor_client.py,
# cogs/supervisor_control.py). The supervisor binds 127.0.0.1 only.
# ---------------------------------------------------------------------------

SUPERVISOR_CONTROL_PORT: int = _env_port("SUPERVISOR_CONTROL_PORT", 8767)
# Same fallback as supervisor.py: a dedicated secret, else INTERNAL_API_SECRET.
SUPERVISOR_CONTROL_SECRET: str = (
    os.environ.get("SUPERVISOR_CONTROL_SECRET", "").strip() or os.environ.get("INTERNAL_API_SECRET", "").strip()
)
SUPERVISOR_CONTROL_URL: str = f"http://127.0.0.1:{SUPERVISOR_CONTROL_PORT}"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set. "
        "Add it to your .env file or export it as an environment variable."
    )

if not INTERNAL_API_SECRET:
    raise RuntimeError(
        "INTERNAL_API_SECRET is not set. "
        "Add it to your .env file or export it as an environment variable."
    )
