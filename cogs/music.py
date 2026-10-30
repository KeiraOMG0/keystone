import asyncio
import io
import logging
import os
import random
import re
import time
from collections import deque
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
import yt_dlp
import config as _config
from core.text_utils import split_message
from services import live_events
from services.cookie_listener_service import CookieListenerService
from services.music_control import (
    STATE_QUEUE_LIMIT,
    MusicActionError,
    MusicJobs,
    check_volume,
    clean_query,
    safe_playlist_name,
    short,
)
from services.pot_server_service import pot_base_url


log = logging.getLogger(__name__)

# Music echoes user input (search terms, playlist names) and third-party
# titles (YouTube / playlist JSON), any of which can contain "@everyone",
# "<@&role>" or "<@user>". Every message this cog sends through a command
# Context goes out with no pings at all (mute_mentions, applied in
# cog_before_invoke and to the Context built for search-pick replies), on
# top of the bot-wide default in core/bot_factory.py. Echoed names also go
# through discord.utils.escape_mentions so they don't even render as mentions.
NO_PINGS = discord.AllowedMentions.none()


def mute_mentions(ctx) -> None:
    """Make ctx.send (and ctx.reply, which calls ctx.send) default to
    AllowedMentions.none(). Idempotent; an explicit allowed_mentions wins."""
    if getattr(ctx, "_music_mentions_muted", False):
        return
    original = ctx.send

    async def send(*args, **kwargs):
        kwargs.setdefault("allowed_mentions", NO_PINGS)
        return await original(*args, **kwargs)

    ctx.send = send
    ctx._music_mentions_muted = True


def safe_text(text) -> str:
    """User / third-party text for echoing: mentions neutralised."""
    return discord.utils.escape_mentions(str(text))


# Commands whose channel is NOT remembered as the music text channel (staff
# diagnostics are often run in a staff channel; help is read-only).
_NON_PLAYER_COMMANDS: frozenset[str] = frozenset({"musicstatus", "debugurl", "musichelp"})


class _DashboardAuthor:
    """Stand-in author when the dashboard actor is not a cached member (e.g.
    the owner is not in the guild). Only ``id``/``voice``/``display_name`` are read."""

    def __init__(self, user_id: int, name: str = "dashboard user") -> None:
        self.id = user_id
        self.voice = None
        self.display_name = name
        self.bot = False


class _DashboardContext:
    """Duck-typed commands.Context for actions started from the dashboard.

    play_next / _autoplay_refill / _join_channel only use ``send``, ``author``,
    ``guild`` and ``channel``. Sends go to the music text channel (the channel a
    music command was last used in, else the voice channel's own text chat)
    with AllowedMentions.none(); with no usable channel they are dropped (the
    dashboard still shows the state). A failed send never breaks playback."""

    def __init__(self, bot, guild, author, channel) -> None:
        self.bot = bot
        self.guild = guild
        self.author = author
        self.channel = channel
        self._music_mentions_muted = True

    async def send(self, content=None, **kwargs):
        if self.channel is None:
            return None
        kwargs["allowed_mentions"] = NO_PINGS
        try:
            return await self.channel.send(content, **kwargs)
        except discord.HTTPException:
            log.warning("music: dashboard announcement to channel %s failed", getattr(self.channel, "id", "?"))
            return None

    async def reply(self, content=None, **kwargs):
        kwargs.pop("mention_author", None)
        return await self.send(content, **kwargs)


ffmpeg_log = logging.getLogger(f"{__name__}.ffmpeg")

PAGE_SIZE: int = 20
ARROW_LEFT: str = "⬅️"
ARROW_RIGHT: str = "➡️"
SELECTION_TTL: int = 300


class _FFmpegStderrLogger(io.RawIOBase):
    """File-like object that forwards ffmpeg's stderr output to our logger
    line-by-line, so a stream that ffmpeg silently rejects (e.g. HTTP 403
    from a bad/expired googlevideo URL) shows up instead of just looking
    like the track ended immediately with no error."""

    def __init__(self, title: str) -> None:
        self._title = title
        self._buffer = b""

    def write(self, data: bytes) -> int:
        self._buffer += data
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            text = line.decode(errors="replace").strip()
            if text:
                ffmpeg_log.warning("[%s] %s", self._title, text)
        return len(data)

    def writable(self) -> bool:
        return True


class _YdlLogCapture:
    """yt-dlp logger that keeps error/warning text so failures can be
    diagnosed (pot server down vs cookies vs unavailable) instead of being
    swallowed by `ignoreerrors` + `quiet`."""

    def __init__(self, track_debug: bool = False) -> None:
        self.errors: list[str] = []
        # Only populated for !debugurl: the yt-dlp progress lines (which name the
        # player clients it contacted).
        self.debug_lines: Optional[list[str]] = [] if track_debug else None

    def debug(self, msg: str) -> None:
        if self.debug_lines is not None and len(self.debug_lines) < 200:
            self.debug_lines.append(str(msg))

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        text = str(msg)
        if "po token" in text.lower():
            self.errors.append(text)
        log.debug("yt-dlp warning: %s", text)

    def error(self, msg: str) -> None:
        self.errors.append(str(msg))


def diagnose_extract_failure(messages: list[str], pot_up: Optional[bool]) -> str:
    """Turn raw yt-dlp error text into an actionable one-liner."""
    text = " ".join(messages).lower()
    if any(k in text for k in ("sign in to confirm", "not a bot", "cookies", "login required", "sign in")):
        return (
            "YouTube wants a signed-in session: cookies are missing/expired. "
            f"Re-export {_config.MUSIC_COOKIES_FILE} from a logged-in browser."
        )
    if pot_up is False or any(
        k in text for k in ("po token", "bgutil", "connection refused", "failed to establish", "pot provider")
    ):
        return (
            f"PO token server appears down/unreachable at {pot_base_url()}. "
            "Check `!musicstatus` and logs/pot_server.log."
        )
    if any(
        k in text
        for k in ("video unavailable", "private video", "has been removed", "not available", "copyright", "members-only", "age-restricted", "age restricted", "confirm your age")
    ):
        return "The video is unavailable (private/removed/region- or age-restricted); not a bot-side problem."
    return "Unknown extraction failure; see the yt-dlp error above."


# Error classes for !debugurl / cookie-refresh decisions -> (label, advice).
ERROR_CLASSES: dict[str, tuple[str, str]] = {
    "timeout": ("timeout", "Extraction timed out; a hung network call or a stuck PO token server."),
    "age_gated": ("age-gated", "Age-restricted: needs cookies from a signed-in, age-verified account."),
    "cookies_expired": ("cookies expired", "YouTube wants a signed-in session; refresh cookies.txt."),
    "pot_down": ("pot server down", "PO token server unreachable; check `!musicstatus`."),
    "unavailable": ("unavailable", "Private/removed/region-locked video; not a bot-side problem."),
    "unknown": ("unknown", "Unclassified failure; see the log for the yt-dlp error."),
}


def classify_extract_error(messages: list[str], pot_up: Optional[bool], timed_out: bool = False) -> str:
    """Bucket raw yt-dlp error text into one of ERROR_CLASSES' keys."""
    if timed_out:
        return "timeout"
    text = " ".join(messages).lower()
    if any(k in text for k in ("confirm your age", "age-restricted", "age restricted", "age verification")):
        return "age_gated"
    if any(k in text for k in ("sign in to confirm", "not a bot", "cookies", "login required", "sign in")):
        return "cookies_expired"
    if pot_up is False or any(
        k in text for k in ("po token", "bgutil", "connection refused", "failed to establish", "pot provider")
    ):
        return "pot_down"
    if any(
        k in text
        for k in ("video unavailable", "private video", "has been removed", "not available", "copyright", "members-only")
    ):
        return "unavailable"
    return "unknown"


# ---------------------------------------------------------------------------
# Autoplay (pure helpers so the selection/stop rules are unit-testable)
# ---------------------------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")
AUTOPLAY_BATCH_SIZE: int = 3
AUTOPLAY_MIX_ITEMS: int = 30


def extract_video_id(url: str) -> Optional[str]:
    match = _VIDEO_ID_RE.search(url or "")
    return match.group(1) if match else None


class AutoplayState:
    """Per-guild autoplay state (in memory only)."""

    def __init__(self, enabled: bool, history: int) -> None:
        self.enabled = enabled
        self.recent: deque[str] = deque(maxlen=max(1, history))  # recently played video ids
        self.consecutive = 0          # autoplay tracks played since a human-queued one
        self.auto_urls: set[str] = set()  # queued by autoplay, not yet started
        self.cap_notified = False

    def record_played(self, url: str, *, autoplay: bool) -> None:
        vid = extract_video_id(url)
        if vid:
            self.recent.append(vid)
        if autoplay:
            self.consecutive += 1
        else:
            self.consecutive = 0
            self.cap_notified = False

    def reset(self) -> None:
        self.consecutive = 0
        self.auto_urls.clear()
        self.cap_notified = False


def autoplay_block_reason(
    *,
    enabled: bool,
    intentional: bool,
    humans: int,
    consecutive: int,
    max_consecutive: int,
    has_seed: bool,
) -> Optional[str]:
    """Why autoplay must NOT queue anything right now (None = go ahead)."""
    if not enabled:
        return "disabled"
    if intentional:
        return "intentional_stop"
    if humans <= 0:
        return "no_listeners"
    if consecutive >= max_consecutive:
        return "cap"
    if not has_seed:
        return "no_seed"
    return None


def select_autoplay_candidates(
    entries: list[dict], recent_ids: set[str], seed_id: Optional[str], limit: int
) -> list[tuple[str, str]]:
    """Pick up to ``limit`` (url, title) tracks from a Mix's entries, skipping the
    seed, anything recently played, and duplicates within the batch."""
    blocked = set(recent_ids)
    if seed_id:
        blocked.add(seed_id)
    picked: list[tuple[str, str]] = []
    for entry in entries:
        if len(picked) >= limit:
            break
        vid = (entry or {}).get("id")
        if not vid or vid in blocked:
            continue
        blocked.add(vid)
        url = entry.get("url") or f"https://www.youtube.com/watch?v={vid}"
        picked.append((url, entry.get("title") or "Unknown"))
    return picked


# ---------------------------------------------------------------------------
# !debugurl helpers (masking, expiry, blocklist, report)
# ---------------------------------------------------------------------------

DEBUGURL_DEFAULT_MASK: frozenset[str] = frozenset(
    {"sig", "lsig", "signature", "pot", "key", "token", "auth", "ip", "ei", "cookie"}
)
_SENSITIVE_NAME_RE = re.compile(r"secret|token|sign", re.I)
YOUTUBE_INPUT_HOSTS: tuple[str, ...] = ("youtube.com", "youtu.be", "youtube-nocookie.com")
STREAM_PROBE_HOSTS: tuple[str, ...] = ("googlevideo.com", "youtube.com", "google.com")
# yt-dlp `allowed_extractors` for !debugurl: YouTube's extractors only (incl.
# youtube:search for plain queries), never "generic" (security audit 2026-10-30).
DEBUGURL_EXTRACTORS: list[str] = ["youtube.*"]
_MASK = "***"


def build_mask_set(extra: Optional[list[str]] = None) -> frozenset[str]:
    return DEBUGURL_DEFAULT_MASK | {e.strip().lower() for e in (extra or []) if e.strip()}


def is_sensitive_param(name: str, mask: frozenset[str]) -> bool:
    n = name.lower()
    return n in mask or bool(_SENSITIVE_NAME_RE.search(n))


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def is_youtube_url(url: str) -> bool:
    try:
        return _host_matches(urlsplit(url).hostname or "", YOUTUBE_INPUT_HOSTS)
    except ValueError:
        return False


async def url_block_reason(url: str) -> str | None:
    """Why `url` must not be handed to yt-dlp, or None when it may be.

    Any member can `!play <url>` (and `control_music` holders can add one from
    the dashboard), and yt-dlp's generic extractor fetches whatever it is given
    FROM THIS MACHINE. Without this check a member could make the bot send
    requests to loopback services (the internal API, the supervisor, other
    local sites behind the same tunnel) or the home network (security audit
    2026-10-30). Only http(s) to hosts that resolve exclusively to public
    addresses are allowed; YouTube hosts skip the DNS lookup. Residual risk:
    yt-dlp follows HTTP redirects itself, and DNS can change between this
    check and yt-dlp's own lookup."""
    import ipaddress
    import socket

    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "invalid URL"
    if parts.scheme not in ("http", "https") or not host:
        return "only http(s) links are allowed"
    if _host_matches(host, YOUTUBE_INPUT_HOSTS):
        return None
    try:
        addresses = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
            )
        except (OSError, UnicodeError):
            return "the host could not be resolved"
        addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    for address in addresses:
        mapped = getattr(address, "ipv4_mapped", None)
        if not (mapped or address).is_global:
            return "private, local and reserved network addresses are not allowed"
    return None


