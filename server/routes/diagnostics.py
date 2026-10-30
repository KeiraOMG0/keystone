"""
GET  /api/diagnostics               (STAFF ONLY)
POST /api/diagnostics/cache/clear   (STAFF ONLY; bot down -> 502 internal_api_unavailable)

Full ops dump for the dashboard's Diagnostics page: the bot process's dump
(fetched through server/services/internal_client.py, the only module allowed
to call the internal API) merged with a section about THIS dashboard process
(pid/cpu/memory, sessions aggregates, request metrics, dashboard.db, the
Cloudflare tunnel process and the stack's listening ports).

Gating: require_staff (same guard as the Permissions / Settings pages - the
page can reveal operational detail, so it must not hang off a grantable
permission). One deliberate extension, `_require_staff_even_if_bot_down`:
require_staff itself asks the bot process "is this user staff?", so with the
bot DOWN it would 502 and this page - most useful exactly when things are
broken - could never load. When (and only when) the bot genuinely cannot
answer (no HTTP answer at all, a 5xx, or the guild not cached yet during
startup; see `_bot_unreachable`), we fall back to the same rule the bot applies
(OWNER_ID env, or a role in STAFF_ROLE_IDS) evaluated against the roles cached
on the session at login. Anyone not matching still gets the 502. If the bot
answers with anything else (e.g. 404 "Member ... not found" for someone kicked
or banned since login) the answer is 403, never the cached roles. Residual
risk, accepted: while the bot is really down, cached roles can be up to a
session lifetime (30 days sliding) stale. In bot-down mode the response
carries only dashboard-side data plus `bot: {"reachable": false}`.

Bot down (or any InternalAPIError on the dump fetch) never fails this route:
it returns the dashboard section and `bot.reachable=false`.

HARD LINES: this response never contains session ids, OAuth access/refresh
tokens, SESSION_COOKIE_SECRET, INTERNAL_API_SECRET, DISCORD_CLIENT_SECRET or
the bot token. Session data is exposed only as aggregates (counts, usernames,
timestamps); the sessions table's token columns are never selected. The whole
response additionally passes through `_scrub` (literal secret values + token
shapes).
"""

from __future__ import annotations

import collections
import datetime
import inspect
import os
import platform
import re
import sys
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from server import config
from server.auth.dependencies import require_session
from server.db.engine import async_session_factory
from server.db.models import Session
from server.permissions.guards import require_staff
from server.services import internal_client
from server.services.internal_client import InternalAPIError

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

REDACTED = "<redacted>"
_STARTED_AT = time.time()

# --------------------------------------------------------------------------
# Secret scrubbing (server/ deliberately doesn't import the bot's packages)
# --------------------------------------------------------------------------

_HARD_NAME_RE = re.compile(
    r"(token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|apikey|private[_-]?key|credential|"
    r"authorization|bearer|(?:^|[_-])key$)",
    re.IGNORECASE,
)
_DISCORD_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")


def _secret_values() -> list[str]:
    vals = {v for k, v in os.environ.items() if _HARD_NAME_RE.search(k) and v and len(v) >= 6}
    for v in (config.SESSION_COOKIE_SECRET, config.INTERNAL_API_SECRET, config.DISCORD_CLIENT_SECRET):
        if v and len(v) >= 6:
            vals.add(v)
    return sorted(vals, key=len, reverse=True)


def _scrub(obj: Any) -> Any:
    secrets = _secret_values()

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            s = o
            for v in secrets:
                s = s.replace(v, REDACTED)
            s = _JWT_RE.sub(REDACTED, s)
            s = _DISCORD_TOKEN_RE.sub(REDACTED, s)
            return _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o

    return walk(obj)


def _redact_cmdline(args: list[str]) -> list[str]:
    out: list[str] = []
    mask_next = False
    for raw in args or []:
        arg = str(raw)
        if mask_next:
            mask_next = False
            if not arg.startswith("-"):
                out.append(REDACTED)
                continue
        if arg.startswith("-"):
            name, sep, _val = arg.partition("=")
            if _HARD_NAME_RE.search(name.lstrip("-")):
                out.append(f"{name}={REDACTED}" if sep else arg)
                mask_next = not sep
                continue
        m = re.match(r"^([A-Za-z_][\w.-]*)=(.*)$", arg, re.DOTALL)
        if m and _HARD_NAME_RE.search(m.group(1)):
            out.append(f"{m.group(1)}={REDACTED}")
            continue
        out.append(arg)
    return out


# --------------------------------------------------------------------------
# Request metrics middleware (pure ASGI; registered in server/app.py)
# --------------------------------------------------------------------------

