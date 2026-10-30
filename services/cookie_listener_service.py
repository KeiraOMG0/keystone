"""
Cookie-extension listener for the music cog.

A browser extension (see docs/COOKIE_EXTENSION.md) polls ``GET /needs-refresh``
and, when asked, POSTs the user's live YouTube cookies (Netscape cookies.txt
text) to ``POST /cookies``. This service receives them and atomically replaces
the yt-dlp cookies file so playback keeps working when cookies rotate.

It opens a network listener, so it is locked down:
- loopback bind by default; disabled unless explicitly enabled
- the musicbot-cookie-sync extension's wire API (GET /needs-refresh,
  POST /cookies, default port 8999) so the stock extension works unmodified
- auth: if MUSIC_COOKIE_LISTENER_SECRET is set it is REQUIRED (``X-Cookie-Secret``,
  constant-time compare). If not set, the request must carry an ``Origin`` that is
  a browser-extension origin (optionally narrowed to an ID allowlist); a website
  cannot forge Origin, so drive-by POSTs from web pages are blocked, and
  non-browser callers (no Origin) are refused
- CORS only for ``chrome-extension://`` / ``moz-extension://`` origins; any other
  browser Origin is rejected (so web pages / DNS-rebinding cannot talk to it)
- never blocks or crashes the bot: a busy port logs one warning and binding is
  retried in the background with backoff
- request size cap, per-minute request rate limit
- only cookies for youtube.com / google.com / googlevideo.com (and subdomains,
  which includes accounts.google.com) are kept; everything else is dropped
- atomic write (temp file + os.replace) with one rolling ``.bak`` backup
- cookie values are never logged; only counts and timestamps

Lifecycle is owned by the music cog (``cog_load`` / ``cog_unload``).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections import deque
from typing import Callable, Optional

from aiohttp import web

log = logging.getLogger(__name__)

SECRET_HEADER = "X-Cookie-Secret"
ALLOWED_DOMAINS: tuple[str, ...] = ("youtube.com", "google.com", "googlevideo.com")
ALLOWED_ORIGIN_SCHEMES: tuple[str, ...] = ("chrome-extension://", "moz-extension://")
MAX_COOKIES = 2000
MAX_VALUE_LEN = 4096
MAX_NAME_LEN = 256
_HTTPONLY_PREFIX = "#HttpOnly_"
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class CookieListenerConfigError(RuntimeError):
    """The listener is enabled but misconfigured (e.g. no secret)."""


def domain_allowed(domain: str) -> bool:
    d = domain.strip().lstrip(".").lower()
    if not d or not _DOMAIN_RE.match(d):
        return False
    return any(d == ok or d.endswith("." + ok) for ok in ALLOWED_DOMAINS)


def parse_netscape(body: str) -> tuple[list[str], int, int]:
    """Validate a Netscape cookies.txt body.

    Returns ``(clean_lines, accepted, rejected)``. Comment/blank lines are
    ignored (not counted). Malformed lines and cookies for non-allowed domains
    are dropped and counted as rejected. Nothing here logs or returns values
    other than inside ``clean_lines``.
    """
    clean: list[str] = []
    rejected = 0
    for raw in body.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        prefix = ""
        if line.startswith(_HTTPONLY_PREFIX):
            prefix = _HTTPONLY_PREFIX
            line = line[len(_HTTPONLY_PREFIX):]
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            rejected += 1
            continue
        domain, incl, path, secure, expiry, name, value = parts
        incl, secure = incl.upper(), secure.upper()
        ok = (
            domain_allowed(domain)
            and incl in ("TRUE", "FALSE")
            and secure in ("TRUE", "FALSE")
            and path.startswith("/")
            and expiry.isdigit()
            and 0 < len(name) <= MAX_NAME_LEN
            and len(value) <= MAX_VALUE_LEN
            and not _CTRL_RE.search(domain + path + name + value)
        )
        if not ok or len(clean) >= MAX_COOKIES:
            rejected += 1
            continue
        clean.append("\t".join([prefix + domain.strip().lower(), incl, path, secure, expiry, name, value]))
    return clean, len(clean), rejected


def render_netscape(lines: list[str]) -> str:
    header = ["# Netscape HTTP Cookie File", "# Written by the bot's cookie listener. Do not edit.", ""]
    return "\n".join(header + lines) + "\n"


def write_atomic(path: str, text: str) -> None:
    """Replace ``path`` with ``text`` atomically, keeping ``path + '.bak'``."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cookies-", suffix=".tmp", dir=directory)
    bak_tmp = None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        if os.path.exists(path):
            bak_tmp = path + ".bak.tmp"
            shutil.copy2(path, bak_tmp)
            _replace(bak_tmp, path + ".bak")
            bak_tmp = None
        _replace(tmp, path)
    except BaseException:
        for leftover in (tmp, bak_tmp):
            if leftover:
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        raise