def mask_url(url: str, mask: frozenset[str]) -> tuple[str, list[tuple[str, str]]]:
    """Return (scheme://host/path, [(param name, shown value)]) with sensitive
    parameter values and sensitive path segments replaced by ``***``. Userinfo and
    fragment are dropped."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    segs = parts.path.split("/")
    for i in range(1, len(segs)):
        # googlevideo/HLS style paths embed values as /key/value/ pairs.
        if is_sensitive_param(segs[i - 1], mask) and segs[i] != _MASK:
            segs[i] = _MASK
    base = f"{parts.scheme}://{host}{'/'.join(segs)}"
    params: list[tuple[str, str]] = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        shown = _MASK if is_sensitive_param(name, mask) else (value if len(value) <= 40 else value[:37] + "...")
        params.append((name, shown))
    return base, params


def format_masked_url(url: str, mask: frozenset[str]) -> str:
    base, params = mask_url(url, mask)
    lines = [base] + [f"  {name}={val}" for name, val in params]
    return "\n".join(lines).replace("```", "'''")


def url_expiry(url: str) -> Optional[int]:
    """Epoch seconds from ``expire=`` (query) or ``/expire/<n>/`` (path), if present."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if name.lower() == "expire" and value.isdigit():
            return int(value)
    m = re.search(r"/expire/(\d{9,11})(?:/|$)", parts.path)
    return int(m.group(1)) if m else None


def check_blocked(text: str, blocked: list[str]) -> Optional[str]:
    """Return the first blocked domain/keyword found in ``text`` (case-insensitive)."""
    low = (text or "").lower()
    for term in blocked:
        if term and term in low:
            return term
    return None


def parse_debugurl_args(raw: Optional[str]) -> tuple[str, bool]:
    """Split ``<query|url> [--full]`` into (query, full_flag)."""
    tokens = (raw or "").split()
    full = any(t.lower() == "--full" for t in tokens)
    return " ".join(t for t in tokens if t.lower() != "--full"), full


def _fmt_delta(seconds: float) -> str:
    return fmt_uptime(abs(seconds))


def build_debugurl_report(d: dict, mask: frozenset[str], now: Optional[float] = None) -> str:
    """Render diagnostics for !debugurl. The stream URL is only ever shown masked."""
    now = time.time() if now is None else now
    ok = d.get("stream_url") is not None
    lines = [f"**debugurl** {'OK' if ok else 'FAILED'}  (resolved in {d.get('elapsed_ms', 0)}ms)"]
    if d.get("error_class"):
        label, advice = ERROR_CLASSES.get(d["error_class"], ERROR_CLASSES["unknown"])
        lines.append(f"Error class: **{label}**: {advice}")
        if d.get("error_text"):
            # yt-dlp errors can embed full URLs; reduce them to scheme+host+path first.
            err = re.sub(r"https?://\S+", lambda m: mask_url(m.group(0), mask)[0], d["error_text"])
            lines.append(f"yt-dlp said: {sanitize_status_text(err, 300)}")
    lines.append(
        f"Extractor: {sanitize_status_text(d.get('extractor') or 'n/a', 30)}  "
        f"Video id: {sanitize_status_text(d.get('video_id') or 'n/a', 20)}"
    )
    if d.get("title"):
        dur = d.get("duration")
        lines.append(f"Title: {sanitize_status_text(d['title'], 100)} ({MusicCog._fmt_duration(dur)})")
    if ok:
        fmt_bits = [
            f"id {d.get('format_id') or '?'}",
            f"{d.get('acodec') or '?'}",
            f"abr {d.get('abr')}k" if d.get("abr") else None,
            f"tbr {d.get('tbr')}k" if d.get("tbr") else None,
            f"{d.get('protocol') or '?'}",
            f"{d.get('ext') or '?'}",
        ]
        lines.append("Format: " + ", ".join(sanitize_status_text(b, 30) for b in fmt_bits if b))
        pot_state = {True: "up", False: "DOWN", None: "n/a"}[d.get("pot_server_up")]
        lines.append(
            f"PO token: {'used (pot= in URL)' if d.get('pot_used') else 'not used'}  pot server: {pot_state}"
        )
        clients = ", ".join(d.get("clients_contacted") or []) or "n/a"
        lines.append(
            f"Player client: URL c={sanitize_status_text(d.get('url_client') or 'n/a', 24)}  "
            f"contacted: {sanitize_status_text(clients, 80)}"
        )
        exp = d.get("expires_at")
        if exp:
            when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(exp))
            rel = f"in {_fmt_delta(exp - now)}" if exp > now else f"{_fmt_delta(now - exp)} AGO (expired)"
            lines.append(f"URL expiry: {rel} ({when})")
        else:
            lines.append("URL expiry: no expire= found")
        head = d.get("head") or {}
        if head.get("skipped"):
            lines.append(f"HEAD: skipped ({sanitize_status_text(head['skipped'], 60)})")
        elif head.get("error"):
            lines.append(f"HEAD: failed after {head.get('latency_ms')}ms ({sanitize_status_text(head['error'], 60)})")
        else:
            lines.append(f"HEAD: {head.get('status')} in {head.get('latency_ms')}ms")
    flags = [
        f"availability={sanitize_status_text(d.get('availability') or 'n/a', 24)}",
        f"age_limit={d.get('age_limit') if d.get('age_limit') is not None else 'n/a'}",
        f"live_status={sanitize_status_text(d.get('live_status') or 'n/a', 24)}",
        f"embeddable={d.get('playable_in_embed') if d.get('playable_in_embed') is not None else 'n/a'}",
    ]
    lines.append("Availability: " + "  ".join(flags))
    if ok:
        lines.append("Stream URL (sensitive values masked):")
        lines.append("```\n" + format_masked_url(d["stream_url"], mask) + "\n```")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# !musicstatus formatting (module level so it is unit-testable)
# ---------------------------------------------------------------------------

_WIN_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/](?:[^\\/\s\"'`<>|]+(?: [^\\/\s\"'`<>|]+)*[\\/])*[^\\/\s\"'`<>|]*"
)  # allows spaces inside directory names ("Program Files")
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(bearer|basic)\s+\S+")
_POSIX_PATH_RE = re.compile(r"(?<![\w:/.])/(?:[\w.\-@~]+/)+[\w.\-@~]*")
_SECRET_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|cookies?|authorization|api[_-]?key|sid|bearer)\b(\s*[=:]\s*|\s+)\S+"
)


def _basename(path_text: str) -> str:
    return re.split(r"[\\/]", path_text.rstrip("\\/"))[-1] or "<path>"