class _Metrics:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.total = 0
        self.inflight = 0
        self.status_class: collections.Counter = collections.Counter()
        self.durations: collections.deque = collections.deque(maxlen=1000)
        self.routes: dict[str, dict[str, float]] = {}

    def record(self, method: str, route: str, status: int, ms: float) -> None:
        self.total += 1
        self.status_class[f"{status // 100}xx"] += 1
        self.durations.append(ms)
        r = self.routes.setdefault(f"{method} {route}", {"count": 0, "total_ms": 0.0, "max_ms": 0.0, "errors": 0})
        r["count"] += 1
        r["total_ms"] += ms
        r["max_ms"] = max(r["max_ms"], ms)
        if status >= 500:
            r["errors"] += 1

    def snapshot(self) -> dict[str, Any]:
        d = sorted(self.durations)

        def pct(q: float) -> float | None:
            return round(d[min(len(d) - 1, int(len(d) * q))], 1) if d else None

        top = sorted(self.routes.items(), key=lambda kv: -kv[1]["count"])[:25]
        return {
            "since": _iso(self.started_at),
            "total_requests": self.total,
            "in_flight": self.inflight,
            "status_classes": dict(self.status_class),
            "latency_ms": {"samples": len(d), "p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99), "max": round(d[-1], 1) if d else None},
            "routes": [
                {
                    "route": k, "count": int(v["count"]), "avg_ms": round(v["total_ms"] / v["count"], 1),
                    "max_ms": round(v["max_ms"], 1), "errors": int(v["errors"]),
                }
                for k, v in top
            ],
        }


METRICS = _Metrics()


class RequestMetricsMiddleware:
    """Counts requests + latency per ROUTE TEMPLATE (never the raw path, so
    appeal tokens / ids in URLs are not recorded)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status_holder = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        METRICS.inflight += 1
        t0 = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            METRICS.inflight -= 1
            route = getattr(scope.get("route"), "path", None) or "(unmatched)"
            METRICS.record(scope.get("method", "?"), route, status_holder["code"], (time.perf_counter() - t0) * 1000)


# --------------------------------------------------------------------------
# Dashboard-process section
# --------------------------------------------------------------------------

def _iso(ts: float | None) -> str | None:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat() if ts else None


def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _nt(t):
    return dict(t._asdict()) if t is not None else None


_PROC = None


def _proc():
    global _PROC
    if _PROC is None:
        _PROC = psutil.Process(os.getpid())
        _PROC.cpu_percent(None)
        time.sleep(0.2)
    return _PROC


def _addr(a) -> str | None:
    return f"{a.ip}:{a.port}" if a else None


def _process_part() -> dict[str, Any]:
    if psutil is None:
        return {"error": "psutil is not installed (pip install psutil)"}
    p = _proc()
    ncpu = psutil.cpu_count(logical=True) or 1
    with p.oneshot():
        mem = p.memory_info()
        conns = _try(lambda: list((getattr(p, "net_connections", None) or p.connections)(kind="inet")), [])
        by_state = collections.Counter(c.status for c in conns)
        of = _try(p.open_files, [])
        return {
            "pid": p.pid,
            "ppid": _try(p.ppid),
            "name": p.name(),
            "exe": _try(p.exe),
            "cwd": _try(p.cwd),
            "cmdline": _redact_cmdline(_try(p.cmdline, [])),
            "username": _try(p.username),
            "status": p.status(),
            "started_at": _iso(p.create_time()),
            "uptime_seconds": round(time.time() - p.create_time(), 1),
            "cpu_percent": round(p.cpu_percent(None) / ncpu, 1),
            "cpu_times": _nt(_try(p.cpu_times)),
            "rss_bytes": mem.rss,
            "vms_bytes": mem.vms,
            "threads": p.num_threads(),
            "handles": _try(lambda: p.num_handles() if hasattr(p, "num_handles") else p.num_fds()),
            "io": _nt(_try(p.io_counters)),
            "ctx_switches": _nt(_try(p.num_ctx_switches)),
            "open_files": {"total": len(of), "rows": [{"path": f.path} for f in of[:100]]},
            "sockets": {
                "total": len(conns),
                "by_state": dict(by_state),
                "rows": [
                    {"type": "TCP" if c.type == 1 else "UDP", "local": _addr(c.laddr), "remote": _addr(c.raddr), "status": c.status}
                    for c in conns[:100]
                ],
            },
        }


def _stack_part() -> dict[str, Any]:
    """Cloudflare tunnel process(es) and the stack's listening ports."""
    if psutil is None:
        return {"error": "psutil is not installed"}
    ncpu = psutil.cpu_count(logical=True) or 1
    tunnel = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if "cloudflared" not in name:
            continue
        # Only OUR tunnel (named idlesys / started by start_tunnel), not any
        # other cloudflared on the machine.
        cmd = " ".join(_try(proc.cmdline, []) or []).lower()
        if "idlesys" not in cmd and "start_tunnel" not in cmd:
            continue
        try:
            mem = proc.memory_info()
            tunnel.append(
                {
                    "pid": proc.pid, "name": proc.info["name"], "status": proc.status(),
                    "uptime_seconds": round(time.time() - proc.create_time(), 1),
                    "rss_bytes": mem.rss, "threads": proc.num_threads(),
                    "cpu_percent": round(proc.cpu_percent(None) / ncpu, 1),
                    "cmdline": _redact_cmdline(_try(proc.cmdline, [])),
                    "exe": _try(proc.exe),
                }
            )
        except Exception as exc:
            tunnel.append({"pid": proc.pid, "error": f"{type(exc).__name__}: {exc}"})
    tunnel_pids = {t["pid"] for t in tunnel if "pid" in t}
    conns = _try(lambda: psutil.net_connections(kind="inet"), [])
    listening = {}
    for c in conns:
        if c.status == "LISTEN" and c.laddr:
            listening.setdefault(c.laddr.port, c)
    pot_port = int(os.environ.get("MUSIC_POT_SERVER_PORT") or "4416")
    wanted = [
        (config.INTERNAL_API_PORT, "Bot internal API"),
        (config.DASHBOARD_PORT, "Dashboard"),
        (pot_port, "PO-token server"),
    ]
    for c in conns:
        if c.status == "LISTEN" and c.pid in tunnel_pids and c.laddr:
            wanted.append((c.laddr.port, f"cloudflared (pid {c.pid})"))
    rows = []
    for port, label in wanted:
        c = listening.get(port)
        rows.append(
            {
                "port": port, "label": label, "listening": c is not None,
                "bind": _addr(c.laddr) if c else None, "pid": c.pid if c else None,
                "process": _try(lambda: psutil.Process(c.pid).name()) if c and c.pid else None,
            }
        )
    return {"tunnel_processes": tunnel, "tunnel_running": bool(tunnel), "ports": rows}


async def _sessions_part() -> dict[str, Any]:
    """Aggregates only. Never selects token columns or session ids."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    day = now - datetime.timedelta(hours=24)
    async with async_session_factory() as db:
        total = await db.scalar(select(func.count()).select_from(Session))
        active_24h = await db.scalar(select(func.count()).select_from(Session).where(Session.last_seen_at >= day))
        members = await db.scalar(select(func.count()).select_from(Session).where(Session.is_guild_member.is_(True)))
        oldest = await db.scalar(select(func.min(Session.created_at)))
        newest_seen = await db.scalar(select(func.max(Session.last_seen_at)))
        # Who signed in and when, newest first. No per-user session COUNT:
        # sessions idle 30+ days are deleted (Privacy Policy; housekeeping), so
        # a count would only ever be "logins in the last 30 days" and read as a
        # lifetime counter. The page shows name + avatar via /api/resolve.
        last_seen = func.max(Session.last_seen_at).label("last_seen")
        rows = (
            await db.execute(
                select(Session.discord_user_id, func.max(Session.discord_username).label("username"), last_seen)
                .group_by(Session.discord_user_id)
                .order_by(last_seen.desc())
                .limit(50)
            )
        ).all()
    return {
        "total": total,
        "active_24h": active_24h,
        "guild_members": members,
        "oldest_created_at": str(oldest) if oldest else None,
        "last_seen_at": str(newest_seen) if newest_seen else None,
        "max_age_seconds": config.SESSION_MAX_AGE_SECONDS,
        "by_user": [
            {"user_id": str(r.discord_user_id), "username": r.username,
             "last_seen": str(r.last_seen) if r.last_seen else None}
            for r in rows
        ],
    }


def _db_part() -> dict[str, Any]:
    path = config.DASHBOARD_DB_PATH
    return {
        "path": path,
        "size_bytes": _try(lambda: os.path.getsize(path)),
        "wal_bytes": _try(lambda: os.path.getsize(path + "-wal")),
        "modified_at": _try(lambda: _iso(os.path.getmtime(path))),
    }


def _config_part() -> dict[str, Any]:
    def secret(v: str) -> str:
        return "set" if v else "unset"

    return {
        "DASHBOARD_HOST": config.DASHBOARD_HOST,
        "DASHBOARD_PORT": config.DASHBOARD_PORT,
        "DASHBOARD_GUILD_ID": str(config.DASHBOARD_GUILD_ID),
        "FRONTEND_BASE_URL": config.FRONTEND_BASE_URL,
        "DISCORD_OAUTH_REDIRECT_URI": config.DISCORD_OAUTH_REDIRECT_URI,
        "INTERNAL_API_BASE_URL": config.INTERNAL_API_BASE_URL,
        "DASHBOARD_COOKIE_SECURE": config.COOKIE_SECURE_MODE,  # "auto" | "true" | "false"
        "DASHBOARD_SESSION_MAX_AGE_SECONDS": config.SESSION_MAX_AGE_SECONDS,
        "DISCORD_CLIENT_ID": config.DISCORD_CLIENT_ID or "unset",
        "DISCORD_CLIENT_SECRET": secret(config.DISCORD_CLIENT_SECRET),
        "INTERNAL_API_SECRET": secret(config.INTERNAL_API_SECRET),
        "SESSION_COOKIE_SECRET": secret(config.SESSION_COOKIE_SECRET),
    }


def _versions_part() -> dict[str, Any]:
    from importlib import metadata

    def ver(d: str):
        try:
            return metadata.version(d)
        except Exception:
            return None

    out = {"python": sys.version.split()[0], "implementation": platform.python_implementation(), "platform": platform.platform()}
    out.update({d: ver(d) for d in ("fastapi", "starlette", "uvicorn", "SQLAlchemy", "alembic", "httpx", "aiosqlite", "psutil", "slowapi", "itsdangerous")})
    return out


async def _section(fn) -> Any:
    try:
        res = fn()
        return await res if inspect.isawaitable(res) else res
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}


async def dashboard_section(request: Request | None = None) -> dict[str, Any]:
    import asyncio

    proc, stack = await asyncio.gather(
        _section(lambda: asyncio.to_thread(_process_part)),
        _section(lambda: asyncio.to_thread(_stack_part)),
    )
    routes: list[dict[str, Any]] = []
    if request is not None:
        found: dict[str, set[str]] = {}
        for r in request.app.routes:
            if getattr(r, "path", None):
                found.setdefault(r.path, set()).update(getattr(r, "methods", None) or [])
        try:  # included routers are opaque in app.routes on newer FastAPI; OpenAPI lists them
            for path, ops in request.app.openapi().get("paths", {}).items():
                found.setdefault(path, set()).update(m.upper() for m in ops)
        except Exception:
            pass
        routes = [{"path": p, "methods": sorted(m)} for p, m in sorted(found.items())]
    return {
        "process": proc,
        "sessions": await _section(_sessions_part),
        "database": await _section(_db_part),
        "requests": await _section(METRICS.snapshot),
        "stack": stack,
        "config": await _section(_config_part),
        "versions": await _section(_versions_part),
        "routes": routes,
    }


# --------------------------------------------------------------------------
# Guard + route
# --------------------------------------------------------------------------

def _cached_staff(session: Session) -> bool:
    """Mirror of PermissionService.is_staff using the roles cached at login."""
    owner = int(os.environ.get("OWNER_ID") or "0")
    if owner and session.discord_user_id == owner:
        return True
    staff_roles = {int(x) for x in (os.environ.get("STAFF_ROLE_IDS") or "").split(",") if x.strip().isdigit()}
    return bool(staff_roles & {int(r) for r in (session.cached_role_ids or [])})


# Role-based cached staff (not the owner) is only trusted while it is recent:
# roles cached at a login within this window, or staff confirmed live by the bot
# within it (this process). A staff member demoted after logging in therefore
# cannot keep restarting things through the supervisor for the whole 30-day session.
CACHED_STAFF_MAX_AGE = datetime.timedelta(hours=24)
_staff_confirmed_at: dict[int, float] = {}


def _cached_staff_is_fresh(session: Session) -> bool:
    owner = int(os.environ.get("OWNER_ID") or "0")
    if owner and session.discord_user_id == owner:
        return True
    hit = _staff_confirmed_at.get(int(session.discord_user_id))
    if hit is not None and time.monotonic() - hit <= CACHED_STAFF_MAX_AGE.total_seconds():
        return True
    created = session.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.now(datetime.timezone.utc) - created <= CACHED_STAFF_MAX_AGE


def _bot_unreachable(exc: InternalAPIError) -> bool:
    """True only when the bot genuinely cannot answer "is this user staff?":
    no HTTP answer at all (connection refused / timeout), a 5xx (bot instance
    not attached yet = 503, Discord API error = 502, crash = 500), or the
    guild not cached yet during startup (404 "Guild ... not found").

    Any other answer - notably 404 "Member ... not found" for someone who left
    or was banned, or 401/422 - means the bot IS up and has an opinion, so the
    cached-role fallback must NOT apply (fail closed: an unrecognised 404
    detail also counts as "bot up")."""
    code = exc.status_code
    if code is None or code >= 500:
        return True
    if code == 404 and isinstance(exc.detail, str) and exc.detail.startswith("Guild "):
        return True
    return False


async def _require_staff_even_if_bot_down(session: Session = Depends(require_session)) -> Session:
    if not session.is_guild_member:
        raise HTTPException(status_code=403, detail={"error": "not_a_member"})
    try:
        confirmed = await require_staff(session)
        _staff_confirmed_at[int(session.discord_user_id)] = time.monotonic()
        return confirmed
    except HTTPException as exc:
        cause = exc.__cause__ or exc.__context__
        cause = cause if isinstance(cause, InternalAPIError) else None
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        if not (exc.status_code == 502 and detail.get("error") == "internal_api_unavailable" and cause is not None):
            raise
        if not _bot_unreachable(cause):
            # The bot answered but could not confirm staff (e.g. the member was
            # kicked/banned since login): forbidden, never the cached roles.
            raise HTTPException(status_code=403, detail={
                "error": "forbidden",
                "required_permission": "staff",
                "message": "This page is restricted to default staff roles or the server owner.",
            }) from cause
        if _cached_staff(session) and _cached_staff_is_fresh(session):
            return session
        raise


@router.get("")
async def get_diagnostics(
    request: Request,
    session: Session = Depends(_require_staff_even_if_bot_down),
) -> dict:
    t0 = time.perf_counter()
    bot: dict[str, Any]
    try:
        dump = await internal_client.get_diagnostics(config.DASHBOARD_GUILD_ID)
        bot = {"reachable": True, "round_trip_ms": round((time.perf_counter() - t0) * 1000), **dump}
    except InternalAPIError as exc:
        bot = {"reachable": False, "error": "internal_api_unavailable", "round_trip_ms": round((time.perf_counter() - t0) * 1000)}
        if exc.status_code:
            bot["status_code"] = exc.status_code
    dash = await dashboard_section(request)
    dash["internal_api"] = {
        "reachable": bot["reachable"],
        "round_trip_ms": bot["round_trip_ms"],
        "base_url": config.INTERNAL_API_BASE_URL,
    }
    return _scrub(
        {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "viewer": {"user_id": str(session.discord_user_id), "username": session.discord_username},
            "bot": bot,
            "dashboard": dash,
        }
    )


@router.post("/cache/clear")
async def clear_diagnostics_cache(session: Session = Depends(_require_staff_even_if_bot_down)) -> dict:
    """Staff-only "Clear cached data" button (Diagnostics > System). Empties
    the bot's diagnostics caches (TTL cache, node version, log tails) through
    the internal API, then resets the live hub's per-connection delta state so
    every open Diagnostics page gets a full fresh snapshot on the next push
    (the push loop is woken right away). Same gate as GET (CSRF Origin check
    via the global middleware). The bot is the only cache holder, so with the
    bot down this answers 502 internal_api_unavailable with a clear message
    instead of pretending it worked. Read-only ops action: logged bot-side,
    no audit_log row (same as the supervisor restart controls)."""
    requested_by = f"{session.discord_username or 'unknown'} ({session.discord_user_id})"
    try:
        result = await internal_client.clear_diagnostics_cache(config.DASHBOARD_GUILD_ID, requested_by)
    except InternalAPIError as exc:
        raise HTTPException(status_code=502, detail={
            "error": "internal_api_unavailable",
            "message": "The bot is unreachable, so its diagnostics cache could not be cleared. Try again once it is back.",
        }) from exc
    from server.live.hub import hub

    hub.reset_diagnostics_state()
    return {
        "ok": True,
        "cleared": [str(k) for k in (result.get("cleared") or [])][:100],
        "entries": int(result.get("entries") or 0),
        "log_cache_cleared": bool(result.get("log_cache_cleared")),
        "at": result.get("at"),
    }


# Public name for other staff-only ops routes that must keep working while the
# bot is down (server/routes/supervisor.py: restart controls).
require_staff_even_if_bot_down = _require_staff_even_if_bot_down
scrub = _scrub