def _replace(src: str, dst: str, attempts: int = 5) -> None:
    # On Windows os.replace can briefly fail if another process (yt-dlp) has the
    # destination open; retry a few times.
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.1)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class CookieListenerService:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8999,
        secret: str = "",
        cookies_path: str = "cookies.txt",
        max_bytes: int = 524288,
        rate_per_min: int = 30,
        extension_ids: Optional[list[str]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.host = host
        self.port = port
        self._secret = secret or ""
        self._secret_digest = hashlib.sha256(self._secret.encode("utf-8")).digest()
        self.cookies_path = cookies_path
        self.max_bytes = max_bytes
        self.rate_per_min = rate_per_min
        self._clock = clock
        self.extension_ids = {i.strip().lower() for i in (extension_ids or []) if i.strip()}
        self._retry_task: Optional[asyncio.Task] = None
        self.bind_pending = False

        self._runner: Optional[web.AppRunner] = None
        self._hits: deque[float] = deque()
        self._write_lock = asyncio.Lock()
        self._needs_refresh = False
        self._refreshed: Optional[asyncio.Event] = None
        self._last_text: Optional[str] = None

        self.last_update_at: Optional[float] = None
        self.accepted_count: int = 0
        self.rejected_count: int = 0
        self.updates_total: int = 0
        self.last_error: Optional[str] = None
        self.last_poll_at: Optional[float] = None
        self._seen_origins: set[str] = set()
        self._reject_log: dict[str, list] = {}  # reason -> [last WARNING time, suppressed count]

    @classmethod
    def from_config(cls, config_module) -> "CookieListenerService":
        return cls(
            host=config_module.MUSIC_COOKIE_LISTENER_HOST,
            port=config_module.MUSIC_COOKIE_LISTENER_PORT,
            secret=config_module.MUSIC_COOKIE_LISTENER_SECRET,
            cookies_path=config_module.MUSIC_COOKIES_FILE,
            max_bytes=config_module.MUSIC_COOKIE_LISTENER_MAX_BYTES,
            rate_per_min=config_module.MUSIC_COOKIE_LISTENER_RATE_PER_MIN,
            extension_ids=config_module.MUSIC_COOKIE_LISTENER_EXTENSION_IDS,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._runner is not None

    @property
    def auth_mode(self) -> str:
        return "secret" if self._secret else "origin"

    async def start(self) -> None:
        """Bind once; raises OSError if the port is unavailable."""
        if self._runner is not None:
            return
        if not _is_loopback(self.host):
            log.warning(
                "Cookie listener bound to non-loopback host %s -- anything that can reach it and "
                "passes auth can replace your cookies file. Prefer 127.0.0.1.",
                self.host,
            )
        self._refreshed = asyncio.Event()
        app = web.Application(middlewares=[self._middleware], client_max_size=self.max_bytes)
        app.router.add_get("/needs-refresh", self._h_needs_refresh)
        app.router.add_post("/cookies", self._h_cookies)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        self.bind_pending = False
        for addr in runner.addresses:
            if isinstance(addr, tuple) and len(addr) >= 2:
                self.port = addr[1]
                break
        if self._secret:
            mode = "shared secret (X-Cookie-Secret) required"
        elif self.extension_ids:
            mode = f"no secret set: browser-extension Origin required, limited to {len(self.extension_ids)} allowed id(s)"
        else:
            mode = "no secret set: any browser-extension Origin accepted (set MUSIC_COOKIE_LISTENER_SECRET to harden)"
        log.info("Cookie listener started on %s:%s; auth mode: %s", self.host, self.port, mode)

    async def start_background(self) -> None:
        """Try to bind; never raises. On failure, warn once and keep retrying in
        the background (backoff capped at 60s) until the port frees up."""
        try:
            await self.start()
            return
        except OSError as e:
            log.warning(
                "Cookie listener could not bind %s:%s (%s). Another process may hold the port "
                "(8999 is the cookie extension's default port). Music keeps working; "
                "will retry in the background.",
                self.host, self.port, type(e).__name__,
            )
        except Exception:
            log.exception("Cookie listener failed to start; will retry in the background")
        self.bind_pending = True
        self._retry_task = asyncio.create_task(self._retry_loop(), name="cookie-listener-retry")

    async def _retry_loop(self) -> None:
        delay = 2.0
        while True:
            await asyncio.sleep(delay)
            try:
                await self.start()
                log.info("Cookie listener bound after retry")
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = min(delay * 2, 60.0)

    async def stop(self) -> None:
        task, self._retry_task = self._retry_task, None
        if task is not None:
            task.cancel()
        self.bind_pending = False
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
            log.info("Cookie listener stopped")

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "bind_pending": self.bind_pending,
            "auth_mode": self.auth_mode,
            "host": self.host,
            "port": self.port,
            "last_update_at": self.last_update_at,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "updates_total": self.updates_total,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "needs_refresh": self._needs_refresh,
        }

    def request_refresh(self) -> None:
        """Flag that fresh cookies are wanted; the extension sees it on its next poll."""
        self._needs_refresh = True

    async def request_refresh_and_wait(self, timeout: float = 45.0) -> bool:
        """Ask the extension for fresh cookies; True if they arrived in time."""
        if not self.running or self._refreshed is None:
            return False
        self._refreshed.clear()
        self._needs_refresh = True
        try:
            await asyncio.wait_for(self._refreshed.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            self._needs_refresh = False
            return False

    async def reassert(self) -> None:
        """Rewrite the last accepted cookies. yt-dlp saves its jar back to the file
        when an extraction ends; if an update landed mid-extraction that would have
        clobbered it, so the cog calls this afterwards."""
        if self._last_text is None:
            return
        async with self._write_lock:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, write_atomic, self.cookies_path, self._last_text
                )
            except OSError as e:
                self.last_error = f"reapply failed: {type(e).__name__}"

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _origin_ok(self, origin: Optional[str]) -> bool:
        """No Origin is fine here (auth decides); a present Origin must be an allowed extension."""
        if origin is None:
            return True
        low = origin.lower()
        if not low.startswith(ALLOWED_ORIGIN_SCHEMES):
            return False
        if self.extension_ids:
            return low.split("://", 1)[1] in self.extension_ids
        return True

    def _cors(self, request: web.Request) -> dict[str, str]:
        origin = request.headers.get("Origin")
        if not origin or not self._origin_ok(origin):
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": f"Content-Type, {SECRET_HEADER}",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }

    def _auth_ok(self, request: web.Request) -> bool:
        if self._secret:
            provided = request.headers.get(SECRET_HEADER, "")
            digest = hashlib.sha256(provided.encode("utf-8", errors="replace")).digest()
            return hmac.compare_digest(digest, self._secret_digest)
        # No secret configured: only a browser extension (which a web page cannot
        # impersonate) may call; _origin_ok already vetted the value.
        return request.headers.get("Origin") is not None

    def _rate_limited(self) -> bool:
        now = self._clock()
        while self._hits and now - self._hits[0] > 60:
            self._hits.popleft()
        if len(self._hits) >= self.rate_per_min:
            return True
        self._hits.append(now)
        return False

    def _text(self, request: web.Request, status: int, text: str = "", extra: Optional[dict] = None) -> web.Response:
        """Plain-text reply, as the extension expects (text/plain bodies)."""
        headers = {**self._cors(request), **(extra or {})}
        return web.Response(status=status, body=text.encode(), content_type="text/plain", headers=headers)

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        if self._rate_limited():
            return self._text(request, 429, "rate limited", {"Retry-After": "60"})
        origin = request.headers.get("Origin")
        if not self._origin_ok(origin):
            self._log_rejection("web or non-allowed Origin")
            return self._text(request, 403, "origin not allowed")
        if request.method == "OPTIONS":
            # CORS preflight: browsers never attach custom headers here, so no auth.
            return web.Response(status=204, headers=self._cors(request))
        if origin and origin.lower() not in self._seen_origins:
            self._seen_origins.add(origin.lower())
            log.info(
                "Cookie listener: first request from extension origin %s "
                "(set MUSIC_COOKIE_LISTENER_EXTENSION_IDS to accept only this id)", origin,
            )
        # The poll is read-only and leaks only {"needsRefresh": bool}; Chrome sends no
        # Origin on it, so it is open (Origin still vetted above). POST needs auth.
        poll = request.method == "GET" and request.path == "/needs-refresh"
        if not poll and not self._auth_ok(request):
            reason = "bad or missing secret" if self._secret else "no extension Origin and no secret"
            self.last_error = f"rejected request: {reason}"
            self._log_rejection(reason)
            return self._text(request, 401, "unauthorized")
        try:
            return await handler(request)
        except web.HTTPRequestEntityTooLarge:
            self.last_error = "rejected request: body too large"
            return self._text(request, 413, "payload too large")
        except web.HTTPException as e:
            return self._text(request, e.status)

    def _log_rejection(self, reason: str) -> None:
        """First rejection of each kind at WARNING, then at most one WARNING per
        10 minutes (with a count of suppressed ones); the rest go to DEBUG."""
        now = self._clock()
        entry = self._reject_log.get(reason)
        if entry is None:
            self._reject_log[reason] = [now, 0]
            log.warning("Cookie listener rejected a request: %s", reason)
        elif now - entry[0] >= 600:
            log.warning("Cookie listener rejected a request: %s (%d more since last report)", reason, entry[1])
            entry[0], entry[1] = now, 0
        else:
            entry[1] += 1
            log.debug("Cookie listener rejected a request: %s", reason)

    async def _h_needs_refresh(self, request: web.Request) -> web.Response:
        first = self.last_poll_at is None
        self.last_poll_at = self._clock()
        if first:
            log.info("Cookie extension connected (first poll received)")
        # The extension's contract: {"needsRefresh": <bool>} as application/json.
        return web.Response(
            status=200,
            body=json.dumps({"needsRefresh": self._needs_refresh}).encode(),
            content_type="application/json",
            headers=self._cors(request),
        )

    async def _h_cookies(self, request: web.Request) -> web.Response:
        if request.content_length is not None and request.content_length > self.max_bytes:
            self.last_error = "rejected request: body too large"
            return self._text(request, 413, "payload too large")
        raw = await request.read()  # also enforced by client_max_size
        if len(raw) > self.max_bytes:
            return self._text(request, 413, "payload too large")
        try:
            body = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.last_error = "rejected request: body is not valid UTF-8"
            return self._text(request, 400, "bad request")
        if not body.strip():
            self.last_error = "rejected request: empty body"
            return self._text(request, 400, "empty cookies payload")

        lines, accepted, rejected = parse_netscape(body)
        if accepted == 0:
            self.last_error = f"rejected update: no acceptable cookies ({rejected} rejected)"
            log.warning("Cookie update rejected: 0 accepted, %d rejected", rejected)
            return self._text(request, 422, "no acceptable cookies", {"X-Cookies-Rejected": str(rejected)})

        text = render_netscape(lines)
        async with self._write_lock:
            try:
                await asyncio.get_running_loop().run_in_executor(None, write_atomic, self.cookies_path, text)
            except OSError as e:
                self.last_error = f"write failed: {type(e).__name__}"
                log.error("Cookie update could not be written: %s", type(e).__name__)
                return self._text(request, 500, "failed to write cookies file")
            self._last_text = text
        self.last_update_at = self._clock()
        self.accepted_count = accepted
        self.rejected_count = rejected
        self.updates_total += 1
        self.last_error = None
        self._needs_refresh = False
        if self._refreshed is not None:
            self._refreshed.set()
        log.info(
            "Cookies refreshed at %s: %d accepted, %d rejected",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_update_at)), accepted, rejected,
        )
        # The extension expects "ok"; counts ride along as additive headers.
        return self._text(
            request, 200, "ok",
            {"X-Cookies-Accepted": str(accepted), "X-Cookies-Rejected": str(rejected)},
        )