def sanitize_status_text(text: object, limit: int = 160) -> str:
    """Make a log/error line safe to show in chat: filesystem paths collapse to
    their file name, secret-looking key=value pairs are redacted, and
    backticks/mentions are neutralised."""
    text = str(text).replace("\r", " ").replace("\n", " ")
    text = _WIN_PATH_RE.sub(lambda m: _basename(m.group(0)), text)
    text = _POSIX_PATH_RE.sub(lambda m: _basename(m.group(0)), text)
    text = _AUTH_SCHEME_RE.sub(lambda m: f"{m.group(1)} [redacted]", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
    text = text.replace("`", "'").replace("@", "@\u200b")
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def fmt_uptime(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def build_music_status(
    st: dict,
    *,
    staff: bool,
    ping_ms: Optional[int] = None,
    now_playing: Optional[str] = None,
    queue_count: int = 0,
    loop_enabled: bool = False,
    bot_uptime: Optional[float] = None,
    log_lines: int = 5,
    autoplay: Optional[dict] = None,
    cookie_refresh: Optional[dict] = None,
) -> str:
    """Render the status message. The public view (staff=False) exposes only
    coarse health info: no URL/host/port, PID, paths, error text or log lines.
    The staff view adds those, still path/secret-sanitised."""
    cookies = st.get("cookies") or {}
    if cookies.get("present"):
        cookie_line = f"present ({_basename(str(cookies.get('path') or 'cookies'))}), {cookies.get('age_hours')}h old"
    else:
        cookie_line = "MISSING"

    lines = [
        "**Music status**",
        f"Ping: {f'{ping_ms}ms' if ping_ms is not None else 'N/A'}  Bot uptime: {fmt_uptime(bot_uptime)}",
        f"Now playing: {sanitize_status_text(now_playing, 100) if now_playing else 'nothing'}"
        f"  Queue: {queue_count}  Loop: {'on' if loop_enabled else 'off'}",
        f"Pot server: {sanitize_status_text(st.get('state'), 30)} "
        f"(responding: {'yes' if st.get('responding') else 'no'}, "
        f"enabled: {'yes' if st.get('enabled') else 'no'})",
        f"Pot server uptime: {fmt_uptime(st.get('uptime_seconds'))}  "
        f"Restarts: {int(st.get('restart_count') or 0)}",
        f"Cookies: {cookie_line}",
        f"Node: {sanitize_status_text(st.get('node_version') or 'n/a', 30)}  "
        f"yt-dlp: {sanitize_status_text(st.get('ytdlp_version') or 'n/a', 30)}  "
        f"bgutil plugin: {sanitize_status_text(st.get('bgutil_plugin_version') or 'n/a', 30)}",
    ]
    if autoplay is not None:
        state = "on" if autoplay.get("enabled") else "off"
        detail = (
            f" ({int(autoplay.get('consecutive') or 0)}/{int(autoplay.get('max') or 0)} in a row)"
            if autoplay.get("enabled")
            else ""
        )
        lines.append(f"Autoplay: {state}{detail}")
    if cookie_refresh is not None:
        # Public output shows only the age of the last extension refresh.
        age = cookie_refresh.get("age_seconds")
        lines.append(f"Last cookie refresh: {fmt_uptime(age) + ' ago' if age is not None else 'never'}")
        poll_age = cookie_refresh.get("poll_age_seconds")
        lines.append(f"Extension last seen: {fmt_uptime(poll_age) + ' ago' if poll_age is not None else 'never'}")
    if not staff:
        # Callers send this through split_message(), so nothing is truncated here.
        return "\n".join(lines)

    lines.append("**Staff details**")
    lines.append(
        f"URL: `{sanitize_status_text(st.get('url') or 'n/a', 60)}`  pid: `{st.get('pid')}`"
        + ("  (adopted external process)" if st.get("adopted_external") else "")
    )
    lines.append(f"Watchdog: {'on' if st.get('watchdog') else 'off'} (active: {st.get('watchdog_active')})")
    last_error = st.get("last_error")
    lines.append(f"Last error: {sanitize_status_text(last_error, 200) if last_error else 'none'}")
    if cookie_refresh is not None:
        lines.append(
            f"Cookie listener: updates {int(cookie_refresh.get('updates_total') or 0)}, "
            f"last accepted {int(cookie_refresh.get('accepted_count') or 0)}/"
            f"rejected {int(cookie_refresh.get('rejected_count') or 0)}, "
            f"error: {sanitize_status_text(cookie_refresh.get('last_error') or 'none', 120)}"
        )
    tail = [sanitize_status_text(ln, 140) for ln in (st.get("log_tail") or [])][-log_lines:]
    head = "\n".join(lines)
    if tail:
        name = _basename(str(st.get("log_path") or "pot_server.log"))
        block = f"\nLog tail ({sanitize_status_text(name, 60)}):\n```\n" + "\n".join(tail) + "\n```"
        # Trim the tail (never the closing fence) to fit Discord's 2000 limit.
        room = 1990 - len(head)
        if len(block) > room:
            keep = max(0, room - len("\n```\n\n```") - 40)
            block = f"\nLog tail:\n```\n{chr(10).join(tail)[-keep:]}\n```" if keep > 0 else ""
        head += block
    return head


# ---------------------------------------------------------------------------
# Music help (generated from the cog's registered commands)
# ---------------------------------------------------------------------------

# Grouping only: commands are discovered from the cog, so a command missing from
# this table still shows up (under "Other") and nothing is described by hand.
MUSIC_HELP_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Voice & Playback", ("join", "leave", "play", "pause", "resume", "skip", "volume", "np", "previous")),
    ("Queue", ("queue", "shuffle", "remove_queue", "remove", "move")),
    ("Autoplay & Loop", ("autoplay", "loop")),
    ("Playlists & History", ("save", "load", "playlists", "delete_playlist", "history")),
    ("Status & Debug", ("musicstatus", "debugurl", "musichelp")),
)


def command_summary(cmd: commands.Command) -> str:
    """One-line description: first line of brief/help, minus the usage tail and staff tag."""
    raw = (cmd.brief or cmd.help or "").strip()
    text = raw.splitlines()[0] if raw else ""
    text = text.split(" Usage:")[0].strip()
    return text[len("(Staff)"):].strip() if text.startswith("(Staff)") else text


def command_usage(cmd: commands.Command, prefix: str) -> str:
    """`!name <args>`: the docstring's `Usage:` line if there is one, else the signature."""
    m = re.search(r"Usage:\s*(.+)", cmd.help or "")
    if m:
        return m.group(1).strip()
    return f"{prefix}{cmd.name} {cmd.signature}".strip()


def find_music_command(cmds: list[commands.Command], name: str, prefix: str = "!") -> Optional[commands.Command]:
    key = (name or "").strip().lower()
    if prefix and key.startswith(prefix):
        key = key[len(prefix):]
    for cmd in cmds:
        if key == cmd.name.lower() or key in (a.lower() for a in cmd.aliases):
            return cmd
    return None


def render_music_help(cmds: list[commands.Command], prefix: str = "!", notes: Optional[list[str]] = None) -> str:
    """Categorised overview of every visible music command (aliases, usage, staff marks)."""
    by_name = {c.name: c for c in cmds}
    placed: set[str] = set()
    sections: list[tuple[str, list[commands.Command]]] = []
    for title, names in MUSIC_HELP_CATEGORIES:
        group = [by_name[n] for n in names if n in by_name]
        placed.update(c.name for c in group)
        if group:
            sections.append((title, group))
    rest = [c for c in cmds if c.name not in placed]
    if rest:
        sections.append(("Other", rest))

    lines = [f"**Music help** (prefix `{prefix}`). Details: `{prefix}musichelp <command>` or `/music help`."]
    for title, group in sections:
        lines.append("")
        lines.append(f"**{title}**")
        for c in group:
            aliases = f" (also {', '.join(f'`{prefix}{a}`' for a in c.aliases)})" if c.aliases else ""
            staff = " **(staff)**" if c.extras.get("staff_only") else ""
            lines.append(f"`{command_usage(c, prefix)}`{aliases}{staff} - {command_summary(c)}")
    for note in notes or []:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def render_command_help(cmd: commands.Command, prefix: str = "!") -> str:
    lines = [f"**{prefix}{cmd.name}**" + (" **(staff only)**" if cmd.extras.get("staff_only") else "")]
    lines.append(f"Usage: `{command_usage(cmd, prefix)}`")
    if cmd.aliases:
        lines.append("Aliases: " + ", ".join(f"`{prefix}{a}`" for a in cmd.aliases))
    body = re.sub(r"\s*Usage:.*", "", (cmd.help or "").strip(), flags=re.S).strip()
    if body:
        lines.append(body)
    return "\n".join(lines)


class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        self._ydl_base: dict = {
            # Prefer a plain https progressive stream over HLS (m3u8) --
            # FFmpegPCMAudio is far more reliable against a single https URL
            # than against an HLS manifest.
            "format": "bestaudio[protocol^=https]/best[protocol^=https]/bestaudio/best",
            "quiet": True,
            "ignoreerrors": True,
            "cookiefile": _config.MUSIC_COOKIES_FILE,
            "js_runtimes": {"node": {"path": _config.MUSIC_NODE_PATH}},
            "default_search": _config.MUSIC_DEFAULT_SEARCH,
            "noplaylist": not _config.MUSIC_ALLOW_PLAYLISTS,
            "socket_timeout": _config.MUSIC_YTDLP_SOCKET_TIMEOUT,
            "extractor_args": {
                "youtube": {"player_client": _config.MUSIC_PLAYER_CLIENTS},
                # Point the bgutil HTTP provider at the configured host/port so
                # MUSIC_POT_SERVER_HOST/PORT actually work end-to-end.
                "youtubepot-bgutilhttp": {"base_url": [pot_base_url()]},
            },
        }
        if _config.MUSIC_EJS_REMOTE_COMPONENTS:
            self._ydl_base["remote_components"] = ["ejs:github"]

        self._ffmpeg_opts: dict = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            "options": "-vn",
        }

        self.song_queue: deque[tuple[str, str]] = deque()
        self.pending_selection: dict[int, tuple[list[tuple[str, str]], int, float]] = {}
        self.current_volume: float = _config.MUSIC_DEFAULT_VOLUME / 100
        self.loop_enabled: bool = False
        self.last_activity: float = time.time()
        self._play_lock = asyncio.Lock()
        self.current_song: Optional[dict] = None
        self.play_history: deque[tuple[str, str]] = deque(maxlen=10)
        self.previous_song: Optional[tuple[str, str]] = None
        self.skip_votes: set[int] = set()
        self.started_at: float = time.time()
        # Set right before an intentional vc.stop()/disconnect so after_play can
        # tell a deliberate stop from a stream that died mid-track.
        self._intentional_stop: bool = False
        self._stop_requested: bool = False
        # Per-guild autoplay state (in memory; the env var only sets the boot default).
        self._autoplay: dict[int, AutoplayState] = {}
        # Set in cog_load when MUSIC_COOKIE_LISTENER_ENABLED and the listener started.
        self.cookie_listener: Optional[CookieListenerService] = None
        # Dashboard control (internal_api/routes/music.py): the text channel a
        # music command was last used in (where dashboard actions announce),
        # background yt-dlp jobs, fire-and-forget play_next tasks, audit writer.
        self._announce_channel_id: Optional[int] = None
        self.dashboard_jobs = MusicJobs()
        self._bg_tasks: set[asyncio.Task] = set()
        self._audit = None

        self.inactivity_check.start()

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        mute_mentions(ctx)
        command = getattr(getattr(ctx, "command", None), "name", None)
        if getattr(ctx, "guild", None) is not None and command not in _NON_PLAYER_COMMANDS:
            channel_id = getattr(getattr(ctx, "channel", None), "id", None)
            if channel_id:
                self._announce_channel_id = channel_id

    async def cog_load(self) -> None:
        if not _config.MUSIC_COOKIE_LISTENER_ENABLED:
            return
        svc = CookieListenerService.from_config(_config)
        # Never raises or blocks: a busy port warns once and retries in the background.
        await svc.start_background()
        self.cookie_listener = svc
        # Exposed for diagnostics (snapshot()); same pattern as bot.pot_server_service.
        self.bot.cookie_listener_service = svc  # type: ignore[attr-defined]

    async def cog_unload(self) -> None:
        self.inactivity_check.cancel()
        self.dashboard_jobs.cancel_all()
        for task in list(self._bg_tasks):
            task.cancel()
        svc, self.cookie_listener = self.cookie_listener, None
        if svc is not None:
            await svc.stop()
            if getattr(self.bot, "cookie_listener_service", None) is svc:
                self.bot.cookie_listener_service = None  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _clear_current_song(self) -> None:
        self.current_song = None

    def _set_current_song(self, url: str, title: str, duration: Optional[int], requester_id: int = 0) -> None:
        self.current_song = {
            "url": url,
            "title": title,
            "duration": duration,
            "start_time": time.time(),
            "requester_id": requester_id,
            # Pause bookkeeping for the dashboard progress bar (!np keeps its
            # original wall-clock elapsed).
            "paused_at": None,
            "paused_total": 0.0,
        }
        self.skip_votes.clear()

    def _elapsed(self) -> Optional[float]:
        """Seconds of the current track actually played (pauses excluded)."""
        cur = self.current_song
        if not cur:
            return None
        now = time.time()
        paused = float(cur.get("paused_total") or 0.0)
        if cur.get("paused_at"):
            paused += now - cur["paused_at"]
        return max(0.0, now - cur["start_time"] - paused)

    # ------------------------------------------------------------------
    # Live updates (dashboard `music` topic)
    # ------------------------------------------------------------------

    def _notify(self, event: str) -> None:
        """Tell dashboard viewers the player changed. Never raises; safe to call
        from discord.py's player thread (after_play) as well as the loop."""
        payload = {"event": event}
        try:
            loop = getattr(self.bot, "loop", None)
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if loop is not None and running is not loop and not getattr(loop, "is_closed", lambda: True)():
                loop.call_soon_threadsafe(live_events.publish, "music", payload)
            else:
                live_events.publish("music", payload)
        except Exception:
            log.debug("music live publish failed", exc_info=True)

    def _push_history(self, url: str, title: str) -> None:
        self.previous_song = (url, title)
        self.play_history.append((url, title))

    def _reset_playback_state(self, clear_queue: bool = True) -> None:
        self._clear_current_song()
        if clear_queue:
            self.song_queue.clear()
            for st in self._autoplay.values():
                st.reset()
        self.pending_selection.clear()

    def _autoplay_for(self, guild_id: int) -> AutoplayState:
        st = self._autoplay.get(guild_id)
        if st is None:
            st = self._autoplay[guild_id] = AutoplayState(
                _config.MUSIC_AUTOPLAY_DEFAULT, _config.MUSIC_AUTOPLAY_HISTORY
            )
        return st

    @staticmethod
    def _guild_id_of(obj) -> int:
        return getattr(getattr(obj, "guild", None), "id", 0) or 0

    def _expire_stale_selections(self) -> None:
        now = time.time()
        stale = [cid for cid, (_, _, exp) in self.pending_selection.items() if now > exp]
        for cid in stale:
            del self.pending_selection[cid]

    def _update_activity(self) -> None:
        self.last_activity = time.time()

    # ------------------------------------------------------------------
    # Core player actions. The prefix commands and the dashboard
    # (dashboard_* below) both call these, so they behave the same and every
    # change is published on the live `music` topic. Validation and the
    # chat replies stay with each caller.
    # ------------------------------------------------------------------

    def _do_pause(self) -> bool:
        vc = self._get_vc()
        if vc and vc.is_playing():
            vc.pause()
            if self.current_song is not None and not self.current_song.get("paused_at"):
                self.current_song["paused_at"] = time.time()
            self._update_activity()
            self._notify("paused")
            return True
        return False

    def _do_resume(self) -> bool:
        vc = self._get_vc()
        if vc and vc.is_paused():
            vc.resume()
            cur = self.current_song
            if cur is not None and cur.get("paused_at"):
                cur["paused_total"] = float(cur.get("paused_total") or 0.0) + (time.time() - cur["paused_at"])
                cur["paused_at"] = None
            self._update_activity()
            self._notify("resumed")
            return True
        return False

    def _force_skip(self, vc) -> None:
        """Stop the current track on purpose (no autoplay, no retry); after_play
        then starts the next queued one."""
        self.skip_votes.clear()
        self._intentional_stop = True
        vc.stop()
        self._update_activity()
        self._notify("skipped")

    def _do_volume(self, vol: int) -> None:
        self.current_volume = max(0.0, min(vol / 100, 2.0))
        vc = self._get_vc()
        if vc and vc.source:
            vc.source.volume = self.current_volume
        self._update_activity()
        self._notify("volume")

    def _do_loop(self, enabled: bool) -> None:
        self.loop_enabled = bool(enabled)
        self._notify("loop")

    def _do_autoplay(self, guild_id: int, enabled: bool) -> AutoplayState:
        st = self._autoplay_for(guild_id)
        st.enabled = bool(enabled)
        st.consecutive = 0
        st.cap_notified = False
        if not st.enabled and st.auto_urls:
            # Drop any not-yet-started autoplay tracks so "off" really stops it.
            keep = [t for t in self.song_queue if t[0] not in st.auto_urls]
            self.song_queue.clear()
            self.song_queue.extend(keep)
            st.auto_urls.clear()
        self._notify("autoplay")
        return st

    def _do_clear_queue(self) -> int:
        count = len(self.song_queue)
        self.song_queue.clear()
        for st in self._autoplay.values():
            st.auto_urls.clear()
        self._notify("queue")
        return count

    def _do_shuffle(self) -> bool:
        if len(self.song_queue) < 2:
            return False
        queue_list = list(self.song_queue)
        random.shuffle(queue_list)
        self.song_queue.clear()
        self.song_queue.extend(queue_list)
        self._notify("queue")
        return True

    def _do_remove(self, index: int) -> str:
        """Remove 1-based ``index`` (caller validated it); returns the title."""
        queue_list = list(self.song_queue)
        removed_title = queue_list[index - 1][1]
        del queue_list[index - 1]
        self.song_queue.clear()
        self.song_queue.extend(queue_list)
        self._notify("queue")
        return removed_title

    def _do_move(self, from_index: int, to_index: int) -> tuple[str, str]:
        """Move 1-based ``from_index`` to ``to_index`` (caller validated both)."""
        queue_list = list(self.song_queue)
        entry = queue_list.pop(from_index - 1)
        queue_list.insert(to_index - 1, entry)
        self.song_queue.clear()
        self.song_queue.extend(queue_list)
        self._notify("queue")
        return entry

    async def _do_leave(self) -> bool:
        vc = self._get_vc()
        if not vc:
            return False
        if vc.is_playing() or vc.is_paused():
            self._intentional_stop = True
        await vc.disconnect()
        self._reset_playback_state(clear_queue=True)
        self._notify("left")
        return True

    async def _enqueue_url(self, search: str) -> Optional[dict]:
        """Resolve a URL (a track or, if allowed, a playlist) and append it to
        the queue, exactly like `!play <url>`. None when yt-dlp got nothing
        or the URL points at a private/local address (url_block_reason)."""
        blocked = await url_block_reason(search)
        if blocked:
            log.info("music: refused URL (%s)", blocked)
            return None
        url_opts = {**self._ydl_base, "extract_flat": "in_playlist"}
        info = await self._extract_info(url_opts, search)
        if not info:
            return None
        if "entries" in info:
            entries = [e for e in info["entries"] if e and e.get("id")]
            for entry in entries:
                watch_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
                self.song_queue.append((watch_url, entry.get("title") or "Unknown"))
            self._update_activity()
            self._notify("queue")
            return {"playlist": True, "title": info.get("title") or "Unknown", "count": len(entries)}
        watch_url = info.get("webpage_url") or search
        self.song_queue.append((watch_url, info.get("title") or "Unknown"))
        self._update_activity()
        self._notify("queue")
        return {"playlist": False, "title": info.get("title") or "Unknown", "count": 1}

    async def _search(self, search: str) -> list[tuple[str, str]]:
        """Up to 10 (url, title) search results, exactly like `!play <query>`."""
        search_opts = {
            **self._ydl_base,
            "default_search": "ytsearch10",
            "noplaylist": True,
            "extract_flat": "in_playlist",
        }
        info = await self._extract_info(search_opts, search)
        if not info:
            return []
        if "entries" in info:
            results: list[tuple[str, str]] = []
            for e in info["entries"]:
                if not e or not e.get("id"):
                    continue
                title = e.get("title") or e.get("fulltitle")
                if not title:
                    continue
                url = e.get("webpage_url") or f"https://www.youtube.com/watch?v={e['id']}"
                results.append((url, title))
            return results[:10]
        return [(info.get("webpage_url") or search, info.get("title") or info.get("fulltitle") or "Unknown")]

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def _get_vc(self) -> Optional[discord.VoiceClient]:
        return self.bot.voice_clients[0] if self.bot.voice_clients else None

    @staticmethod
    def _is_url(text: str) -> bool:
        return text.startswith("http://") or text.startswith("https://")

    @staticmethod
    def _fmt_duration(seconds: Optional[int]) -> str:
        if seconds is None:
            return "?:??"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    async def _pot_server_up(self) -> Optional[bool]:
        svc = getattr(self.bot, "pot_server_service", None)
        if svc is None or not _config.MUSIC_POT_SERVER_ENABLED:
            return None
        try:
            return await svc._is_up()
        except Exception:
            return None

    async def _extract_info(
        self,
        opts: dict,
        url: str,
        *,
        capture: Optional[_YdlLogCapture] = None,
        diag: Optional[dict] = None,
        allow_cookie_retry: bool = True,
    ) -> Optional[dict]:
        """Run yt-dlp with the timeout wrapper. ``capture``/``diag`` let !debugurl
        see the error text and whether it timed out. If the cookie listener is
        running and the failure looks cookie-related, ask the extension for fresh
        cookies once and retry."""
        svc = self.cookie_listener
        before = svc.updates_total if svc is not None else 0
        capture = capture or _YdlLogCapture()
        diag = diag if diag is not None else {}
        try:
            info = await self._extract_info_inner(opts, url, capture, diag)
        finally:
            if svc is not None and svc.updates_total != before:
                # yt-dlp saves its cookie jar back to the file when it finishes; an
                # update that landed mid-extraction would have been overwritten.
                try:
                    await svc.reassert()
                except Exception:
                    log.exception("cookie reassert failed")
        if (
            info is None
            and allow_cookie_retry
            and svc is not None
            and svc.running
            and classify_extract_error(capture.errors, None, bool(diag.get("timeout"))) in ("cookies_expired", "age_gated")
        ):
            log.info("Extraction looks cookie-related; asking the browser extension for fresh cookies")
            if await svc.request_refresh_and_wait():
                log.info("Fresh cookies received; retrying extraction")
                return await self._extract_info(opts, url, allow_cookie_retry=False)
            log.warning("No cookie refresh arrived from the extension in time")
        return info

    async def _extract_info_inner(
        self, opts: dict, url: str, capture: _YdlLogCapture, diag: dict
    ) -> Optional[dict]:
        loop = asyncio.get_running_loop()
        run_opts = {**opts, "logger": capture}

        def _run() -> Optional[dict]:
            with yt_dlp.YoutubeDL(run_opts) as ydl:
                return ydl.extract_info(url, download=False)

        timeout = _config.MUSIC_EXTRACT_TIMEOUT
        try:
            info = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=timeout)
        except asyncio.TimeoutError:
            diag["timeout"] = True
            log.error(
                "yt-dlp extraction timed out after %ds for %s. Likely a hung network call or a stuck "
                "PO token server (pot server up: %s).",
                timeout, url, await self._pot_server_up(),
            )
            return None
        except yt_dlp.utils.DownloadError as e:
            capture.errors.append(str(e))
            log.error("yt-dlp DownloadError for %s: %s", url, e)
            info = None
        except Exception:
            log.exception("yt-dlp unexpected error for %s", url)
            return None

        if info is None:
            pot_up = await self._pot_server_up()
            log.error(
                "yt-dlp returned nothing for %s -- %s [yt-dlp said: %s]",
                url,
                diagnose_extract_failure(capture.errors, pot_up),
                " | ".join(capture.errors)[:500] or "<no error text>",
            )
        return info

    async def _get_voice_channel(
        self, ctx: commands.Context, channel_id: Optional[str] = None
    ) -> Optional[discord.abc.GuildChannel]:
        if channel_id:
            ch = self.bot.get_channel(int(channel_id))
            if ch:
                return ch
        if ctx.author.voice:
            return ctx.author.voice.channel
        return None

    async def _join_channel(
        self, ctx: commands.Context, channel_id: Optional[str] = None
    ) -> Optional[discord.VoiceClient]:
        existing = self._get_vc()
        if existing and existing.is_connected():
            return existing
        channel = await self._get_voice_channel(ctx, channel_id)
        if not channel:
            await ctx.send("You are not in a voice channel. Use `join <channel_id>` to specify one.")
            return None
        try:
            vc = await channel.connect()
            self._update_activity()
            self._notify("joined")
            name = getattr(channel, "name", None) or f"channel {channel.id}"
            log.info("Joined voice channel: %s", name)
            await ctx.send(f"Joined voice channel: **{name}**")
            return vc
        except Exception as e:
            log.exception("Failed to join voice channel")
            await ctx.send(f"⚠️ Could not join voice channel: {e}")
            return None

    # ------------------------------------------------------------------
    # Queue pagination
    # ------------------------------------------------------------------

    def _build_queue_page(self, page: int) -> tuple[str, int]:
        queue_list = list(self.song_queue)
        total = len(queue_list)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = page % total_pages
        start = page * PAGE_SIZE
        slice_ = queue_list[start:start + PAGE_SIZE]
        # Titles are capped so a full page always fits in one 2000-char message.
        lines = "\n".join(
            f"`{start + i + 1}.` {self._shorten(title, 80)}" for i, (_, title) in enumerate(slice_)
        )
        content = f"📜 **Queue** — page {page + 1}/{total_pages} ({total} tracks)\n{lines}"
        return content, total_pages

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[: limit - 1] + "…"

    # ------------------------------------------------------------------
    # Core playback
    # ------------------------------------------------------------------

    async def _autoplay_refill(self, ctx: commands.Context, vc: discord.VoiceClient) -> int:
        """Queue related tracks (YouTube Mix for the last played video) when the
        queue has run dry. Returns how many were queued; never raises."""
        try:
            st = self._autoplay_for(self._guild_id_of(vc))
            members = getattr(getattr(vc, "channel", None), "members", None) or []
            seed_url = self.previous_song[0] if self.previous_song else ""
            seed_id = extract_video_id(seed_url)
            max_run = _config.MUSIC_AUTOPLAY_MAX_CONSECUTIVE
            reason = autoplay_block_reason(
                enabled=st.enabled,
                intentional=self._intentional_stop,
                humans=sum(1 for m in members if not getattr(m, "bot", False)),
                consecutive=st.consecutive,
                max_consecutive=max_run,
                has_seed=bool(seed_id),
            )
            if reason:
                log.info("Autoplay not queuing: %s", reason)
                if reason == "cap" and not st.cap_notified:
                    st.cap_notified = True
                    await ctx.send(
                        f"🔮 Autoplay paused after {max_run} tracks in a row. Queue something to keep it going."
                    )
                return 0

            limit = min(AUTOPLAY_BATCH_SIZE, max_run - st.consecutive)
            mix_opts = {
                **self._ydl_base,
                "extract_flat": "in_playlist",
                "noplaylist": False,
                "playlist_items": f"1-{AUTOPLAY_MIX_ITEMS}",
            }
            info = await self._extract_info(mix_opts, f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}")
            entries = [e for e in ((info or {}).get("entries") or []) if e and e.get("id")]
            picked = select_autoplay_candidates(entries, set(st.recent), seed_id, limit)
            if not picked:
                log.info("Autoplay: no unplayed related tracks found for %s", seed_id)
                return 0
            for url, title in picked:
                self.song_queue.append((url, title))
                st.auto_urls.add(url)
            log.info("Autoplay queued %d related track(s) for %s", len(picked), seed_id)
            self._notify("queue")
            await ctx.send(f"🔮 Autoplay queued {len(picked)} related track{'s' if len(picked) != 1 else ''}.")
            return len(picked)
        except Exception:
            log.exception("Autoplay refill failed")
            return 0

    async def play_next(
        self, ctx: commands.Context, vc: discord.VoiceClient, allow_autoplay: bool = False
    ) -> None:
        """Start the next queued track. ``allow_autoplay`` is True only when the
        previous track finished on its own (not skip/stop/leave/error), so an
        intentional stop can never trigger a surprise autoplay."""
        async with self._play_lock:
            if not vc.is_connected():
                return
            if vc.is_playing() or vc.is_paused():
                return

            self._clear_current_song()
            stream_opts = {**self._ydl_base, "noplaylist": True}

            if not self.song_queue and allow_autoplay:
                await self._autoplay_refill(ctx, vc)

            while self.song_queue:
                webpage_url, title = self.song_queue.popleft()
                try:
                    info = await self._extract_info(stream_opts, webpage_url)
                    if not info:
                        await ctx.send(f"⚠️ Could not load **{safe_text(title)}**, skipping...")
                        continue

                    stream_url: Optional[str] = info.get("url")
                    if not stream_url:
                        await ctx.send(f"⚠️ No stream URL for **{safe_text(title)}**, skipping...")
                        continue

                    duration: Optional[int] = info.get("duration")
                    log.debug(
                        "Extracted stream for %s: format=%s acodec=%s ext=%s protocol=%s",
                        title,
                        info.get("format"),
                        info.get("acodec"),
                        info.get("ext"),
                        info.get("protocol"),
                    )

                    try:
                        before_options = self._ffmpeg_opts.get("before_options", "")
                        headers = info.get("http_headers")
                        if headers:
                            header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                            before_options = f'{before_options} -headers "{header_str}"'.strip()

                        ffmpeg_opts = {
                            **self._ffmpeg_opts,
                            "before_options": before_options,
                            "stderr": _FFmpegStderrLogger(title),
                        }
                        source = discord.PCMVolumeTransformer(
                            discord.FFmpegPCMAudio(stream_url, **ffmpeg_opts),
                            volume=self.current_volume,
                        )
                    except Exception as e:
                        await ctx.send(f"⚠️ FFmpeg error for **{safe_text(title)}**: {e}, skipping...")
                        continue

                    start_time = time.monotonic()
                    ap = self._autoplay_for(self._guild_id_of(vc))
                    was_auto = webpage_url in ap.auto_urls
                    ap.auto_urls.discard(webpage_url)

                    # If playback dies well short of the known duration (expired
                    # stream URL, dropped connection) re-resolve and replay the
                    # track instead of treating it as finished.
                    retries_left = 2

                    def after_play(
                        err: Optional[Exception],
                        _vc=vc,
                        _url=webpage_url,
                        _title=title,
                        _start=start_time,
                        _duration=duration,
                    ) -> None:
                        nonlocal retries_left
                        played_for = time.monotonic() - _start
                        was_intentional = self._intentional_stop
                        self._intentional_stop = False
                        # Only the dashboard's Stop sets this: a stopped track
                        # must not be re-queued by loop mode.
                        was_stop = self._stop_requested
                        self._stop_requested = False
                        died_early = (
                            not was_intentional
                            and _duration is not None
                            and 2 < played_for < _duration - 5
                        )
                        if err:
                            log.error("Player error after %s (played %.1fs): %s", _title, played_for, err)
                        elif was_intentional:
                            log.info("Stopped intentionally: %s (%.1fs)", _title, played_for)
                        elif died_early:
                            log.warning(
                                "Playback of %s stopped at %.1fs of a %ds track -- looks like the stream "
                                "URL died mid-play (expired signature or dropped connection).",
                                _title, played_for, _duration,
                            )
                        elif played_for < 2:
                            log.warning(
                                "Playback of %s ended almost immediately (%.2fs) with no error reported "
                                "-- check the ffmpeg logger above for the real cause "
                                "(bad stream URL, expired signature, or ffmpeg failing to decode).",
                                _title,
                                played_for,
                            )
                        self._clear_current_song()
                        self._notify("track_end")
                        live_vc = self._get_vc()
                        finished_naturally = (
                            not was_intentional and not err and not died_early and played_for >= 2
                        )
                        if died_early and retries_left > 0 and live_vc and live_vc.is_connected():
                            retries_left -= 1
                            log.info("Retrying %s (%d retries left) with a freshly resolved stream", _title, retries_left)
                            self.song_queue.appendleft((_url, _title))
                            if was_auto:
                                self._autoplay_for(self._guild_id_of(_vc)).auto_urls.add(_url)
                        elif self.loop_enabled and not was_stop:
                            self.song_queue.append((_url, _title))
                        if live_vc and live_vc.is_connected():
                            asyncio.run_coroutine_threadsafe(
                                self.play_next(ctx, live_vc, allow_autoplay=finished_naturally), self.bot.loop
                            )

                    vc.play(source, after=after_play)
                    ap.record_played(webpage_url, autoplay=was_auto)
                    self._set_current_song(webpage_url, title, duration, ctx.author.id)
                    self._push_history(webpage_url, title)
                    self._update_activity()
                    log.info("Now playing: %s", title)
                    self._notify("track_start")
                    await ctx.send(f"🎶 Now playing: **{safe_text(title)}**")
                    return

                except discord.ClientException as e:
                    if "already playing" in str(e).lower():
                        self.song_queue.appendleft((webpage_url, title))
                        return
                    await ctx.send(f"⚠️ Discord error for **{safe_text(title)}**: {e}, skipping...")
                except Exception as e:
                    await ctx.send(f"⚠️ Could not play **{safe_text(title)}**, skipping... ({e})")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if message.channel.id in self.pending_selection:
            results, requester_id, expiry = self.pending_selection[message.channel.id]
            if time.time() > expiry:
                del self.pending_selection[message.channel.id]
            elif message.author.id == requester_id:
                try:
                    index = int(message.content.strip())
                    if 0 <= index < len(results):
                        url, title = results[index]
                        del self.pending_selection[message.channel.id]
                        self.song_queue.append((url, title))
                        self._update_activity()
                        self._notify("queue")
                        await message.channel.send(f"Queued: **{safe_text(title)}**", allowed_mentions=NO_PINGS)
                        vc = self._get_vc()
                        if vc and not vc.is_playing():
                            ctx = await self.bot.get_context(message)
                            mute_mentions(ctx)
                            await self.play_next(ctx, vc)
                        return
                    else:
                        await message.channel.send(f"Pick a number between 0 and {len(results) - 1}.", allowed_mentions=NO_PINGS)
                        return
                except ValueError:
                    pass

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    @tasks.loop(seconds=30)
    async def inactivity_check(self) -> None:
        self._expire_stale_selections()
        for vc in list(self.bot.voice_clients):
            if not vc.is_playing() and time.time() - self.last_activity > _config.MUSIC_INACTIVITY_TIMEOUT:
                log.info("Disconnecting from voice due to inactivity")
                await vc.disconnect()
                self._reset_playback_state(clear_queue=True)
                self._notify("left")

    # ------------------------------------------------------------------
    # Voice commands
    # ------------------------------------------------------------------

    @commands.command()
    async def join(self, ctx: commands.Context, channel_id: Optional[str] = None) -> None:
        """Join your current voice channel, or a specific one by ID."""
        await self._join_channel(ctx, channel_id)

    @commands.command()
    async def leave(self, ctx: commands.Context) -> None:
        """Leave the voice channel and clear the queue."""
        if await self._do_leave():
            await ctx.send("Left voice channel")

    # ------------------------------------------------------------------
    # Playback commands
    # ------------------------------------------------------------------

    @commands.command()
    async def play(self, ctx: commands.Context, *, search: str) -> None:
        """Play a song or playlist. Pass a YouTube URL or a search query."""
        vc = await self._join_channel(ctx)
        if not vc:
            return

        if self._is_url(search):
            queued = await self._enqueue_url(search)
            if queued is None:
                await ctx.send("Could not load that URL.")
                return
            if queued["playlist"]:
                await ctx.send(f"Queued playlist: **{safe_text(queued['title'])}** ({queued['count']} tracks)")
            else:
                await ctx.send(f"Queued: **{safe_text(queued['title'])}**")
            if not vc.is_playing():
                await self.play_next(ctx, vc)
            return

        results = await self._search(search)
        if not results:
            await ctx.send("No results found.")
            return

        expiry = time.time() + SELECTION_TTL
        self.pending_selection[ctx.channel.id] = (results, ctx.author.id, expiry)
        lines = "\n".join(f"`{i}` {self._shorten(title, 150)}" for i, (_, title) in enumerate(results))
        await ctx.send(f"🔎 Pick a result (reply with a number):\n{lines}")

    @commands.command()
    async def pause(self, ctx: commands.Context) -> None:
        """Pause the current song."""
        if self._do_pause():
            await ctx.send("⏸️ Paused")

    @commands.command()
    async def resume(self, ctx: commands.Context) -> None:
        """Resume a paused song."""
        if self._do_resume():
            await ctx.send("▶️ Resumed")

    @commands.command()
    async def skip(self, ctx: commands.Context) -> None:
        """Skip the current song. Instant if you requested it, otherwise requires a majority vote."""
        vc = self._get_vc()
        if not vc or not vc.is_playing():
            await ctx.send("Nothing is playing.")
            return

        requester_id = self.current_song.get("requester_id", 0) if self.current_song else 0

        if ctx.author.id == requester_id:
            self._force_skip(vc)
            await ctx.send("⏭️ Skipped.")
            return

        listeners = [m for m in vc.channel.members if not m.bot and m.id != requester_id]
        needed = max(1, len(listeners) // 2)

        if ctx.author.id in self.skip_votes:
            await ctx.send(f"You already voted to skip. ({len(self.skip_votes)}/{needed})")
            return

        self.skip_votes.add(ctx.author.id)

        if len(self.skip_votes) >= needed:
            self._force_skip(vc)
            await ctx.send("⏭️ Vote passed — skipped.")
        else:
            await ctx.send(f"⏭️ Skip vote: {len(self.skip_votes)}/{needed} needed.")

    @commands.command()
    async def volume(self, ctx: commands.Context, vol: int) -> None:
        """Set the playback volume (0–200)."""
        self._do_volume(vol)
        await ctx.send(f"🔊 Volume set to {vol}%")

    @commands.command()
    async def np(self, ctx: commands.Context) -> None:
        """Show the currently playing song."""
        if not self.current_song:
            await ctx.send("Nothing is playing right now.")
            return

        title = self.current_song["title"]
        duration = self.current_song["duration"]
        elapsed = int(time.time() - self.current_song["start_time"])

        elapsed_fmt = self._fmt_duration(elapsed)
        total_fmt = self._fmt_duration(duration)
        vol_pct = int(self.current_volume * 100)
        loop_status = "Enabled" if self.loop_enabled else "Disabled"
        autoplay_status = "Enabled" if self._autoplay_for(getattr(ctx.guild, "id", 0)).enabled else "Disabled"
        queue_count = len(self.song_queue)

        await ctx.send(
            f"🎵 **Now Playing**\n"
            f"{title}\n\n"
            f"⏱ {elapsed_fmt} / {total_fmt}\n"
            f"🔁 Loop: {loop_status}\n"
            f"🔮 Autoplay: {autoplay_status}\n"
            f"📜 Queue: {queue_count} song{'s' if queue_count != 1 else ''}\n"
            f"🔊 Volume: {vol_pct}%"
        )

    @commands.command()
    async def previous(self, ctx: commands.Context) -> None:
        """Replay the last song that was played."""
        if not self.previous_song:
            await ctx.send("No previous song to replay.")
            return
        vc = await self._join_channel(ctx)
        if not vc:
            return
        url, title = self.previous_song
        self.song_queue.appendleft((url, title))
        self._update_activity()
        self._notify("queue")
        await ctx.send(f"↩️ Replaying: **{safe_text(title)}**")
        if not vc.is_playing():
            await self.play_next(ctx, vc)

    @commands.command()
    async def history(self, ctx: commands.Context) -> None:
        """Show the last 10 songs that were played."""
        if not self.play_history:
            await ctx.send("No songs have been played yet.")
            return
        lines = "\n".join(
            f"`{i + 1}.` {title}" for i, (_, title) in enumerate(reversed(list(self.play_history)))
        )
        for chunk in split_message(f"📖 **Recently Played** (most recent first)\n{lines}", 1990):
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------
    # Queue commands
    # ------------------------------------------------------------------

    @commands.command(name="queue")
    async def queue_cmd(self, ctx: commands.Context) -> None:
        """Show the current queue with pagination."""
        if not self.song_queue:
            await ctx.send("Queue is empty")
            return

        content, total_pages = self._build_queue_page(0)
        msg = await ctx.send(content)

        if total_pages == 1:
            return

        await msg.add_reaction(ARROW_LEFT)
        await msg.add_reaction(ARROW_RIGHT)

        current_page = 0

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                user != self.bot.user
                and reaction.message.id == msg.id
                and str(reaction.emoji) in (ARROW_LEFT, ARROW_RIGHT)
            )

        while True:
            try:
                reaction, user = await self.bot.wait_for("reaction_add", timeout=60.0, check=check)
            except asyncio.TimeoutError:
                try:
                    await msg.clear_reactions()
                except Exception:
                    pass
                break

            total_pages = max(1, (len(self.song_queue) + PAGE_SIZE - 1) // PAGE_SIZE)
            if str(reaction.emoji) == ARROW_RIGHT:
                current_page = (current_page + 1) % total_pages
            else:
                current_page = (current_page - 1) % total_pages

            try:
                await msg.remove_reaction(reaction.emoji, user)
            except Exception:
                pass

            content, _ = self._build_queue_page(current_page)
            await msg.edit(content=content)

    @commands.command()
    async def shuffle(self, ctx: commands.Context) -> None:
        """Randomly shuffle all songs in the queue."""
        if not self._do_shuffle():
            await ctx.send("Not enough songs in the queue to shuffle.")
            return
        await ctx.send("🔀 Queue shuffled.")

    @commands.command(name="loop")
    async def loop_cmd(self, ctx: commands.Context) -> None:
        """Toggle loop mode."""
        self._do_loop(not self.loop_enabled)
        await ctx.send(f"🔁 Loop {'enabled' if self.loop_enabled else 'disabled'}.")

    @commands.command(name="autoplay")
    async def autoplay_cmd(self, ctx: commands.Context, mode: Optional[str] = None) -> None:
        """Toggle autoplay: when the queue empties, related tracks get queued. Usage: !autoplay [on|off]"""
        if ctx.guild is None:
            await ctx.send("Autoplay only works in a server.")
            return
        st = self._autoplay_for(ctx.guild.id)
        arg = (mode or "").strip().lower()
        if arg in ("on", "enable", "true", "1"):
            enabled = True
        elif arg in ("off", "disable", "false", "0"):
            enabled = False
        elif arg == "":
            enabled = not st.enabled
        else:
            await ctx.send("Usage: `!autoplay [on|off]`")
            return
        self._do_autoplay(ctx.guild.id, enabled)
        await ctx.send(
            f"🔮 Autoplay {'enabled' if st.enabled else 'disabled'}."
            + (f" (max {_config.MUSIC_AUTOPLAY_MAX_CONSECUTIVE} in a row)" if st.enabled else ""),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command()
    async def remove_queue(self, ctx: commands.Context) -> None:
        """Clear all songs from the queue."""
        self._do_clear_queue()
        await ctx.send("🗑️ Queue cleared")

    @commands.command()
    async def removequeue(self, ctx: commands.Context) -> None:
        """Alias for remove_queue."""
        await self.remove_queue(ctx)

    @commands.command()
    async def remove(self, ctx: commands.Context, index: int) -> None:
        """Remove a specific song from the queue by its position number."""
        if not self.song_queue:
            await ctx.send("Queue is empty.")
            return
        if index < 1 or index > len(self.song_queue):
            await ctx.send(f"Index out of range. Queue has {len(self.song_queue)} song(s).")
            return
        removed_title = self._do_remove(index)
        await ctx.send(f"🗑️ Removed `{index}.` **{safe_text(removed_title)}**")

    @commands.command()
    async def move(self, ctx: commands.Context, from_index: int, to_index: int) -> None:
        """Move a song from one queue position to another."""
        if not self.song_queue:
            await ctx.send("Queue is empty.")
            return
        n = len(self.song_queue)
        if from_index < 1 or from_index > n:
            await ctx.send(f"Source index out of range. Queue has {n} song(s).")
            return
        if to_index < 1 or to_index > n:
            await ctx.send(f"Destination index out of range. Queue has {n} song(s).")
            return
        if from_index == to_index:
            await ctx.send("Source and destination are the same.")
            return
        entry = self._do_move(from_index, to_index)
        await ctx.send(f"↕️ Moved **{safe_text(entry[1])}** from `{from_index}` to `{to_index}`.")

    # ------------------------------------------------------------------
    # Playlist helpers
    # ------------------------------------------------------------------

    _PLAYLISTS_DIR: str = "playlists"

    def _playlist_path(self, name: str) -> str:
        os.makedirs(self._PLAYLISTS_DIR, exist_ok=True)
        safe = "".join(c for c in name if c.isalnum() or c in "_-")
        if not safe:
            raise ValueError("Invalid playlist name")
        return os.path.join(self._PLAYLISTS_DIR, f"{safe}.json")

    def _save_playlist(self, name: str, tracks: list[tuple[str, str]], created_by: Optional[int] = None) -> None:
        """Write a playlist. Files are {"created_by": id, "tracks": [...]};
        files from before ownership existed are a bare track list (no owner)."""
        import json
        path = self._playlist_path(name)
        data = {
            "created_by": int(created_by) if created_by else None,
            "tracks": [{"url": url, "title": title} for url, title in tracks],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _playlist_entries(data) -> list:
        if isinstance(data, dict):
            data = data.get("tracks") or []
        return [e for e in (data or []) if isinstance(e, dict)]

    def _load_playlist(self, name: str) -> list[tuple[str, str]]:
        import json
        path = self._playlist_path(name)
        if not os.path.exists(path):
            raise FileNotFoundError(f"No playlist named '{name}'")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return [(entry["url"], entry["title"]) for entry in self._playlist_entries(data) if entry.get("url")]

    def _playlist_owner(self, name: str) -> tuple[bool, Optional[int]]:
        """(exists, created_by). Legacy/unreadable files have no owner (None)."""
        import json
        path = self._playlist_path(name)
        if not os.path.exists(path):
            return False, None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return True, None
        owner = data.get("created_by") if isinstance(data, dict) else None
        return True, (int(owner) if isinstance(owner, int) and not isinstance(owner, bool) and owner > 0 else None)

    async def _has_staff_base(self, member) -> bool:
        """staff_base grant (owner / STAFF_ROLE_IDS bypass), as !musicstatus uses."""
        if not isinstance(member, discord.Member):
            return False
        perms = getattr(self.bot, "permission_service", None)
        if perms is None:
            return False
        try:
            return bool(await perms.has_permission(member, "staff_base"))
        except Exception:
            log.exception("music staff_base check failed")
            return False

    async def _may_modify_playlist(self, name: str, member) -> tuple[bool, bool, Optional[int]]:
        """(exists, allowed, owner). A playlist may be overwritten or deleted by
        whoever saved it or by staff_base holders; ownerless (legacy) ones only
        by staff_base. A new name is always allowed."""
        exists, owner = await asyncio.to_thread(self._playlist_owner, name)
        if not exists:
            return False, True, None
        if owner is not None and owner == getattr(member, "id", None):
            return True, True, owner
        return True, await self._has_staff_base(member), owner

    def _list_playlists(self) -> list[str]:
        os.makedirs(self._PLAYLISTS_DIR, exist_ok=True)
        return [f[:-5] for f in os.listdir(self._PLAYLISTS_DIR) if f.endswith(".json")]

    # ------------------------------------------------------------------
    # Playlist commands
    # ------------------------------------------------------------------

    @commands.command(name="save")
    async def save_cmd(self, ctx: commands.Context, *, name: str) -> None:
        """Save the current song + queue as a named playlist."""
        tracks: list[tuple[str, str]] = []
        if self.current_song:
            tracks.append((self.current_song["url"], self.current_song["title"]))
        tracks.extend(self.song_queue)

        if not tracks:
            await ctx.send("Nothing to save — queue is empty and nothing is playing.")
            return

        try:
            _, allowed, owner = await self._may_modify_playlist(name, ctx.author)
        except ValueError:
            await ctx.send("⚠️ Invalid playlist name. Use letters, numbers, `_` or `-` only.")
            return
        if not allowed:
            await ctx.send(
                f"❌ **{safe_text(name)}** belongs to someone else. Only whoever saved it (or staff) can overwrite it; "
                "pick another name."
            )
            return

        try:
            # Overwrites keep the original owner; legacy files are claimed by the (staff) saver.
            self._save_playlist(name, tracks, created_by=owner or ctx.author.id)
        except ValueError:
            await ctx.send("⚠️ Invalid playlist name. Use letters, numbers, `_` or `-` only.")
            return
        self._notify("playlists")

        await ctx.send(f"💾 Saved **{safe_text(name)}** ({len(tracks)} track{'s' if len(tracks) != 1 else ''}).")

    @commands.command(name="load")
    async def load_cmd(self, ctx: commands.Context, *, name: str) -> None:
        """Load a saved playlist into the queue."""
        try:
            tracks = self._load_playlist(name)
        except FileNotFoundError:
            await ctx.send(f"❌ No playlist named **{safe_text(name)}**. Use `!playlists` to see saved ones.")
            return
        except Exception as e:
            await ctx.send(f"⚠️ Could not load playlist: {e}")
            return

        if not tracks:
            await ctx.send(f"⚠️ Playlist **{safe_text(name)}** is empty.")
            return

        vc = await self._join_channel(ctx)
        if not vc:
            return

        for track in tracks:
            self.song_queue.append(track)

        self._update_activity()
        self._notify("queue")
        await ctx.send(f"📂 Loaded **{safe_text(name)}** — queued {len(tracks)} track{'s' if len(tracks) != 1 else ''}.")

        if not vc.is_playing():
            await self.play_next(ctx, vc)

    @commands.command(name="playlists")
    async def playlists_cmd(self, ctx: commands.Context) -> None:
        """List all saved playlists."""
        names = self._list_playlists()
        if not names:
            await ctx.send("No playlists saved yet. Use `!save <name>` to create one.")
            return
        lines = "\n".join(f"  `{n}`" for n in sorted(names))
        for chunk in split_message(f"💿 **Saved playlists:**\n{lines}", 1990):
            await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="delete_playlist")
    async def delete_playlist_cmd(self, ctx: commands.Context, *, name: str) -> None:
        """Delete a saved playlist."""
        try:
            path = self._playlist_path(name)
        except ValueError:
            await ctx.send("⚠️ Invalid playlist name.")
            return
        if not os.path.exists(path):
            await ctx.send(f"❌ No playlist named **{safe_text(name)}**.")
            return
        _, allowed, _ = await self._may_modify_playlist(name, ctx.author)
        if not allowed:
            await ctx.send(f"❌ Only whoever saved **{safe_text(name)}** (or staff) can delete it.")
            return
        os.remove(path)
        self._notify("playlists")
        await ctx.send(f"🗑️ Deleted playlist **{safe_text(name)}**.")

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def _is_status_staff(self, ctx: commands.Context) -> bool:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return False
        perms = getattr(self.bot, "permission_service", None)
        if perms is None:
            return False
        try:
            return bool(await perms.has_permission(ctx.author, "staff_base"))
        except Exception:
            log.exception("musicstatus staff check failed")
            return False

    @commands.command(name="musicstatus", aliases=["status"])
    async def musicstatus_cmd(self, ctx: commands.Context) -> None:
        """Music/PO-token status: pot server, uptime, restarts, cookies, versions. Open to everyone; staff also see technical details."""
        svc = getattr(self.bot, "pot_server_service", None)
        if svc is None:
            await ctx.reply("PO token server service is not available.", mention_author=False)
            return
        staff = await self._is_status_staff(ctx)
        st, kwargs = await self._status_inputs(svc, getattr(ctx.guild, "id", 0), staff)
        text = build_music_status(st, staff=staff, **kwargs)
        await self._send_chunks(ctx, text)

    async def _status_inputs(self, svc, guild_id: int, staff: bool) -> tuple[dict, dict]:
        """Raw pot-server status + the keyword arguments build_music_status takes
        (shared by !musicstatus and the dashboard's status card)."""
        st = await svc.status(log_lines=8 if staff else 0)
        latency = getattr(self.bot, "latency", None)
        ping_ms = round(latency * 1000) if isinstance(latency, (int, float)) and latency == latency else None
        now_playing = self.current_song["title"] if self.current_song else None
        ap = self._autoplay_for(guild_id)
        cookie_refresh = None
        if self.cookie_listener is not None:
            snap = self.cookie_listener.snapshot()
            last = snap.get("last_update_at")
            cookie_refresh = {
                "age_seconds": (time.time() - last) if last else None,
                "poll_age_seconds": (time.time() - snap["last_poll_at"]) if snap.get("last_poll_at") else None,
                # Only used by the staff view:
                "updates_total": snap.get("updates_total"),
                "accepted_count": snap.get("accepted_count"),
                "rejected_count": snap.get("rejected_count"),
                "last_error": snap.get("last_error"),
            }
        return st, {
            "ping_ms": ping_ms,
            "now_playing": now_playing,
            "queue_count": len(self.song_queue),
            "loop_enabled": self.loop_enabled,
            "bot_uptime": time.time() - self.started_at,
            "autoplay": {
                "enabled": ap.enabled,
                "consecutive": ap.consecutive,
                "max": _config.MUSIC_AUTOPLAY_MAX_CONSECUTIVE,
            },
            "cookie_refresh": cookie_refresh,
        }

    async def _send_chunks(self, ctx: commands.Context, text: str, limit: int = 1990) -> None:
        """Reply with the first chunk, then send the rest (Discord-safe, no pings)."""
        none = discord.AllowedMentions.none()
        for i, chunk in enumerate(split_message(text, limit)):
            if i == 0:
                await ctx.reply(chunk, mention_author=False, allowed_mentions=none)
            else:
                await ctx.send(chunk, allowed_mentions=none)

    # ------------------------------------------------------------------
    # Help (!musichelp, /music help): generated from the registered commands
    # ------------------------------------------------------------------

    def _help_commands(self) -> list[commands.Command]:
        return [
            c for c in self.get_commands()
            if not c.hidden and not (c.help or "").startswith("Alias for")
        ]

    def _help_prefix(self) -> str:
        p = getattr(self.bot, "command_prefix", "!")
        return p if isinstance(p, str) else "!"

    def _help_notes(self) -> list[str]:
        prefix = self._help_prefix()
        if self.cookie_listener is None:
            cookies = "off"
        elif self.cookie_listener.running:
            cookies = "active (the browser extension refreshes cookies automatically)"
        else:
            cookies = "waiting for its port to free up"
        return [
            f"`{prefix}musicstatus` shows health, autoplay state and how long ago cookies were last refreshed. "
            f"Cookie auto-refresh: {cookies}.",
            "Commands marked **(staff)** are limited to staff / the bot owner.",
        ]

    def help_text(self, command: Optional[str] = None) -> str:
        """Overview, or details for one command (name or alias)."""
        prefix = self._help_prefix()
        cmds = self._help_commands()
        if command and command.strip():
            cmd = find_music_command(cmds, command, prefix)
            if cmd is None:
                shown = sanitize_status_text(command, 40)
                return f"Unknown music command `{shown}`. Use `{prefix}musichelp` to list them."
            return render_command_help(cmd, prefix)
        return render_music_help(cmds, prefix, self._help_notes())

    @commands.command(name="musichelp", aliases=["mhelp"])
    async def musichelp_cmd(self, ctx: commands.Context, *, command: Optional[str] = None) -> None:
        """Show the music commands, or details for one. Usage: !musichelp [command]"""
        none = discord.AllowedMentions.none()
        for chunk in split_message(self.help_text(command), 1990):
            await ctx.send(chunk, allowed_mentions=none)

    music_group = app_commands.Group(name="music", description="Music commands.")

    @music_group.command(name="help", description="How to use the music commands.")
    @app_commands.describe(command="A music command to get details on")
    async def music_help_slash(self, interaction: discord.Interaction, command: Optional[str] = None) -> None:
        none = discord.AllowedMentions.none()
        chunks = split_message(self.help_text(command), 1990)
        await interaction.response.send_message(chunks[0], ephemeral=True, allowed_mentions=none)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True, allowed_mentions=none)

    @music_help_slash.autocomplete("command")
    async def music_help_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = (current or "").lower().lstrip(self._help_prefix())
        names = sorted({n for c in self._help_commands() for n in (c.name, *c.aliases)})
        return [app_commands.Choice(name=n, value=n) for n in names if cur in n.lower()][:25]

    # ------------------------------------------------------------------
    # !debugurl (owner / staff_base only)
    # ------------------------------------------------------------------

    async def _is_bot_owner(self, ctx: commands.Context) -> bool:
        owner_id = getattr(_config, "OWNER_ID", 0)
        if owner_id and ctx.author.id == owner_id:
            return True
        try:
            return bool(await self.bot.is_owner(ctx.author))
        except Exception:
            return False

    async def _head_probe(self, url: str, headers: Optional[dict]) -> dict:
        host = urlsplit(url).hostname or ""
        if not _host_matches(host, STREAM_PROBE_HOSTS):
            return {"skipped": "not a YouTube/Google stream host"}
        safe_headers = {
            k: v for k, v in (headers or {}).items() if k.lower() not in ("cookie", "authorization")
        }
        started = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.head(url, headers=safe_headers, allow_redirects=False) as resp:
                    return {"status": resp.status, "latency_ms": round((time.monotonic() - started) * 1000)}
        except Exception as e:
            return {"error": type(e).__name__, "latency_ms": round((time.monotonic() - started) * 1000)}

    def _debugurl_refusal(self, target: str) -> Optional[str]:
        """Why !debugurl (or the dashboard's staff debug card) refuses ``target``."""
        hit = check_blocked(target, _config.MUSIC_DEBUGURL_BLOCKED)
        if hit:
            return f"Refused: input matches a blocked term (`{sanitize_status_text(hit, 40)}`)."
        # Anything with a scheme (any case: HTTP://, file:, ftp:) must be an
        # http(s) YouTube link; everything else is only ever a YouTube search.
        has_scheme = re.match(r"^[a-z][a-z0-9+.-]*://|^(?:file|data):", (target or "").strip(), re.IGNORECASE)
        if (self._is_url(target) or has_scheme) and not (
            self._is_url(target.strip().lower()) and is_youtube_url(target.strip())
        ):
            return "Refused: only YouTube URLs (or a search query) can be debugged."
        return None

    async def _debugurl_diagnose(self, target: str) -> dict:
        """Resolve ``target`` exactly like playback would and collect diagnostics."""
        capture = _YdlLogCapture(track_debug=True)
        diag: dict = {}
        started = time.monotonic()
        is_url = self._is_url(target)
        # YouTube's own extractors only: a youtube.com link they don't
        # recognise must NOT fall through to the generic extractor, which
        # would fetch (and follow redirects to) any site from this machine.
        opts = {**self._ydl_base, "noplaylist": True, "allowed_extractors": DEBUGURL_EXTRACTORS}
        info = await self._extract_info(
            opts, target if is_url else f"ytsearch1:{target}",
            capture=capture, diag=diag, allow_cookie_retry=False,
        )
        d: dict = {"elapsed_ms": round((time.monotonic() - started) * 1000), "stream_url": None}
        if info and "entries" in info:
            info = next((e for e in (info.get("entries") or []) if e), None)
        pot_up = await self._pot_server_up()
        d["pot_server_up"] = pot_up
        if not info or not info.get("url"):
            d["error_class"] = classify_extract_error(capture.errors, pot_up, bool(diag.get("timeout")))
            d["error_text"] = " | ".join(capture.errors)[:300] or None
            return d

        stream_url: str = info["url"]
        d.update(
            stream_url=stream_url,
            extractor=info.get("extractor_key") or info.get("extractor"),
            video_id=info.get("id"),
            title=info.get("title"),
            duration=info.get("duration"),
            format_id=info.get("format_id"),
            acodec=info.get("acodec"),
            abr=round(info["abr"]) if isinstance(info.get("abr"), (int, float)) else None,
            tbr=round(info["tbr"]) if isinstance(info.get("tbr"), (int, float)) else None,
            protocol=info.get("protocol"),
            ext=info.get("ext"),
            availability=info.get("availability"),
            age_limit=info.get("age_limit"),
            live_status=info.get("live_status"),
            playable_in_embed=info.get("playable_in_embed"),
            expires_at=url_expiry(stream_url),
        )
        query_names = {n.lower(): v for n, v in parse_qsl(urlsplit(stream_url).query, keep_blank_values=True)}
        d["pot_used"] = "pot" in query_names
        d["url_client"] = query_names.get("c")
        seen: list[str] = []
        for line in capture.debug_lines or []:
            m = re.search(r"Downloading (\S+) player API JSON", line)
            if m and m.group(1) not in seen:
                seen.append(m.group(1))
        d["clients_contacted"] = seen
        d["head"] = await self._head_probe(stream_url, info.get("http_headers"))
        return d

    @commands.command(name="debugurl", extras={"staff_only": True})
    async def debugurl_cmd(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        """(Staff) Resolve a track like playback does and show stream diagnostics; sensitive URL parts are masked. Usage: !debugurl [<query|url>] [--full]"""
        is_owner = await self._is_bot_owner(ctx)
        if not (is_owner or await self._is_status_staff(ctx)):
            await ctx.reply("This command is staff-only.", mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none())
            return

        target, want_full = parse_debugurl_args(query)
        if not target and self.current_song:
            target = self.current_song["url"]
        if not target:
            await ctx.reply("Usage: `!debugurl <query|url> [--full]` (or play something first).",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        refusal = self._debugurl_refusal(target)
        if refusal:
            await ctx.reply(refusal, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        if want_full and not is_owner:
            await ctx.send("`--full` is bot-owner only; showing the masked view instead.",
                           allowed_mentions=discord.AllowedMentions.none())
            want_full = False

        d = await self._debugurl_diagnose(target)
        mask = build_mask_set(_config.MUSIC_DEBUGURL_MASK_PARAMS)
        await self._send_chunks(ctx, build_debugurl_report(d, mask))

        if want_full and d.get("stream_url"):
            # The unmasked URL only ever goes to the owner's DMs, never the channel.
            none = discord.AllowedMentions.none()
            try:
                for chunk in split_message(f"Unmasked stream URL (do not share):\n```\n{d['stream_url']}\n```", 1990):
                    await ctx.author.send(chunk, allowed_mentions=none)
                await ctx.send("Full URL sent to your DMs.", allowed_mentions=none)
            except (discord.Forbidden, discord.HTTPException):
                await ctx.send("Could not DM you the full URL (are your DMs closed?). Nothing was posted here.",
                               allowed_mentions=none)

    # ------------------------------------------------------------------
    # Dashboard control (internal_api/routes/music.py -> /dashboard/music).
    # Drives the SAME player state and core methods as the prefix commands.
    # Every action announces in the music text channel ("via dashboard by
    # <name>", AllowedMentions.none()) and writes a `music_control` audit row.
    # Refusals raise MusicActionError (code + message + HTTP status).
    # ------------------------------------------------------------------

    def _audit_service(self):
        if self._audit is None and getattr(self.bot, "db", None) is not None:
            from database.repositories.audit_repo import AuditRepository
            from services.audit_service import AuditService

            self._audit = AuditService(AuditRepository(self.bot.db))
        return self._audit

    async def _audit_action(self, guild_id: int, actor_id: int, action: str, **detail) -> None:
        svc = self._audit_service()
        if svc is None:
            return
        try:
            await svc.log(
                guild_id, actor_id, "music_control", target_type="music",
                detail={"action": action, "via": "dashboard", **detail},
            )
        except Exception:
            log.exception("music: audit write failed for %s", action)

    def _announce_channel(self, guild):
        """Where dashboard actions announce: the channel a music command was
        last used in, else the connected voice channel's text chat."""
        candidates = []
        if self._announce_channel_id:
            candidates.append(guild.get_channel(self._announce_channel_id))
        vc = self._get_vc()
        if vc and vc.is_connected():
            candidates.append(getattr(vc, "channel", None))
        me = getattr(guild, "me", None)
        for ch in candidates:
            if ch is None or not hasattr(ch, "send"):
                continue
            try:
                perms = ch.permissions_for(me)
                if perms.view_channel and perms.send_messages:
                    return ch
            except Exception:
                continue
        return None

    def _dashboard_ctx(self, guild, actor) -> _DashboardContext:
        return _DashboardContext(self.bot, guild, actor, self._announce_channel(guild))

    @staticmethod
    def _by(actor) -> str:
        name = getattr(actor, "display_name", None) or "a dashboard user"
        return f" (via dashboard by **{discord.utils.escape_markdown(safe_text(name))}**)"

    def _spawn_play_next(self, ctx, vc) -> None:
        """Start the next track in the background (dashboard requests must not
        wait on yt-dlp); same guard as the commands' `if not vc.is_playing()`."""
        if vc.is_playing():
            return
        task = asyncio.get_running_loop().create_task(self.play_next(ctx, vc), name="music-dashboard-play-next")
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    @staticmethod
    def _display_url(url: str) -> str:
        """Queue URLs for the page: YouTube links as-is, anything else without
        its query/fragment (a pasted link could carry a token)."""
        try:
            if is_youtube_url(url):
                return url
            parts = urlsplit(url)
            if parts.scheme in ("http", "https") and parts.hostname:
                return f"{parts.scheme}://{parts.hostname}{parts.path}"
        except ValueError:
            pass
        return ""

    @staticmethod
    def _voice_channel_ok(channel, guild) -> bool:
        return isinstance(channel, (discord.VoiceChannel, discord.StageChannel)) and channel.guild.id == guild.id

    def dashboard_voice_channels(self, guild, actor) -> list[dict]:
        """Voice channels the actor can see, with whether the bot can join."""
        me = guild.me
        out = []
        vc = self._get_vc()
        current_id = getattr(getattr(vc, "channel", None), "id", None) if vc and vc.is_connected() else None
        for ch in sorted(guild.voice_channels + list(getattr(guild, "stage_channels", [])), key=lambda c: c.position):
            if isinstance(actor, discord.Member) and not ch.permissions_for(actor).view_channel:
                continue
            perms = ch.permissions_for(me)
            out.append({
                "channel_id": ch.id,
                "name": ch.name,
                "listeners": sum(1 for m in ch.members if not m.bot),
                "bot_can_join": bool(perms.view_channel and perms.connect and perms.speak),
                "connected": ch.id == current_id,
            })
        return out

    def dashboard_state(self, guild) -> dict:
        vc = self._get_vc()
        connected = bool(vc and vc.is_connected())
        channel = getattr(vc, "channel", None) if connected else None
        ap = self._autoplay_for(guild.id)
        queue = list(self.song_queue)
        cur = self.current_song
        current = None
        if cur:
            current = {
                "title": short(cur.get("title")),
                "url": self._display_url(cur.get("url") or ""),
                "duration": cur.get("duration"),
                "elapsed": round(self._elapsed() or 0.0, 1),
                "requester_id": cur.get("requester_id") or None,
            }
        announce = self._announce_channel(guild)
        prev = self.previous_song
        return {
            "connected": connected,
            "voice_channel": (
                {
                    "channel_id": channel.id,
                    "name": getattr(channel, "name", ""),
                    "listeners": sum(1 for m in getattr(channel, "members", []) if not getattr(m, "bot", False)),
                }
                if channel is not None
                else None
            ),
            "playing": bool(connected and vc.is_playing()),
            "paused": bool(connected and vc.is_paused()),
            "current": current,
            "queue": [
                {"position": i + 1, "title": short(t), "url": self._display_url(u), "autoplay": u in ap.auto_urls}
                for i, (u, t) in enumerate(queue[:STATE_QUEUE_LIMIT])
            ],
            "queue_total": len(queue),
            "volume": round(self.current_volume * 100),
            "loop": self.loop_enabled,
            "autoplay": {
                "enabled": ap.enabled,
                "consecutive": ap.consecutive,
                "max": _config.MUSIC_AUTOPLAY_MAX_CONSECUTIVE,
            },
            "previous": {"title": short(prev[1]), "url": self._display_url(prev[0])} if prev else None,
            "history": [
                {"title": short(t), "url": self._display_url(u)} for u, t in reversed(list(self.play_history))
            ],
            "announce_channel": {"channel_id": announce.id, "name": getattr(announce, "name", "")} if announce else None,
            "inactivity_timeout": _config.MUSIC_INACTIVITY_TIMEOUT,
        }

    async def _dashboard_connect(self, guild, actor, channel_id: Optional[int]):
        """Connected voice client, joining (or moving to) ``channel_id`` or the
        actor's own voice channel. Announces like `!join`."""
        vc = self._get_vc()
        if vc and vc.is_connected():
            if channel_id and getattr(vc.channel, "id", None) != channel_id:
                await self._dashboard_move(guild, actor, vc, channel_id)
            return vc
        channel = None
        if channel_id:
            channel = guild.get_channel(channel_id)
            if not self._voice_channel_ok(channel, guild):
                raise MusicActionError("channel_not_found", "That voice channel does not exist.", 404)
        else:
            voice = getattr(actor, "voice", None)
            channel = getattr(voice, "channel", None)
            if channel is None:
                raise MusicActionError(
                    "no_voice_channel", "The bot is not in a voice channel. Pick one to join first.", 409
                )
        self._check_joinable(guild, actor, channel)
        try:
            vc = await channel.connect(timeout=20.0)
        except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException) as exc:
            log.warning("music: dashboard join of %s failed: %s", channel.id, exc)
            raise MusicActionError("join_failed", "Could not join that voice channel. Try again.", 409)
        self._update_activity()
        self._notify("joined")
        log.info("Joined voice channel (dashboard): %s", channel.name)
        await self._dashboard_ctx(guild, actor).send(f"Joined voice channel: **{safe_text(channel.name)}**{self._by(actor)}")
        await self._audit_action(guild.id, actor.id, "join", channel_id=channel.id)
        return vc

    def _check_joinable(self, guild, actor, channel) -> None:
        if isinstance(actor, discord.Member):
            ap = channel.permissions_for(actor)
            if not (ap.view_channel and ap.connect):
                raise MusicActionError("actor_cannot_join", "You can't join that voice channel yourself.", 403)
        bp = channel.permissions_for(guild.me)
        if not (bp.view_channel and bp.connect and bp.speak):
            raise MusicActionError("bot_missing_permissions", "The bot can't connect or speak in that channel.", 409)

    async def _dashboard_move(self, guild, actor, vc, channel_id: int) -> None:
        channel = guild.get_channel(channel_id)
        if not self._voice_channel_ok(channel, guild):
            raise MusicActionError("channel_not_found", "That voice channel does not exist.", 404)
        self._check_joinable(guild, actor, channel)
        try:
            await vc.move_to(channel)
        except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException) as exc:
            log.warning("music: dashboard move to %s failed: %s", channel.id, exc)
            raise MusicActionError("join_failed", "Could not move to that voice channel. Try again.", 409)
        self._update_activity()
        self._notify("moved")
        await self._dashboard_ctx(guild, actor).send(f"Moved to voice channel: **{safe_text(channel.name)}**{self._by(actor)}")
        await self._audit_action(guild.id, actor.id, "move_channel", channel_id=channel.id)

    async def dashboard_control(
        self,
        guild,
        actor,
        action: str,
        *,
        value=None,
        index: Optional[int] = None,
        to_index: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> dict:
        """One simple player action from the dashboard. Returns {message, state}."""
        by = self._by(actor)
        vc = self._get_vc()
        connected = bool(vc and vc.is_connected())
        msg: str
        detail: dict = {}

        if action == "pause":
            if not self._do_pause():
                raise MusicActionError("not_playing", "Nothing is playing.")
            msg = "⏸️ Paused"
        elif action == "resume":
            if not self._do_resume():
                raise MusicActionError("not_paused", "Nothing is paused.")
            msg = "▶️ Resumed"
        elif action == "skip":
            if not connected or not (vc.is_playing() or vc.is_paused()):
                raise MusicActionError("not_playing", "Nothing is playing.")
            detail["title"] = short((self.current_song or {}).get("title") or "", 100)
            self._force_skip(vc)
            msg = "⏭️ Skipped."
        elif action == "stop":
            cleared = self._do_clear_queue()
            if connected and (vc.is_playing() or vc.is_paused()):
                self._stop_requested = True
                self._force_skip(vc)
            elif not cleared:
                raise MusicActionError("not_playing", "Nothing is playing and the queue is empty.")
            detail["cleared"] = cleared
            msg = "⏹️ Stopped and cleared the queue."
        elif action == "previous":
            if not self.previous_song:
                raise MusicActionError("no_previous", "No previous song to replay.")
            vc = await self._dashboard_connect(guild, actor, channel_id)
            url, title = self.previous_song
            self.song_queue.appendleft((url, title))
            self._update_activity()
            self._notify("queue")
            detail["title"] = short(title, 100)
            ctx = self._dashboard_ctx(guild, actor)
            await ctx.send(f"↩️ Replaying: **{safe_text(title)}**{by}")
            await self._audit_action(guild.id, actor.id, action, **detail)
            self._spawn_play_next(ctx, vc)
            return {"message": f"Replaying {short(title, 100)}", "state": self.dashboard_state(guild)}
        elif action == "shuffle":
            if not self._do_shuffle():
                raise MusicActionError("queue_too_short", "Not enough songs in the queue to shuffle.")
            msg = "🔀 Queue shuffled."
        elif action == "clear":
            detail["cleared"] = self._do_clear_queue()
            msg = "🗑️ Queue cleared"
        elif action == "loop":
            if not isinstance(value, bool):
                raise MusicActionError("invalid_value", "Loop needs true or false.", 400)
            self._do_loop(value)
            detail["enabled"] = value
            msg = f"🔁 Loop {'enabled' if value else 'disabled'}."
        elif action == "autoplay":
            if not isinstance(value, bool):
                raise MusicActionError("invalid_value", "Autoplay needs true or false.", 400)
            st = self._do_autoplay(guild.id, value)
            detail["enabled"] = value
            msg = f"🔮 Autoplay {'enabled' if st.enabled else 'disabled'}." + (
                f" (max {_config.MUSIC_AUTOPLAY_MAX_CONSECUTIVE} in a row)" if st.enabled else ""
            )
        elif action == "volume":
            vol = check_volume(value)
            self._do_volume(vol)
            detail["volume"] = vol
            msg = f"🔊 Volume set to {vol}%"
        elif action == "remove":
            n = len(self.song_queue)
            if not isinstance(index, int) or isinstance(index, bool) or not 1 <= index <= n:
                raise MusicActionError("invalid_index", f"Index out of range. Queue has {n} song(s).", 400)
            title = self._do_remove(index)
            detail.update(index=index, title=short(title, 100))
            msg = f"🗑️ Removed `{index}.` **{safe_text(title)}**"
        elif action == "move":
            n = len(self.song_queue)
            for v in (index, to_index):
                if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= n:
                    raise MusicActionError("invalid_index", f"Index out of range. Queue has {n} song(s).", 400)
            if index == to_index:
                raise MusicActionError("invalid_index", "Source and destination are the same.", 400)
            entry = self._do_move(index, to_index)
            detail.update(index=index, to_index=to_index, title=short(entry[1], 100))
            msg = f"↕️ Moved **{safe_text(entry[1])}** from `{index}` to `{to_index}`."
        elif action == "join":
            if connected and (not channel_id or getattr(vc.channel, "id", None) == channel_id):
                raise MusicActionError("already_connected", "The bot is already in that voice channel.")
            await self._dashboard_connect(guild, actor, channel_id)
            return {"message": "Joined", "state": self.dashboard_state(guild)}
        elif action == "leave":
            if not vc:
                raise MusicActionError("not_connected", "The bot is not in a voice channel.")
            ctx = self._dashboard_ctx(guild, actor)  # resolve the channel before leaving
            await self._do_leave()
            await ctx.send(f"Left voice channel{by}")
            await self._audit_action(guild.id, actor.id, action)
            return {"message": "Left voice channel", "state": self.dashboard_state(guild)}
        else:
            raise MusicActionError("unknown_action", "Unknown action.", 400)

        await self._dashboard_ctx(guild, actor).send(f"{msg}{by}")
        await self._audit_action(guild.id, actor.id, action, **detail)
        return {"message": re.sub(r"[*`]", "", msg), "state": self.dashboard_state(guild)}

    # -- slow lookups (background jobs) --------------------------------------

    async def dashboard_start_play(self, guild, actor, query: str, channel_id: Optional[int]) -> dict:
        """`!play <url>`: join first (fast), then resolve the URL in a job."""
        q = clean_query(query)
        if not self._is_url(q):
            raise MusicActionError("not_a_url", "That is not a URL. Search for it instead.", 400)
        vc = await self._dashboard_connect(guild, actor, channel_id)

        async def _run() -> dict:
            queued = await self._enqueue_url(q)
            if queued is None:
                raise MusicActionError("load_failed", "Could not load that URL.", 422)
            ctx = self._dashboard_ctx(guild, actor)
            if queued["playlist"]:
                await ctx.send(f"Queued playlist: **{safe_text(queued['title'])}** ({queued['count']} tracks){self._by(actor)}")
            else:
                await ctx.send(f"Queued: **{safe_text(queued['title'])}**{self._by(actor)}")
            await self._audit_action(
                guild.id, actor.id, "play", title=short(queued["title"], 100), count=queued["count"],
                playlist=queued["playlist"],
            )
            live = self._get_vc()
            if live and live.is_connected():
                self._spawn_play_next(ctx, live)
            return {"title": short(queued["title"]), "count": queued["count"], "playlist": queued["playlist"]}

        del vc  # joined above; the job re-reads the live voice client
        return self.dashboard_jobs.start(guild.id, "play", _run, actor_id=actor.id)

    async def dashboard_start_search(self, guild, actor, query: str) -> dict:
        """`!play <query>`: the 10 results; picking one queues it (dashboard_pick)."""
        q = clean_query(query)
        if self._is_url(q):
            raise MusicActionError("is_url", "That is a URL. Add it directly instead.", 400)
        private: dict = {}

        async def _run() -> dict:
            results = await self._search(q)
            private["results"] = results
            return {"results": [{"index": i, "title": short(t), "url": self._display_url(u)} for i, (u, t) in enumerate(results)]}

        return self.dashboard_jobs.start(guild.id, "search", _run, actor_id=actor.id, private=private)

    async def dashboard_pick(self, guild, actor, job_id: str, index: int, channel_id: Optional[int]) -> dict:
        job = self.dashboard_jobs.raw(guild.id, job_id)
        if job is None or job.get("kind") != "search":
            raise MusicActionError("job_not_found", "Those search results expired. Search again.", 404)
        if job["_actor_id"] != actor.id:
            # Same rule as Discord: only whoever searched picks from the results.
            raise MusicActionError("not_your_search", "Only the person who searched can pick from these results.", 403)
        results = job["_private"].get("results") or []
        if job["status"] != "done" or not results:
            raise MusicActionError("no_results", "No results to pick from.", 409)
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(results):
            raise MusicActionError("invalid_index", f"Pick a number between 0 and {len(results) - 1}.", 400)
        vc = await self._dashboard_connect(guild, actor, channel_id)
        url, title = results[index]
        self.song_queue.append((url, title))
        self._update_activity()
        self._notify("queue")
        ctx = self._dashboard_ctx(guild, actor)
        await ctx.send(f"Queued: **{safe_text(title)}**{self._by(actor)}")
        await self._audit_action(guild.id, actor.id, "play", title=short(title, 100), count=1, playlist=False)
        self._spawn_play_next(ctx, vc)
        return {"message": f"Queued {short(title, 100)}", "state": self.dashboard_state(guild)}

    async def dashboard_start_debugurl(self, guild, actor, query: Optional[str]) -> dict:
        """Staff-only `!debugurl` (masked view only; never --full)."""
        target = (query or "").strip()
        if target:
            target = clean_query(target)
        elif self.current_song:
            target = self.current_song["url"]
        if not target:
            raise MusicActionError("empty_query", "Enter a URL or query (or play something first).", 400)
        refusal = self._debugurl_refusal(target)
        if refusal:
            raise MusicActionError("refused", refusal, 400)

        async def _run() -> dict:
            d = await self._debugurl_diagnose(target)
            mask = build_mask_set(_config.MUSIC_DEBUGURL_MASK_PARAMS)
            await self._audit_action(guild.id, actor.id, "debugurl", ok=d.get("stream_url") is not None)
            return {
                "ok": d.get("stream_url") is not None,
                "error_class": d.get("error_class"),
                "report": build_debugurl_report(d, mask),
            }

        return self.dashboard_jobs.start(guild.id, "debugurl", _run, actor_id=actor.id)

    def dashboard_job(self, guild_id: int, job_id: str) -> Optional[dict]:
        return self.dashboard_jobs.get(guild_id, job_id)

    # -- playlists -----------------------------------------------------------

    def _playlist_summaries(self) -> list[dict]:
        import json

        out = []
        for name in sorted(self._list_playlists(), key=str.lower):
            count = None
            owner = None
            try:
                with open(self._playlist_path(name), encoding="utf-8") as f:
                    data = json.load(f)
                count = sum(1 for e in self._playlist_entries(data) if e.get("url"))
                owner = data.get("created_by") if isinstance(data, dict) else None
            except Exception:
                owner = None
            out.append({"name": name, "tracks": count, "created_by": owner if isinstance(owner, int) and owner > 0 else None})
        return out

    async def dashboard_playlists(self) -> list[dict]:
        return await asyncio.to_thread(self._playlist_summaries)

    async def dashboard_save_playlist(self, guild, actor, name: str) -> dict:
        safe = safe_playlist_name(name)
        tracks: list[tuple[str, str]] = []
        if self.current_song:
            tracks.append((self.current_song["url"], self.current_song["title"]))
        tracks.extend(self.song_queue)
        if not tracks:
            raise MusicActionError("nothing_to_save", "Nothing to save: the queue is empty and nothing is playing.")
        _, allowed, owner = await self._may_modify_playlist(safe, actor)
        if not allowed:
            raise MusicActionError(
                "playlist_not_yours", "That name belongs to someone else's playlist. Only they (or staff) can overwrite it.", 403
            )
        await asyncio.to_thread(self._save_playlist, safe, tracks, owner or actor.id)
        self._notify("playlists")
        n = len(tracks)
        await self._dashboard_ctx(guild, actor).send(
            f"💾 Saved **{safe_text(safe)}** ({n} track{'s' if n != 1 else ''}).{self._by(actor)}"
        )
        await self._audit_action(guild.id, actor.id, "playlist_save", name=safe, tracks=n)
        return {"message": f"Saved {safe} ({n} tracks)", "name": safe}

    async def dashboard_load_playlist(self, guild, actor, name: str, channel_id: Optional[int]) -> dict:
        safe = safe_playlist_name(name)
        try:
            tracks = await asyncio.to_thread(self._load_playlist, safe)
        except FileNotFoundError:
            raise MusicActionError("playlist_not_found", "No playlist with that name.", 404)
        except Exception:
            log.exception("music: dashboard load of playlist %s failed", safe)
            raise MusicActionError("playlist_unreadable", "That playlist file could not be read.", 409)
        if not tracks:
            raise MusicActionError("playlist_empty", "That playlist is empty.")
        vc = await self._dashboard_connect(guild, actor, channel_id)
        for track in tracks:
            self.song_queue.append(track)
        self._update_activity()
        self._notify("queue")
        n = len(tracks)
        ctx = self._dashboard_ctx(guild, actor)
        await ctx.send(f"📂 Loaded **{safe_text(safe)}** — queued {n} track{'s' if n != 1 else ''}.{self._by(actor)}")
        await self._audit_action(guild.id, actor.id, "playlist_load", name=safe, tracks=n)
        self._spawn_play_next(ctx, vc)
        return {"message": f"Loaded {safe} ({n} tracks)", "state": self.dashboard_state(guild)}

    async def dashboard_delete_playlist(self, guild, actor, name: str) -> dict:
        safe = safe_playlist_name(name)
        path = self._playlist_path(safe)
        if not os.path.exists(path):
            raise MusicActionError("playlist_not_found", "No playlist with that name.", 404)
        _, allowed, _ = await self._may_modify_playlist(safe, actor)
        if not allowed:
            raise MusicActionError("playlist_not_yours", "Only whoever saved this playlist (or staff) can delete it.", 403)
        await asyncio.to_thread(os.remove, path)
        self._notify("playlists")
        await self._dashboard_ctx(guild, actor).send(f"🗑️ Deleted playlist **{safe_text(safe)}**.{self._by(actor)}")
        await self._audit_action(guild.id, actor.id, "playlist_delete", name=safe)
        return {"message": f"Deleted {safe}"}

    # -- status (!musicstatus) ----------------------------------------------

    async def dashboard_status(self, guild, *, staff: bool) -> dict:
        """!musicstatus as structured data. The public view carries only coarse
        health info (same as the Discord public view); ``staff`` adds the
        sanitised technical details. Never file paths, cookies or tokens."""
        svc = getattr(self.bot, "pot_server_service", None)
        if svc is None:
            return {"available": False}
        st, kw = await self._status_inputs(svc, guild.id, staff)
        cookies = st.get("cookies") or {}
        cr = kw.get("cookie_refresh")
        out = {
            "available": True,
            "ping_ms": kw["ping_ms"],
            "bot_uptime_seconds": round(kw["bot_uptime"]) if kw.get("bot_uptime") is not None else None,
            "now_playing": sanitize_status_text(kw["now_playing"], 100) if kw.get("now_playing") else None,
            "queue_count": kw["queue_count"],
            "loop": kw["loop_enabled"],
            "autoplay": kw["autoplay"],
            "pot_server": {
                "state": sanitize_status_text(st.get("state"), 30),
                "responding": bool(st.get("responding")),
                "enabled": bool(st.get("enabled")),
                "uptime_seconds": st.get("uptime_seconds"),
                "restart_count": int(st.get("restart_count") or 0),
            },
            "cookies": {"present": bool(cookies.get("present")), "age_hours": cookies.get("age_hours")},
            "versions": {
                "node": sanitize_status_text(st.get("node_version") or "n/a", 30),
                "ytdlp": sanitize_status_text(st.get("ytdlp_version") or "n/a", 30),
                "bgutil_plugin": sanitize_status_text(st.get("bgutil_plugin_version") or "n/a", 30),
            },
            "cookie_refresh": (
                {"age_seconds": cr.get("age_seconds"), "poll_age_seconds": cr.get("poll_age_seconds")} if cr else None
            ),
            "staff": None,
        }
        if staff:
            out["staff"] = {
                "url": sanitize_status_text(st.get("url") or "n/a", 60),
                "pid": st.get("pid"),
                "adopted_external": bool(st.get("adopted_external")),
                "watchdog": bool(st.get("watchdog")),
                "watchdog_active": bool(st.get("watchdog_active")),
                "last_error": sanitize_status_text(st["last_error"], 200) if st.get("last_error") else None,
                "cookie_listener": (
                    {
                        "updates_total": int(cr.get("updates_total") or 0),
                        "accepted_count": int(cr.get("accepted_count") or 0),
                        "rejected_count": int(cr.get("rejected_count") or 0),
                        "last_error": sanitize_status_text(cr["last_error"], 120) if cr.get("last_error") else None,
                    }
                    if cr
                    else None
                ),
                "log_name": sanitize_status_text(_basename(str(st.get("log_path") or "pot_server.log")), 60),
                "log_tail": [sanitize_status_text(ln, 140) for ln in (st.get("log_tail") or [])][-8:],
            }
        return out


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))
