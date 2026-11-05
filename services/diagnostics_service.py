"""
Diagnostics dump for the STAFF-ONLY dashboard Diagnostics page.

Consumed by internal_api/routes/diagnostics.py. Everything here is read-only
(no Discord API calls, no writes, no signals to any process).

Disclosure policy
-----------------
The page is staff-only, so operational detail is shown freely: full file
paths, full child command lines, PIDs, real (non-secret) config values,
per-cookie metadata, log tails, socket tables.

HARD LINES (never in the output, enforced by redaction + tests):
  * the Discord bot token, DISCORD_CLIENT_SECRET, INTERNAL_API_SECRET,
    SESSION_COOKIE_SECRET and the value of ANY env var / flag whose name looks
    like *_SECRET / *_TOKEN / *_PASSWORD / *API_KEY / credential / bearer;
  * anything shaped like a Discord token, JWT, "Bearer xyz" or Cookie header;
  * raw cookie VALUES from cookies.txt (only length + first 2 chars masked,
    e.g. "ab...(len 34)");
  * dashboard session ids / OAuth access+refresh tokens (dashboard side).
Defence in depth: the whole finished dump is walked by `scrub_payload`.

Each section is collected independently (`_run_section`): a failure yields
`{"error": "..."}` for that section only. Slow-changing / expensive parts
(stack scan, children, Discord registry + permissions, the database section,
cookies, config + versions, GC census, route listing) are cached 10-300s in
ONE TTL cache (`heavy`, TTLs in the TTL_* constants) and stamped with an
`as_of`; `clear_caches()` empties it (dashboard "Clear cached data" button).
psutil is optional at runtime.
SQL table names are only ever taken from sqlite_master AND a strict regex.
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import gc
import hashlib
import inspect
import os
import platform
import re
import socket
import sys
import threading
import time
from importlib import metadata
from typing import Any, Callable

try:  # optional dependency: degrade gracefully
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore

import config as _config
from services.psutil_guard import serialized  # every psutil call goes through one lock

REDACTED = "<redacted>"
PROJECT_ROOT = os.path.dirname(os.path.abspath(_config.__file__))

# --------------------------------------------------------------------------
# Redaction (hard lines only)
# --------------------------------------------------------------------------

_HARD_NAME_RE = re.compile(
    r"(token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|apikey|private[_-]?key|"
    r"credential|authorization|bearer|(?:^|[_-])key$)",
    re.IGNORECASE,
)
_DISCORD_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_COOKIE_HDR_RE = re.compile(r"(?i)\b(set-cookie|cookie)\s*:\s*[^\r\n]+")
_KV_SECRET_RE = re.compile(
    r"(?i)\b([\w.-]*(?:token|secret|passw(?:or)?d|api[_-]?key|authorization)[\w.-]*)"
    r"(\s*[=:]\s*)(?!<redacted>)(\"[^\"]*\"|'[^']*'|\S+)"
)
_URL_USERINFO_RE = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")
_OPAQUE_RE = re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_\-+/=]{32,}$")
_WIN_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def is_secret_name(name: str) -> bool:
    return bool(_HARD_NAME_RE.search(name or ""))


def _known_secret_values() -> list[str]:
    """Literal values of every secret-named env var / config attr (>= 6 chars), longest first."""
    vals = {v for k, v in os.environ.items() if is_secret_name(k) and v and len(v) >= 6}
    for attr in ("TOKEN", "INTERNAL_API_SECRET", "DISCORD_CLIENT_SECRET"):
        v = getattr(_config, attr, "")
        if isinstance(v, str) and len(v) >= 6:
            vals.add(v)
    return sorted(vals, key=len, reverse=True)


def redact_text(text: str) -> str:
    """Scrub hard-line secrets out of free text (log lines, exception messages)."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for v in _known_secret_values():
        out = out.replace(v, REDACTED)
    out = _JWT_RE.sub(REDACTED, out)
    out = _DISCORD_TOKEN_RE.sub(REDACTED, out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", out)
    out = _COOKIE_HDR_RE.sub(lambda m: f"{m.group(1)}: {REDACTED}", out)
    out = _URL_USERINFO_RE.sub(r"\1" + REDACTED + "@", out)
    out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    return out


def _clean_value(val: str) -> str:
    if not val:
        return val
    if _WIN_PATH_RE.match(val) or val.startswith("/"):
        return redact_text(val)  # paths are shown in full
    if _OPAQUE_RE.match(val):
        return REDACTED
    return redact_text(val)


def redact_cmdline(args: list[str] | None) -> list[str]:
    """Full argv with only hard-line secrets masked.

    `--token=abc`, `--token abc`, `NAME=value` with a secret-looking NAME,
    Discord-token/JWT shapes and long opaque blobs are masked. File paths
    (including a cookies file path) are shown in full.
    """
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
            name, sep, val = arg.partition("=")
            if is_secret_name(name.lstrip("-")):
                if sep:
                    out.append(f"{name}={REDACTED}")
                else:
                    out.append(arg)
                    mask_next = True
                continue
            out.append(f"{name}={_clean_value(val)}" if sep else arg)
            continue
        m = re.match(r"^([A-Za-z_][\w.-]*)=(.*)$", arg, re.DOTALL)
        if m and not arg.startswith(("http://", "https://")):
            key, val = m.groups()
            out.append(f"{key}={REDACTED if is_secret_name(key) else _clean_value(val)}")
            continue
        out.append(_clean_value(arg))
    return out


def scrub_payload(obj: Any) -> Any:
    """Final pass over the finished dump: mask literal secrets + token shapes."""
    secrets = _known_secret_values()

    def walk(o: Any) -> Any:
        if isinstance(o, str):
            s = o
            for v in secrets:
                s = s.replace(v, REDACTED)
            s = _JWT_RE.sub(REDACTED, s)
            s = _DISCORD_TOKEN_RE.sub(REDACTED, s)
            s = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
            return s
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o

    return walk(obj)


# --------------------------------------------------------------------------
# Config report
# --------------------------------------------------------------------------

# kind: bool | int | str. Names matching a hard secret pattern are set/unset
# only regardless of kind; the explicit "secret" kind also forces that.
CONFIG_ALLOWLIST: dict[str, str] = {
    "DISCORD_TOKEN": "secret", "INTERNAL_API_SECRET": "secret",
    "DISCORD_CLIENT_SECRET": "secret", "SESSION_COOKIE_SECRET": "secret",
    "DISCORD_CLIENT_ID": "str", "DB_PATH": "str", "LOG_LEVEL": "str", "OWNER_ID": "str",
    "STAFF_ROLE_IDS": "str", "UNVERIFIED_ROLE_ID": "str", "VERIFIED_ROLE_ID": "str",
    "UNVERIFIED_CATEGORY_ID": "str", "LOG_CHANNEL_ID": "str", "JOIN_LEAVE_CHANNEL_ID": "str",
    "DASHBOARD_GUILD_ID": "str", "SETUP_GUILD_ID": "str", "APPEAL_BASE_URL": "str",
    "DISCORD_OAUTH_REDIRECT_URI": "str", "FRONTEND_BASE_URL": "str",
    "INTERNAL_API_HOST": "str", "DASHBOARD_HOST": "str", "VERIFY_TEMPLATE_NAME": "str",
    "VERIFY_TICKET_WELCOME_TEMPLATE_NAME": "str", "MUSIC_COOKIES_FILE": "str",
    "MUSIC_NODE_PATH": "str", "MUSIC_DEFAULT_SEARCH": "str", "MUSIC_PLAYER_CLIENTS": "str",
    "MUSIC_POT_SERVER_SCRIPT": "str", "MUSIC_POT_SERVER_HOST": "str",
    "MUSIC_POT_SERVER_NODE": "str", "MUSIC_POT_SERVER_LOG": "str",
    "MUSIC_EJS_REMOTE_COMPONENTS": "str", "OLLAMA_URL": "str", "OLLAMA_MODEL": "str",
    "OLLAMA_EXE": "str",
    "DEBUG": "bool", "AUTO_KICK": "bool", "TICKET_CHAT_LOGGING_ENABLED": "bool",
    "MUSIC_ALLOW_PLAYLISTS": "bool", "MUSIC_POT_SERVER_ENABLED": "bool",
    "MUSIC_POT_SERVER_WATCHDOG": "bool", "DASHBOARD_COOKIE_SECURE": "bool",
    "CHECK_INTERVAL_MINUTES": "int", "TICKET_ARCHIVE_PURGE_DAYS": "int",
    "INTERNAL_API_PORT": "int", "DASHBOARD_PORT": "int", "MUSIC_DEFAULT_VOLUME": "int",
    "MUSIC_INACTIVITY_TIMEOUT": "int", "MUSIC_POT_SERVER_PORT": "int",
    "MUSIC_POT_SERVER_HEALTH_INTERVAL": "int", "MUSIC_POT_SERVER_MAX_RESTARTS": "int",
    "MUSIC_EXTRACT_TIMEOUT": "int", "MUSIC_YTDLP_SOCKET_TIMEOUT": "int",
    "DASHBOARD_SESSION_MAX_AGE_SECONDS": "int",
}


def config_report(environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    out: dict[str, Any] = {}
    for name, kind in CONFIG_ALLOWLIST.items():
        raw = env.get(name)
        if raw is None or raw == "":
            out[name] = "unset"
        elif kind == "secret" or is_secret_name(name):
            out[name] = "set"
        elif kind == "bool":
            out[name] = raw.strip().lower() in ("1", "true", "yes")
        elif kind == "int":
            out[name] = int(raw) if re.fullmatch(r"-?\d{1,12}", raw.strip()) else raw[:200]
        else:
            out[name] = redact_text(raw)[:300]
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def _safe_err(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")[:300]


def _try(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception:
        return default


def _need_psutil() -> None:
    if psutil is None:
        raise RuntimeError("psutil is not installed (pip install psutil)")


def _nt(t: Any) -> dict[str, Any] | None:
    """namedtuple -> dict."""
    return dict(t._asdict()) if t is not None else None


# Heavy cache ---------------------------------------------------------------
#
# ONE TTL cache for every slow-changing / expensive part of the dump. Each
# cached part reports `as_of` + `ttl_seconds` (or `heavy_as_of` +
# `heavy_ttl_seconds` when only PART of a section is cached), which the page
# shows as the "as of" badge. Fast-moving data (process CPU, loop lag, host
# CPU/memory/net counters, gateway latency) is never cached. The dashboard's
# "Clear cached data" button calls `clear_caches()`
# (POST /guilds/{id}/diagnostics/cache/clear).

TTL_RUNTIME = 45  # GC/object census + memory_full_info (process.runtime)
TTL_STACK = 10  # bot-stack process/socket scan (feeds children, system.network, pot server)
TTL_CHILDREN = 15  # per-child psutil rows (children)
TTL_DISCORD_STATIC = 60  # intents, cogs, extensions, listeners, slash commands, bot permissions
TTL_DATABASE = 60  # whole database section: file sizes, migrations, row counts, quick_check
TTL_COOKIES = 60  # cookies.txt parse
TTL_CONFIG = 300  # allowlisted env + package versions (+ node --version, its own 600s cache)
TTL_ROUTES = 300  # internal API route listing (services.internal_api.routes)
TTL_HONEYPOT_STATS = 60

_HEAVY: dict[str, tuple[float, Any, str]] = {}
_HEAVY_LOCKS: dict[str, asyncio.Lock] = {}


def reset_cache() -> None:
    _HEAVY.clear()
    _HEAVY_LOCKS.clear()
    _NODE_CACHE.update(at=0.0, value=None)


def clear_caches() -> dict[str, Any]:
    """Drop every diagnostics cache (the TTL cache, node --version, and the
    log-tail cache) so the next collection recomputes everything. Safe to call
    at any time: an in-flight producer just stores its (fresh) result."""
    keys = sorted(_HEAVY)
    _HEAVY.clear()
    _NODE_CACHE.update(at=0.0, value=None)
    logs = False
    fn = globals().get("clear_log_cache")
    if callable(fn):
        try:
            fn()
            logs = True
        except Exception:
            logs = False
    return {
        "cleared": keys,
        "entries": len(keys),
        "log_cache_cleared": logs,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


async def heavy(key: str, ttl: float, producer: Callable[[], Any]) -> tuple[Any, str, float]:
    """Cache an expensive producer for `ttl` seconds. Returns (value, as_of_iso, ttl)."""
    hit = _HEAVY.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1], hit[2], ttl
    lock = _HEAVY_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _HEAVY.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1], hit[2], ttl
        val = producer()
        if inspect.isawaitable(val):
            val = await val
        as_of = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _HEAVY[key] = (time.monotonic(), val, as_of)
        return val, as_of, ttl


# psutil process cache ------------------------------------------------------

_PROCS: dict[int, Any] = {}
_PROCS_LOCK = threading.Lock()


def get_proc(pid: int | None = None):
    """Cached psutil.Process so cpu_percent(None) measures since the last call."""
    _need_psutil()
    pid = pid or os.getpid()
    with _PROCS_LOCK:
        p = _PROCS.get(pid)
        if p is None:
            p = psutil.Process(pid)
            _PROCS[pid] = p
            p.cpu_percent(None)  # prime; the first reading is meaningless
            p._diag_primed_at = time.monotonic()  # type: ignore[attr-defined]
        if len(_PROCS) > 64:
            for k in [k for k, v in list(_PROCS.items()) if not v.is_running()]:
                _PROCS.pop(k, None)
        return p


def cpu_pct(p) -> float:
    """CPU% of TOTAL machine capacity (Task-Manager style)."""
    ncpu = psutil.cpu_count(logical=True) or 1
    primed = getattr(p, "_diag_primed_at", None)
    if primed is not None:
        wait = 0.2 - (time.monotonic() - primed)
        if wait > 0:
            time.sleep(wait)
        p._diag_primed_at = None
    return round(p.cpu_percent(None) / ncpu, 1)


def proc_basic(p) -> dict[str, Any]:
    cpu = cpu_pct(p)
    with p.oneshot():
        mem = p.memory_info()
        d: dict[str, Any] = {
            "pid": p.pid,
            "name": p.name(),
            "status": p.status(),
            "started_at": _iso(p.create_time()),
            "uptime_seconds": round(time.time() - p.create_time(), 1),
            "cpu_percent": cpu,
            "rss_bytes": mem.rss,
            "vms_bytes": mem.vms,
            "threads": p.num_threads(),
        }
        d["exe"] = _try(p.exe)
        d["cwd"] = _try(p.cwd)
        d["username"] = _try(p.username)
        d["ppid"] = _try(p.ppid)
        d["cpu_times"] = _nt(_try(p.cpu_times))
        d["io"] = _nt(_try(p.io_counters))
        d["ctx_switches"] = _nt(_try(p.num_ctx_switches))
        d["handles"] = _try(lambda: p.num_handles() if hasattr(p, "num_handles") else p.num_fds())
        d["connections"] = len(_conns(p))
    return d


def _conns(p) -> list[Any]:
    fn = getattr(p, "net_connections", None) or getattr(p, "connections", None)
    return list(fn(kind="inet")) if fn else []


def _addr(a: Any) -> str | None:
    if not a:
        return None
    return f"{a.ip}:{a.port}"


def conn_rows(p, limit: int = 100) -> dict[str, Any]:
    conns = _try(lambda: _conns(p), [])
    by_state = collections.Counter(c.status for c in conns)
    rows = [
        {
            "type": "TCP" if c.type == socket.SOCK_STREAM else "UDP",
            "local": _addr(c.laddr),
            "remote": _addr(c.raddr),
            "status": c.status,
        }
        for c in conns[:limit]
    ]
    return {"total": len(conns), "by_state": dict(by_state), "rows": rows}


# --------------------------------------------------------------------------
# Process
# --------------------------------------------------------------------------

async def measure_loop_lag() -> float:
    """ms a 10ms sleep overshoots by: a proxy for event-loop blockage."""
    t0 = time.perf_counter()
    await asyncio.sleep(0.01)
    return round(max(0.0, (time.perf_counter() - t0 - 0.01) * 1000), 2)


@serialized
def _runtime_heavy() -> dict[str, Any]:
    objs = gc.get_objects()
    counts = collections.Counter(type(o).__name__ for o in objs)
    del objs
    p = get_proc()
    mfi = _try(p.memory_full_info)
    return {
        "gc": {
            "counts": list(gc.get_count()),
            "thresholds": list(gc.get_threshold()),
            "stats": gc.get_stats(),
            "enabled": gc.isenabled(),
            "tracked_objects": sum(counts.values()),
            "top_types": [{"type": t, "count": n} for t, n in counts.most_common(15)],
        },
        "memory_full": _nt(mfi),
        "modules_loaded": len(sys.modules),
        "top_level_packages": len({m.split(".")[0] for m in sys.modules}),
    }


def _thread_rows(p) -> list[dict[str, Any]]:
    names = {t.native_id: (t.name, t.daemon) for t in threading.enumerate() if t.native_id}
    rows = []
    for th in _try(p.threads, []):
        nm = names.get(th.id)
        rows.append(
            {
                "id": th.id,
                "name": nm[0] if nm else None,
                "daemon": nm[1] if nm else None,
                "user_time": round(th.user_time, 3),
                "system_time": round(th.system_time, 3),
            }
        )
    rows.sort(key=lambda r: r["user_time"] + r["system_time"], reverse=True)
    return rows[:40]


def _task_rows(loop) -> dict[str, Any]:
    tasks = asyncio.all_tasks(loop)
    by_coro: collections.Counter = collections.Counter()
    rows = []
    for t in tasks:
        coro = t.get_coro()
        cname = getattr(coro, "__qualname__", None) or type(coro).__name__
        by_coro[cname] += 1
        if len(rows) < 80:
            rows.append({"name": t.get_name(), "coro": cname, "done": t.done()})
    return {
        "total": len(tasks),
        "pending": sum(1 for t in tasks if not t.done()),
        "by_coroutine": [{"coro": c, "count": n} for c, n in by_coro.most_common(25)],
        "rows": sorted(rows, key=lambda r: r["name"]),
    }


async def process_section(lag: float | None = None) -> dict[str, Any]:
    if lag is None:
        lag = await measure_loop_lag()
    loop = asyncio.get_running_loop()
    tasks = _task_rows(loop)

    @serialized
    def sync_part() -> dict[str, Any]:
        p = get_proc()
        d = proc_basic(p)
        d["cmdline"] = redact_cmdline(_try(p.cmdline, []))
        of = _try(p.open_files, [])
        d["open_files"] = {"total": len(of), "rows": [{"path": f.path} for f in of[:100]]}
        d["sockets"] = conn_rows(p)
        d["thread_rows"] = _thread_rows(p)
        d["priority"] = _try(p.nice)
        return d

    d = await asyncio.to_thread(sync_part)
    rt, as_of, ttl = await heavy("runtime", TTL_RUNTIME, lambda: asyncio.to_thread(_runtime_heavy))
    d["runtime"] = {**rt, "as_of": as_of, "ttl_seconds": ttl}
    d["python"] = {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "compiler": platform.python_compiler(),
        "executable": sys.executable,
        "prefix": sys.prefix,
        "path": list(sys.path),
        "recursion_limit": sys.getrecursionlimit(),
        "switch_interval": sys.getswitchinterval(),
        "dev_mode": bool(sys.flags.dev_mode),
        "threads_python": threading.active_count(),
    }
    d["platform"] = platform.platform()
    d["working_dir_name"] = os.path.basename(os.getcwd())
    d["asyncio"] = {"loop": type(loop).__name__, "debug": loop.get_debug(), "loop_lag_ms": lag, "tasks": tasks}
    return d


def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name) or ""
    return int(raw) if raw.isdigit() else default


def role_ports() -> dict[int, str]:
    """Configured ports of the bot stack -> role label."""
    ports = {
        _env_port("MUSIC_POT_SERVER_PORT", 4416): "pot server",
        _env_port("MUSIC_COOKIE_LISTENER_PORT", 8999): "cookie listener",
        _env_port("DASHBOARD_PORT", 8080): "dashboard",
    }
    ports[int(getattr(_config, "INTERNAL_API_PORT", 8766))] = "internal API"
    return ports


def _is_our_tunnel(proc) -> bool:
    """cloudflared serving this stack (tunnel named idlesys / start_tunnel script)."""
    name = (proc.info.get("name") or "").lower()
    if "cloudflared" not in name:
        return False
    cmd = " ".join(_try(proc.cmdline, []) or []).lower()
    return "idlesys" in cmd or "start_tunnel" in cmd


@serialized
def build_stack(bot) -> dict[str, Any]:
    """The bot stack, built ONCE per collection: bot + descendants, the pot
    server (spawned or adopted external, found by its port), the dashboard
    (found by its port) + descendants, and our cloudflared tunnel. Returns
    {"pids": {pid: {role, origin}}, "listeners": [...], "connections_by_state", ...}.
    Sockets/processes outside this set are never reported."""
    _need_psutil()
    me = get_proc()
    pids: dict[int, dict[str, str]] = {me.pid: {"role": "bot", "origin": "self"}}
    for c in _try(lambda: me.children(recursive=True), []):
        pids[c.pid] = {"role": "bot child", "origin": "bot child"}

    conns = _try(lambda: psutil.net_connections(kind="inet"), [])
    listen_owner: dict[int, int] = {}
    for c in conns:
        if c.status == "LISTEN" and c.laddr and c.pid:
            listen_owner.setdefault(c.laddr.port, c.pid)
    ports = role_ports()

    def add_external(pid, role: str) -> None:
        if not pid or pid == me.pid:
            return
        if pid in pids:
            pids[pid]["role"] = role  # e.g. bot-spawned pot server
            return
        pids[pid] = {"role": role, "origin": "adopted external"}
        for ch in _try(lambda: psutil.Process(pid).children(recursive=True), []):
            pids.setdefault(ch.pid, {"role": f"{role} child", "origin": "adopted external"})

    svc = getattr(bot, "pot_server_service", None)
    pot_port = next(p for p, r in ports.items() if r == "pot server")
    if svc is not None:
        snap_port = _try(lambda: svc.snapshot(log_lines=0).get("port"))
        if isinstance(snap_port, int):
            pot_port = snap_port
            ports[pot_port] = "pot server"
        add_external(_try(lambda: svc.pid), "pot server")
    add_external(listen_owner.get(pot_port), "pot server")
    dash_port = next(p for p, r in ports.items() if r == "dashboard")
    add_external(listen_owner.get(dash_port), "dashboard")
    tunnel_pids = set()
    for proc in psutil.process_iter(["pid", "name"]):
        if proc.pid not in pids and _is_our_tunnel(proc):
            add_external(proc.pid, "tunnel")
            tunnel_pids.add(proc.pid)

    names: dict[int, str] = {}
    listeners = []
    by_state: collections.Counter = collections.Counter()
    total = 0
    for c in conns:
        if c.pid not in pids:
            continue
        total += 1
        by_state[c.status] += 1
        if c.status == "LISTEN" and c.laddr:
            port = c.laddr.port
            if port in ports:
                role = ports[port]
            elif c.pid in tunnel_pids or pids[c.pid]["role"].startswith("tunnel"):
                role = "tunnel metrics"
            else:
                role = "bot"
            if c.pid not in names:
                names[c.pid] = _try(lambda: psutil.Process(c.pid).name(), "?")
            listeners.append(
                {"local": _addr(c.laddr), "port": port, "pid": c.pid, "process": names[c.pid], "role": role}
            )
    listeners.sort(key=lambda r: r["port"])
    return {
        "pids": pids,
        "listeners": listeners,
        "connections_by_state": dict(by_state),
        "connections_total": total,
    }


@serialized
def children_section(stack) -> dict[str, Any]:
    """Every stack process (role, origin, pid, cmdline, cpu, rss, uptime)."""
    if not stack or "pids" not in stack:
        raise RuntimeError((stack or {}).get("error") or "stack could not be determined")
    by_pid: dict[int, list[str]] = collections.defaultdict(list)
    for l in stack["listeners"]:
        by_pid[l["pid"]].append(l["local"])
    rows: list[dict[str, Any]] = []
    for pid, meta in stack["pids"].items():
        try:
            cp = get_proc(pid)
            row = proc_basic(cp)
            row["cmdline"] = redact_cmdline(_try(cp.cmdline, []))
            row["listening"] = by_pid.get(pid, [])
            row.update(meta)
            rows.append(row)
        except Exception as exc:  # exited / access denied
            rows.append({"pid": pid, "error": _safe_err(exc), **meta})
    rows.sort(key=lambda r: (r.get("origin") != "self", r["pid"]))
    return {"count": len(rows), "processes": rows}


async def build_stack_cached(bot) -> dict[str, Any]:
    """build_stack() (system-wide socket + process scan) at most every TTL_STACK s."""
    val, _as_of, _ttl = await heavy("stack", TTL_STACK, lambda: asyncio.to_thread(build_stack, bot))
    return val


async def children_section_cached(stack) -> dict[str, Any]:
    """children_section() at most every TTL_CHILDREN s (CPU% is then averaged
    over that window). A failed stack is never cached."""
    if not stack or "pids" not in stack:
        return children_section(stack)  # raises the stack's error
    val, as_of, ttl = await heavy("children", TTL_CHILDREN, lambda: asyncio.to_thread(children_section, stack))
    return {**val, "as_of": as_of, "ttl_seconds": ttl}


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------

def _disks() -> list[dict[str, Any]]:
    out = []
    for part in psutil.disk_partitions(all=False):
        if os.name == "nt" and "fixed" not in part.opts:
            continue
        try:
            du = psutil.disk_usage(part.mountpoint)
            out.append(
                {
                    "device": part.device, "mount": part.mountpoint, "fstype": part.fstype,
                    "total_bytes": du.total, "free_bytes": du.free, "percent": du.percent,
                }
            )
        except Exception as exc:
            out.append({"device": part.device, "error": _safe_err(exc)})
    return out


@serialized
def system_section(bot, db_path: str | None) -> dict[str, Any]:
    _need_psutil()
    vm = psutil.virtual_memory()
    sw = _try(psutil.swap_memory)
    freq = _try(psutil.cpu_freq)
    temps = _try(lambda: psutil.sensors_temperatures(), None) if hasattr(psutil, "sensors_temperatures") else None
    out: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "boot_time": _iso(psutil.boot_time()),
        "host_uptime_seconds": round(time.time() - psutil.boot_time(), 0),
        "memory": _nt(vm),
        "swap": _nt(sw),
        "cpu": {
            "logical": psutil.cpu_count(logical=True),
            "physical": psutil.cpu_count(logical=False),
            "system_percent": psutil.cpu_percent(None),
            "per_core_percent": psutil.cpu_percent(None, percpu=True),
            "freq_mhz": _nt(freq),
            "load_avg": _try(lambda: [round(x, 2) for x in psutil.getloadavg()]),
            "times": _nt(_try(psutil.cpu_times)),
            # No psutil.cpu_stats(): it is the call that crashed the bot natively
            # (0xC0000005 in _psutil_windows.pyd, ARCHITECTURE section 21), and
            # nothing showed its numbers.
        },
        "temperatures": (
            {k: [{"label": t.label, "current": t.current} for t in v] for k, v in temps.items()}
            if temps
            else None
        ),
        "disks": _try(_disks, []),
        "disk_io": _nt(_try(psutil.disk_io_counters)),
        "net_io": _nt(_try(psutil.net_io_counters)),
    }
    target = os.path.dirname(os.path.abspath(db_path)) if db_path else os.getcwd()
    try:
        du = psutil.disk_usage(target)
        out["db_disk"] = {"path": target, "total_bytes": du.total, "free_bytes": du.free, "percent": du.percent}
    except Exception as exc:
        out["db_disk"] = {"error": _safe_err(exc)}
    lat = getattr(bot, "latency", None)
    out["discord_latency_ms"] = (
        round(lat * 1000, 1) if isinstance(lat, (int, float)) and lat == lat and lat != float("inf") else None
    )
    out["shards"] = {"shard_count": getattr(bot, "shard_count", None) or 1, "shard_id": getattr(bot, "shard_id", None)}
    return out


async def system_section_async(bot, db_path, stack) -> dict[str, Any]:
    out = await asyncio.to_thread(system_section, bot, db_path)
    if stack and "pids" in stack:
        out["network"] = {
            "scope": "bot stack only",
            "connections_by_state": stack["connections_by_state"],
            "connections_total": stack["connections_total"],
            "listening": stack["listeners"],
        }
    else:
        out["network"] = {"error": "stack could not be determined"}
    return out


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

REQUIRED_GUILD_PERMISSIONS: dict[str, str] = {
    "manage_channels": "Manage Channels",
    "ban_members": "Ban Members",
    "kick_members": "Kick Members",
    "manage_messages": "Manage Messages",
    "manage_roles": "Manage Roles",
    "moderate_members": "Moderate Members",
}


def _lat_ms(x: Any) -> float | None:
    return round(x * 1000, 1) if isinstance(x, (int, float)) and x == x and x != float("inf") else None


def discord_static(bot, guild_id: int) -> dict[str, Any]:
    """The slow-changing part of the Discord section (cached TTL_DISCORD_STATIC s
    by discord_section_cached): intents, cogs, extensions, listener counts,
    slash commands and the bot's guild permissions."""
    out: dict[str, Any] = {"intents": sorted(name for name, enabled in bot.intents if enabled)}
    cogs = []
    for name, cog in sorted(bot.cogs.items()):
        try:
            loops = sum(1 for v in vars(type(cog)).values() if type(v).__name__ == "Loop")
            cogs.append(
                {
                    "name": name,
                    "commands": len(cog.get_commands()),
                    "app_commands": len(cog.get_app_commands()),
                    "listeners": len(cog.get_listeners()),
                    "loops": loops,
                }
            )
        except Exception as exc:
            cogs.append({"name": name, "error": _safe_err(exc)})
    out["cogs"] = cogs
    out["extensions"] = sorted(bot.extensions)
    out["event_listeners"] = {k: len(v) for k, v in sorted(getattr(bot, "extra_events", {}).items())}

    guild = bot.get_guild(guild_id)
    try:
        names = sorted(c.qualified_name for c in bot.tree.walk_commands())
        out["slash_commands"] = {
            "global_top_level": len(bot.tree.get_commands()),
            "global_total": len(names),
            "guild_specific": len(bot.tree.get_commands(guild=guild)) if guild else None,
            "names": names[:300],
            # discord.py does not record whether/when a sync happened and asking
            # Discord is a network call, so this is deliberately not fetched.
            "last_sync": None,
        }
    except Exception as exc:
        out["slash_commands"] = {"error": _safe_err(exc)}

    if guild is None:
        return out
    me = guild.me
    if me is None:
        out["permissions"] = {"error": "bot member not cached in guild"}
        return out
    gp = me.guild_permissions
    held = {label: bool(getattr(gp, attr, False)) for attr, label in REQUIRED_GUILD_PERMISSIONS.items()}
    top = me.top_role
    out["permissions"] = {
        "administrator": bool(gp.administrator),
        "top_role": {"name": top.name, "position": top.position, "of_roles": len(guild.roles)},
        "required": held,
        "missing": [] if gp.administrator else [label for label, ok in held.items() if not ok],
        "all_granted": sorted(name for name, v in gp if v),
    }
    return out


async def discord_section_cached(bot, guild_id: int) -> dict[str, Any]:
    """Live gateway/guild/cache data every call; the static part from the TTL
    cache, stamped heavy_as_of / heavy_ttl_seconds (like the database section)."""
    st, as_of, ttl = await heavy(f"discord-static-{guild_id}", TTL_DISCORD_STATIC, lambda: discord_static(bot, guild_id))
    out = discord_section(bot, guild_id, static=st)
    out["heavy_as_of"] = as_of
    out["heavy_ttl_seconds"] = ttl
    return out


def discord_section(bot, guild_id: int, static: dict[str, Any] | None = None) -> dict[str, Any]:
    user = getattr(bot, "user", None)
    ws = getattr(bot, "ws", None)
    out: dict[str, Any] = {
        "bot_user": {"id": str(user.id), "name": str(user)} if user else None,
        "application_id": str(bot.application_id) if getattr(bot, "application_id", None) else None,
        "ready": bool(bot.is_ready()),
        "closed": bool(bot.is_closed()),
        "latency_ms": _lat_ms(getattr(bot, "latency", None)),
        "guild_count": len(bot.guilds),
        "gateway": {
            "session_id_present": bool(getattr(ws, "session_id", None)),  # the id itself is never shown
            "sequence": getattr(ws, "sequence", None),
            "shard_count": getattr(bot, "shard_count", None) or 1,
        },
        "cache": {
            "users": len(bot.users),
            "guilds": len(bot.guilds),
            "messages": len(bot.cached_messages),
            "emojis": len(bot.emojis),
            "stickers": len(bot.stickers),
            "private_channels": len(bot.private_channels),
            "voice_clients": len(bot.voice_clients),
        },
    }
    voice = []
    for vc in list(bot.voice_clients):
        voice.append(
            {
                "guild_id": str(vc.guild.id) if vc.guild else None,
                "channel": getattr(vc.channel, "name", None),
                "connected": bool(vc.is_connected()),
                "playing": bool(getattr(vc, "is_playing", lambda: False)()),
                "paused": bool(getattr(vc, "is_paused", lambda: False)()),
                "latency_ms": _lat_ms(getattr(vc, "latency", None)),
                "average_latency_ms": _lat_ms(getattr(vc, "average_latency", None)),
            }
        )
    out["voice"] = voice
    out.update(static if static is not None else discord_static(bot, guild_id))

    guild = bot.get_guild(guild_id)
    if guild is None:
        out["guild"] = {"error": f"guild {guild_id} not in cache"}
        return out
    kinds = {"text": 0, "voice": 0, "category": 0, "other": 0}
    for ch in guild.channels:
        t = str(getattr(ch, "type", "other"))
        kinds["text" if t in ("text", "news", "forum") else "voice" if t in ("voice", "stage_voice") else "category" if t == "category" else "other"] += 1
    out["guild"] = {
        "id": str(guild.id),
        "name": guild.name,
        "owner_id": str(guild.owner_id) if guild.owner_id else None,
        "members": guild.member_count,
        "members_cached": len(guild.members),
        "channels": len(guild.channels),
        "channel_breakdown": kinds,
        "roles": len(guild.roles),
        "emojis": len(guild.emojis),
        "boost_tier": getattr(guild, "premium_tier", None),
        "boosts": getattr(guild, "premium_subscription_count", None),
        "chunked": bool(guild.chunked),
    }
    return out


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


async def list_tables(db) -> list[str]:
    """Table names from sqlite_master, filtered through a strict identifier regex."""
    rows = await db.fetchall(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [r[0] for r in rows if _TABLE_NAME_RE.match(r[0])]


async def count_rows(db, table: str, allowed: list[str] | None = None) -> int:
    """SELECT COUNT(*) on a validated table. The name must match a strict
    identifier regex AND be present in `allowed` (default: sqlite_master's list),
    so an untrusted string can never reach the SQL text."""
    if not isinstance(table, str) or not _TABLE_NAME_RE.match(table):
        raise ValueError(f"invalid table name {table!r}")
    if allowed is None:
        allowed = await list_tables(db)
    if table not in allowed:
        raise ValueError(f"table {table!r} is not in the allowed table list")
    row = await db.fetchone(f'SELECT COUNT(*) FROM "{table}"')
    return int(row[0])


class _RoDb:
    """Independent read-only connection so heavy queries never block bot.db's connection."""

    def __init__(self, conn):
        self._c = conn

    async def fetchone(self, sql, params=()):
        async with self._c.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql, params=()):
        async with self._c.execute(sql, params) as cur:
            return await cur.fetchall()

    async def close(self):
        await self._c.close()


async def _open_ro(path: str):
    import pathlib

    import aiosqlite

    uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
    conn = await aiosqlite.connect(uri, uri=True)
    conn.row_factory = aiosqlite.Row
    return _RoDb(conn)


async def _db_heavy(db, path: str | None) -> dict[str, Any]:
    t0 = time.perf_counter()
    ro = None
    if path:
        try:
            ro = await _open_ro(path)
        except Exception:
            ro = None
    q = ro or db
    out: dict[str, Any] = {"reader": "read-only connection" if ro else "shared bot connection"}
    try:
        tables = await list_tables(q)
        counts: dict[str, Any] = {}
        for t in tables:
            try:
                counts[t] = await count_rows(q, t, tables)
            except Exception as exc:
                counts[t] = {"error": _safe_err(exc)}
        out["tables"] = counts
        out["table_count"] = len(tables)
        idx = await q.fetchone("SELECT COUNT(*) FROM sqlite_master WHERE type = 'index'")
        out["index_count"] = idx[0]
        for key in ("page_size", "page_count", "freelist_count", "journal_mode", "user_version", "wal_autocheckpoint"):
            try:
                r = await q.fetchone(f"PRAGMA {key}")
                out[key] = r[0]
            except Exception:
                out[key] = None
        try:
            r = await q.fetchone("PRAGMA quick_check(1)")
            out["quick_check"] = r[0]
        except Exception as exc:
            out["quick_check"] = _safe_err(exc)

        async def breakdown(sql: str, params: tuple = ()) -> Any:
            try:
                return {str(r[0]): r[1] for r in await q.fetchall(sql, params)}
            except Exception as exc:
                return {"error": _safe_err(exc)}

        out["audit_actions_24h"] = await breakdown(
            "SELECT action, COUNT(*) FROM audit_log WHERE created_at >= datetime('now', ?) "
            "GROUP BY action ORDER BY 2 DESC LIMIT 25", ("-1 day",))
        out["audit_actions_7d"] = await breakdown(
            "SELECT action, COUNT(*) FROM audit_log WHERE created_at >= datetime('now', ?) "
            "GROUP BY action ORDER BY 2 DESC LIMIT 25", ("-7 day",))
        out["mod_actions_by_type"] = await breakdown(
            "SELECT action_type, COUNT(*) FROM mod_actions GROUP BY action_type ORDER BY 2 DESC")
        out["mod_actions_7d"] = await breakdown(
            "SELECT action_type, COUNT(*) FROM mod_actions WHERE created_at >= datetime('now', ?) "
            "GROUP BY action_type ORDER BY 2 DESC", ("-7 day",))
        out["tickets_by_status"] = await breakdown(
            "SELECT status, COUNT(*) FROM tickets GROUP BY status ORDER BY 2 DESC")
        try:
            r = await q.fetchone("SELECT MAX(created_at) FROM audit_log")
            out["last_audit_log_at"] = r[0]
        except Exception as exc:
            out["last_audit_log_at"] = {"error": _safe_err(exc)}
    finally:
        if ro is not None:
            await ro.close()
    out["heavy_query_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return out


async def database_section(bot) -> dict[str, Any]:
    """The whole section (file sizes, migrations, row counts, quick_check, ...)
    is served from the TTL cache for TTL_DATABASE s; `as_of`/`ttl_seconds` and
    the older `heavy_as_of`/`heavy_ttl_seconds` names both carry the stamp."""
    db = getattr(bot, "db", None)
    if db is None:
        raise RuntimeError("bot.db is not available")
    val, as_of, ttl = await heavy("database", TTL_DATABASE, lambda: _database_uncached(db))
    return {**val, "as_of": as_of, "ttl_seconds": ttl, "heavy_as_of": as_of, "heavy_ttl_seconds": ttl}


async def _database_uncached(db) -> dict[str, Any]:
    path = getattr(db, "_db_path", None)
    out: dict[str, Any] = {"path": os.path.abspath(path) if path else None}
    if path:
        for label, suffix in (("size_bytes", ""), ("wal_bytes", "-wal"), ("shm_bytes", "-shm")):
            out[label] = _try(lambda s=suffix: os.path.getsize(path + s))
        out["modified_at"] = _try(lambda: _iso(os.path.getmtime(path)))
    rows = await db.fetchall("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
    out["migrations"] = {
        "latest_version": rows[-1][0] if rows else None,
        "latest_name": rows[-1][1] if rows else None,
        "applied_count": len(rows),
        "applied": [{"version": r[0], "name": r[1], "applied_at": r[2]} for r in rows],
    }
    out.update(await _db_heavy(db, path))
    return out


# --------------------------------------------------------------------------
# Services / background
# --------------------------------------------------------------------------

def loops_of(bot) -> list[dict[str, Any]]:
    """Every discord.ext.tasks.Loop declared on any loaded cog."""
    from discord.ext import tasks as _tasks

    rows: list[dict[str, Any]] = []
    for cog_name, cog in bot.cogs.items():
        for attr, val in list(vars(type(cog)).items()):
            if not isinstance(val, _tasks.Loop):
                continue
            try:
                loop = getattr(cog, attr)
                nxt = loop.next_iteration
                rows.append(
                    {
                        "cog": cog_name, "name": attr,
                        "running": bool(loop.is_running()), "failed": bool(loop.failed()),
                        "iterations": loop.current_loop,
                        "interval_seconds": (
                            (getattr(loop, "hours", None) or 0) * 3600
                            + (getattr(loop, "minutes", None) or 0) * 60
                            + (getattr(loop, "seconds", None) or 0)
                        ) or None,
                        "next_run": nxt.isoformat() if nxt else None,
                    }
                )
            except Exception as exc:
                rows.append({"cog": cog_name, "name": attr, "error": _safe_err(exc)})
    return rows


@serialized
def _pot_proc_stats(pid: int) -> dict[str, Any]:
    p = get_proc(pid)
    return {
        "uptime_seconds": round(time.time() - p.create_time(), 1),
        "cpu_percent": cpu_pct(p),
        "rss_bytes": p.memory_info().rss,
    }


def pot_server_section(bot, stack=None) -> dict[str, Any]:
    svc = getattr(bot, "pot_server_service", None)
    if svc is None:
        return {"available": False}
    snap = dict(svc.snapshot(log_lines=200))
    if snap.get("last_error"):
        snap["last_error"] = redact_text(str(snap["last_error"]))
    snap["log_tail"] = [redact_text(str(line))[:500] for line in (snap.get("log_tail") or [])]
    snap["available"] = True
    # Adopted external pot server: the service reports no pid/uptime, so look
    # them up from the stack (found by its listening port).
    for pid, meta in ((stack or {}).get("pids") or {}).items():
        if meta["role"] == "pot server":
            try:
                snap["pid"] = pid
                snap["adopted_external"] = meta["origin"] == "adopted external"
                snap.update(_pot_proc_stats(pid))
            except Exception as exc:
                snap["process_lookup_error"] = _safe_err(exc)
            break
    return snap


async def honeypot_section(bot, guild_id: int) -> dict[str, Any]:
    cog = bot.get_cog("Honeypot")
    svc = getattr(cog, "honeypot_service", None) if cog else None
    if svc is None:
        return {"available": False}
    cfg = await svc.get_config(guild_id)
    out: dict[str, Any] = {"available": True, "config": {k: v for k, v in cfg.items() if not isinstance(v, (dict, list))}}
    out.update(
        configured=cfg.get("configured"), enabled=cfg.get("enabled"), action=cfg.get("action"),
        channel_exists=cfg.get("channel_exists"),
    )
    try:
        out["total_catches"] = await count_rows(bot.db, "honeypot_events")
    except Exception:
        out["total_catches"] = None

    async def stats():
        s = await svc.get_stats(guild_id)
        return {k: v for k, v in s.items() if isinstance(v, (int, float, str, bool)) or v is None}

    try:
        st, as_of, _ = await heavy(f"honeypot-stats-{guild_id}", TTL_HONEYPOT_STATS, stats)
        out["stats"] = {**st, "as_of": as_of}
    except Exception as exc:
        out["stats"] = {"error": _safe_err(exc)}
    return out


def routes_of(app) -> list[dict[str, Any]]:
    found: dict[str, set[str]] = {}
    for r in getattr(app, "routes", []) or []:
        path = getattr(r, "path", None)
        if path:
            found.setdefault(path, set()).update(getattr(r, "methods", None) or [])
    try:
        # Newer FastAPI keeps included routers opaque in app.routes; the
        # (cached) OpenAPI schema is the stable public way to list them.
        for path, ops in app.openapi().get("paths", {}).items():
            found.setdefault(path, set()).update(m.upper() for m in ops)
    except Exception:
        pass
    return [{"path": p, "methods": sorted(m)} for p, m in sorted(found.items())]


async def services_section(bot, guild_id: int, app=None, stack=None) -> dict[str, Any]:
    routes: list[dict[str, Any]] = []
    if app is not None:
        routes, _as_of, _ttl = await heavy("internal-routes", TTL_ROUTES, lambda: routes_of(app))
    out: dict[str, Any] = {
        "internal_api": {
            "status": "up",
            "host": getattr(_config, "INTERNAL_API_HOST", None),
            "port": getattr(_config, "INTERNAL_API_PORT", None),
            "secret_configured": bool(getattr(_config, "INTERNAL_API_SECRET", "")),
            "routes": routes,
        }
    }
    for key, fn in (
        # worker thread: its psutil lookup must not run (or wait for the lock) on the event loop
        ("pot_server", lambda: asyncio.to_thread(pot_server_section, bot, stack)),
        ("background_loops", lambda: loops_of(bot)),
        ("honeypot", lambda: honeypot_section(bot, guild_id)),
    ):
        try:
            res = fn()
            out[key] = await res if inspect.isawaitable(res) else res
        except Exception as exc:
            out[key] = {"error": _safe_err(exc)}
    return out


# --------------------------------------------------------------------------
# Cookies (metadata only; values never stored)
# --------------------------------------------------------------------------

KEY_AUTH_COOKIES = frozenset(
    {"SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO", "__Secure-1PSID", "__Secure-3PSID",
     "__Secure-1PAPISID", "__Secure-3PAPISID", "__Secure-1PSIDTS", "__Secure-3PSIDTS"}
)


def mask_value(value: str) -> str:
    """'ab...(len 34)' at most; nothing but the length for short values."""
    n = len(value)
    return f"{value[:2]}…(len {n})" if n > 4 else f"…(len {n})"


def parse_cookie_file(text: str, now: float | None = None) -> dict[str, Any]:
    """Summarise a Netscape cookies.txt. Raw values never leave this function."""
    now = time.time() if now is None else now
    per_domain: dict[str, int] = {}
    cookies: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        http_only = parts[0].startswith("#HttpOnly_")
        domain = parts[0].removeprefix("#HttpOnly_")
        try:
            exp = int(float(parts[4]))
        except ValueError:
            exp = 0
        cookies.append(
            {
                "name": parts[5],
                "domain": domain,
                "path": parts[2],
                "secure": parts[3].upper() == "TRUE",
                "http_only": http_only,
                "session": exp == 0,
                "expires_at": _iso(exp) if exp else None,
                "expired": bool(exp and exp < now),
                "value_len": len(parts[6]),
                "value_preview": mask_value(parts[6]),
                "key_auth": parts[5] in KEY_AUTH_COOKIES,
                "_exp": exp,
            }
        )
        per_domain[domain.lstrip(".")] = per_domain.get(domain.lstrip("."), 0) + 1
    dated = [c for c in cookies if c["_exp"]]
    oldest = min(dated, key=lambda c: c["_exp"]) if dated else None
    newest = max(dated, key=lambda c: c["_exp"]) if dated else None
    key_cookies = [c for c in cookies if c["key_auth"]]
    for c in cookies:
        c.pop("_exp", None)
    return {
        "cookie_count": len(cookies),
        "domain_count": len(per_domain),
        "per_domain": dict(sorted(per_domain.items(), key=lambda kv: (-kv[1], kv[0]))[:60]),
        "session_cookies": sum(1 for c in cookies if c["session"]),
        "expired_count": sum(1 for c in cookies if c["expired"]),
        "earliest_expiry": oldest["expires_at"] if oldest else None,
        "earliest_expiry_cookie": f'{oldest["name"]} ({oldest["domain"]})' if oldest else None,
        "latest_expiry": newest["expires_at"] if newest else None,
        "latest_expiry_cookie": f'{newest["name"]} ({newest["domain"]})' if newest else None,
        "key_auth_cookies": [c for c in key_cookies],
        "key_auth_expired": any(c["expired"] for c in key_cookies),
        "key_auth_missing": bool(cookies) and not key_cookies,
        "cookies": cookies[:500],
        "truncated": len(cookies) > 500,
    }


def cookies_section() -> dict[str, Any]:
    raw = getattr(_config, "MUSIC_COOKIES_FILE", "") or ""
    if not raw:
        return {"present": False, "configured": False}
    path = raw if os.path.isabs(raw) else os.path.join(PROJECT_ROOT, raw)
    out: dict[str, Any] = {"configured": True, "path": os.path.abspath(path), "file_name": os.path.basename(raw)}
    try:
        st = os.stat(path)
    except OSError:
        out["present"] = False
        return out
    out.update(
        present=True, size_bytes=st.st_size, modified_at=_iso(st.st_mtime),
        age_hours=round((time.time() - st.st_mtime) / 3600, 1),
    )
    if st.st_size > 5 * 1024 * 1024:
        out["error"] = "file too large to parse"
        return out
    with open(path, "rb") as fh:
        blob = fh.read()
    out["sha256"] = hashlib.sha256(blob).hexdigest()
    out.update(parse_cookie_file(blob.decode("utf-8", errors="replace")))
    return out


async def cookies_section_cached() -> dict[str, Any]:
    """cookies_section() is re-parsed at most every TTL_COOKIES s (per-cookie table is large)."""
    val, as_of, ttl = await heavy("cookies", TTL_COOKIES, lambda: asyncio.to_thread(cookies_section))
    return {**val, "as_of": as_of, "ttl_seconds": ttl}


# --------------------------------------------------------------------------
# Config + versions, logs, activity
# --------------------------------------------------------------------------

_NODE_CACHE: dict[str, Any] = {"at": 0.0, "value": None}

KEY_PACKAGES = (
    "discord.py", "aiohttp", "aiosqlite", "yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider",
    "fastapi", "starlette", "pydantic", "uvicorn", "httpx", "SQLAlchemy", "alembic", "psutil",
    "PyNaCl", "croniter", "requests", "websockets", "python-dotenv", "itsdangerous", "slowapi",
)


def node_version() -> str | None:
    if _NODE_CACHE["at"] and time.monotonic() - _NODE_CACHE["at"] < 600:
        return _NODE_CACHE["value"]
    import subprocess

    node = getattr(_config, "MUSIC_POT_SERVER_NODE", "") or getattr(_config, "MUSIC_NODE_PATH", "")
    val = None
    if node:
        try:
            r = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            val = r.stdout.strip() or None
        except Exception:
            val = None
    _NODE_CACHE.update(at=time.monotonic(), value=val)
    return val


def versions_report() -> dict[str, Any]:
    def ver(dist: str) -> str | None:
        try:
            return metadata.version(dist)
        except Exception:
            return None

    out: dict[str, Any] = {"python": sys.version.split()[0], "node": node_version()}
    out.update({p: ver(p) for p in KEY_PACKAGES})
    return out


def config_section() -> dict[str, Any]:
    return {"env": config_report(), "versions": versions_report(), "project_root": PROJECT_ROOT}


async def config_section_cached() -> dict[str, Any]:
    """config_section() at most every TTL_CONFIG s (env and versions only change on restart/install)."""
    val, as_of, ttl = await heavy("config", TTL_CONFIG, lambda: asyncio.to_thread(config_section))
    return {**val, "as_of": as_of, "ttl_seconds": ttl}


_LEVEL_RE = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL|WRN|ERR|FTL)\b")
# cloudflared (tunnel.log) writes short level names
_LEVEL_ALIASES = {"WRN": "WARNING", "ERR": "ERROR", "FTL": "CRITICAL"}


# read_tail cache: path -> ((size, mtime_ns, max_lines, max_bytes), result).
# The Logs section is rebuilt every ~2 s while someone views the page; an
# unchanged file is not re-read or re-redacted. One entry per path, so it is
# bounded by the number of tailed files. A file that grows (size/mtime change)
# is re-read on the next push, so the tails still update live.
_TAIL_CACHE: dict[str, tuple[tuple[int, int, int, int], dict[str, Any]]] = {}
_TAIL_CACHE_LOCK = threading.Lock()


# Access-log scrubbing for tailed lines: same three patterns as supervisor.py's
# `scrub_lines` (kept in sync by hand; supervisor.py must stay stdlib-only and
# is not imported here). Public appeal tokens in paths, OAuth code/state (and
# other token-ish) query values, and client IPs in uvicorn access lines; only
# 127.0.0.1 stays readable, exactly like the supervisor.
_APPEAL_PATH_RE = re.compile(r"(?i)(/api/public/appeals/|/appeal/)[^/?#\s\"']+")
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:code|state|token|access_token|refresh_token)=)[^&#\s\"']+")
_ACCESS_IP_RE = re.compile(r"^(\s*INFO:\s+)(?!127\.0\.0\.1:)(\S+):(\d+) - \"")


def scrub_log_line(line: str) -> str:
    """`redact_text` plus the supervisor's access-log patterns (appeal tokens, OAuth query values, client IPs)."""
    s = redact_text(line)
    s = _APPEAL_PATH_RE.sub(lambda m: m.group(1) + REDACTED, s)
    s = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + REDACTED, s)
    return _ACCESS_IP_RE.sub(lambda m: f'{m.group(1)}<ip>:{m.group(3)} - "', s)


def clear_log_cache() -> None:
    """Drop every cached log tail (the next Logs section re-reads all files)."""
    with _TAIL_CACHE_LOCK:
        _TAIL_CACHE.clear()


def read_tail(path: str, max_lines: int, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    """Cached tail of a log file (see `_TAIL_CACHE`); `age_seconds` is always fresh."""
    st = os.stat(path)
    sig = (st.st_size, st.st_mtime_ns, max_lines, max_bytes)
    with _TAIL_CACHE_LOCK:
        hit = _TAIL_CACHE.get(path)
    if hit is not None and hit[0] == sig:
        res = dict(hit[1])
        # fresh containers so a caller mutating its copy can't touch the cache
        res["tail"] = list(res["tail"])
        res["level_counts"] = dict(res["level_counts"])
    else:
        res = _read_tail_uncached(path, max_lines, max_bytes)
        with _TAIL_CACHE_LOCK:
            _TAIL_CACHE[path] = (sig, dict(res))
    res["age_seconds"] = round(time.time() - st.st_mtime, 0)
    return res


def _read_tail_uncached(path: str, max_lines: int, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    st = os.stat(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, st.st_size - max_bytes))
        data = fh.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    if st.st_size > max_bytes and lines:
        lines = lines[1:]  # first line may be cut
    counts = collections.Counter(
        _LEVEL_ALIASES.get(m.group(1), m.group(1)) for ln in lines for m in [_LEVEL_RE.search(ln)] if m
    )
    tail = [scrub_log_line(ln)[:500] for ln in lines[-max_lines:]]  # cached already scrubbed
    return {
        "size_bytes": st.st_size, "modified_at": _iso(st.st_mtime),
        "age_seconds": round(time.time() - st.st_mtime, 0),
        "lines_scanned": len(lines),
        "level_counts": {k: counts.get(k, 0) for k in ("WARNING", "ERROR", "CRITICAL")},
        "tail": tail,
    }


def _log_files() -> list[tuple[str, str, str, int]]:
    """(key, label, absolute path, tail lines) of every tailed log, in display order."""
    sup = os.path.join(PROJECT_ROOT, "logs", "supervisor")
    pot = os.path.expanduser(str(getattr(_config, "MUSIC_POT_SERVER_LOG", "") or os.path.join("logs", "pot_server.log")))
    if not os.path.isabs(pot):
        pot = os.path.join(PROJECT_ROOT, pot)
    return [
        ("bot", "Bot (console under supervisor.py)", os.path.join(sup, "bot.log"), 150),
        ("dashboard", "Dashboard (uvicorn)", os.path.join(sup, "dashboard.log"), 60),
        ("tunnel", "Cloudflare tunnel", os.path.join(sup, "tunnel.log"), 60),
        ("supervisor", "Supervisor", os.path.join(sup, "supervisor.log"), 60),
        ("pot_server", "PO-token server", os.path.normpath(pot), 60),
        ("kicks", "Kicks (cogs/kick.py)", os.path.join(PROJECT_ROOT, "kicks.log"), 60),
        ("bot_startup", "Bot startup (main.py)", os.path.join(PROJECT_ROOT, "bot_startup.log"), 60),
    ]


def logs_section() -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    tailed: set[str] = set()
    for key, label, path, lines in _log_files():
        tailed.add(os.path.normcase(os.path.abspath(path)))
        row: dict[str, Any] = {"key": key, "label": label, "name": os.path.basename(path), "path": path}
        try:
            row.update(read_tail(path, lines))
            row["exists"] = True
        except OSError:
            row["exists"] = False
        files.append(row)
    # Anything else in logs/ and logs/supervisor/ (.pid, .lock, rotated files).
    logs_dir = os.path.join(PROJECT_ROOT, "logs")
    others = []
    for sub in ("", "supervisor"):
        d = os.path.join(logs_dir, sub) if sub else logs_dir
        if not os.path.isdir(d):
            continue
        for e in sorted(os.scandir(d), key=lambda e: e.name):
            if not e.is_file() or os.path.normcase(os.path.abspath(e.path)) in tailed:
                continue
            st = e.stat()
            others.append({"name": f"{sub}/{e.name}" if sub else e.name,
                           "size_bytes": st.st_size, "modified_at": _iso(st.st_mtime)})
    return {"files": files, "logs_dir": logs_dir, "other_files": others[:50],
            "note": "Services > Pot server also shows the last few pot_server.log lines next to its health."}


def activity_section(bot) -> dict[str, Any]:
    col = getattr(bot, "diagnostics_collector", None)
    if col is None:
        return {"available": False, "note": "cogs.diagnostics_collector is not loaded (restart the bot after deploying)"}
    return {"available": True, **col.snapshot()}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

async def _run_section(fn: Callable[[], Any]) -> Any:
    """Run a sync or async section; convert ANY failure to {"error": ...}."""
    try:
        res = fn()
        if inspect.isawaitable(res):
            res = await res
        return res
    except Exception as exc:
        return {"error": _safe_err(exc)}


def _threaded(fn: Callable[[], Any]) -> Callable[[], Any]:
    return lambda: asyncio.to_thread(fn)


async def collect_diagnostics(bot, guild_id: int, app=None) -> dict[str, Any]:
    db_path = getattr(getattr(bot, "db", None), "_db_path", None)
    # The stack (bot + children, pot server, dashboard, our tunnel) is built
    # ONCE; sections that list processes/sockets are scoped to it.
    stack = await _run_section(lambda: build_stack_cached(bot))
    sections: dict[str, Callable[[], Any]] = {
        "process": process_section,
        "children": lambda: children_section_cached(stack),
        "system": lambda: system_section_async(bot, db_path, stack),
        "discord": lambda: discord_section_cached(bot, guild_id),
        "database": lambda: database_section(bot),
        "services": lambda: services_section(bot, guild_id, app, stack),
        "cookies": cookies_section_cached,
        "config": config_section_cached,
        "logs": _threaded(logs_section),
        "activity": lambda: activity_section(bot),
    }
    # Measured FIRST and alone so the collectors below (thread work, GC census)
    # don't inflate the reading.
    lag = await measure_loop_lag()
    sections["process"] = lambda: process_section(lag)
    names = list(sections)
    results = await asyncio.gather(*(_run_section(sections[n]) for n in names))
    dump: dict[str, Any] = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "guild_id": str(guild_id),
        "psutil_available": psutil is not None,
    }
    dump.update(dict(zip(names, results)))
    return scrub_payload(dump)
