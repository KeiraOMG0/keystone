"""
Process supervisor for the kick-bot stack: bot (main.py), dashboard (uvicorn) and
the Cloudflare tunnel. Replaces start_all.bat when you want auto-restart + alerts.

  venv\\Scripts\\python.exe supervisor.py          (console)
  venv\\Scripts\\pythonw.exe supervisor.py         (no window; used by autostart)
  venv\\Scripts\\python.exe supervisor.py status               (ask the running one)
  venv\\Scripts\\python.exe supervisor.py restart bot "reason"  (bot|dashboard|tunnel|all)
  venv\\Scripts\\python.exe supervisor.py rotate-logs ["reason"] (archive the logs now)
  venv\\Scripts\\python.exe supervisor.py help                 (summary, health, tail, pid, ...)
  sv.bat                                                     (menu + short commands; `sv help`)

- Starts bot -> waits for the internal API -> dashboard -> tunnel.
- Restarts a child when it exits (exponential backoff, reset after it stays up).
- Restarts the bot/dashboard if their port stops answering (hung, not just dead).
- On every crash/restart posts to a Discord webhook with exit code, uptime and the
  last lines of that process's output, and pings you.
- Per-process logs: logs/supervisor/<name>.log + supervisor.log.
- Single instance: an OS lock on logs/supervisor/supervisor.lock; a second copy
  logs "another supervisor is already running" and exits without starting anything.
- Control API (loopback only, bearer auth) so the bot (/restart) and the dashboard
  (Diagnostics -> Services) can ask for a restart and read status:
    GET  /status
    POST /restart   {"target","requested_by","source","reason","delay_s"}
    GET  /logs?service=bot&lines=N   (in-memory output tail, secrets scrubbed)
    POST /rotate-logs {"requested_by","source","reason"}   (202 queued; result in /status)
  The HTTP thread never touches a child process: it queues a request that the
  main loop applies after a short delay (so the requester's reply gets out first).
- Weekly log rotation (LogRotator): Mondays 04:00 local time, or as soon as the
  supervisor notices it is overdue. bot/dashboard/tunnel/supervisor.log and the
  bot's bot_startup.log are moved to logs/archive/<name>.<YYYY-MM-DD>.log.gz and
  started fresh; archives 28+ days old are deleted. Never touches kicks.log
  (the bot's kick counter), pot_server.log, .pid/.lock files or databases.

Config (root .env, all optional): SUPERVISOR_WEBHOOK_URL, SUPERVISOR_PING
(user id, or "&roleid"; defaults to OWNER_ID), SUPERVISOR_TAIL_LINES (default 15),
SUPERVISOR_CONTROL_PORT (default 8767), SUPERVISOR_CONTROL_SECRET (defaults to
INTERNAL_API_SECRET; no secret at all = no control API), SUPERVISOR_RESTART_COOLDOWN
(seconds, default 30).
Stdlib only. Importing this module only reads .env and creates logs/supervisor/;
everything else happens in main().
"""

from __future__ import annotations

import gzip
import hmac
import json
import math
import os
import queue
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs" / "supervisor"
PY = str(ROOT / "venv" / "Scripts" / "python.exe")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

VERSION = "2.1"               # bumped when the control API changes shape (2.1: log rotation)
BACKOFF_START, BACKOFF_MAX = 3.0, 120.0
STABLE_AFTER = 120.0          # uptime that counts as "healthy" and resets backoff
HEALTH_EVERY = 20.0
HEALTH_GRACE = 90.0           # don't health-check a process during startup
HEALTH_FAILS = 3              # consecutive failures before a forced restart
CRASHLOOP_ALERT_EVERY = 5     # extra alert every N consecutive quick crashes
TAIL_KEEP = 200               # output lines kept in memory per child (alerts use TAIL_LINES of them)

# Control API
CONTROL_HOST = "127.0.0.1"    # loopback ONLY, never configurable
REQUESTED_RESTART_DELAY = 1.0  # gap between a requested stop and the new start
DEFAULT_REQUEST_DELAY = 1.5   # default not-before delay for a queued request
MAX_REQUEST_DELAY = 10.0
REQUEST_KILL_WINDOW = 60.0    # an exit this soon after a requested kill counts as "requested"
MAX_BODY = 4096
MAX_LOG_LINES = 200
MAX_CONTROL_CONNECTIONS = 16  # concurrent control-API connections; extra ones are closed at once
REQUEST_DEADLINE = 10.0       # whole-connection deadline (socket timeout alone only bounds each recv)
KILL_CONFIRM_AFTER = 15.0     # a requested kill that has not ended the process by now is retried once, then dropped
TARGETS = {
    "bot": ("bot",),
    "dashboard": ("dashboard",),
    "tunnel": ("tunnel",),
    "all": ("bot", "dashboard", "tunnel"),
}
SOURCES = ("dashboard", "discord", "bot", "cli")
INFO_COLOR = 0x3498DB
WARN_COLOR = 0xF1C40F
LOCK_OFFSET = 1024            # Windows: lock a byte past the pid text so others can still read the pid

# Weekly log rotation (LogRotator)
ROTATE_WEEKDAY, ROTATE_HOUR = 0, 4   # Monday 04:00 local time
ROTATE_RETENTION_DAYS = 28    # an archive whose date is this many days old (or more) is deleted
ROTATE_RENAME_ATTEMPTS = 10   # Windows sharing violations (a reader has the file open) are retried...
ROTATE_RENAME_DELAY = 0.25    # ...this far apart; the writer's lock is released between attempts
ROTATE_COOLDOWN = 60.0        # between accepted POST /rotate-logs requests
ROTATE_DEFER_WAIT = 5.0       # a service start waits at most this long for a running rotation
LOG_OPEN_ATTEMPTS = 20        # a pump retries (re)opening its log this often, 0.1 s apart
ROTATION_STATE_FILE = "log_rotation.json"   # in LOG_DIR
# Archive names: logs/archive/<key>.<YYYY-MM-DD>[.N].log.gz (".log" alone = not compressed yet).
# Only these keys are ever pruned; nothing else in the archive dir is touched.
_ARCHIVE_RE = re.compile(
    r"^(?P<key>bot|dashboard|tunnel|supervisor|bot_startup)\.(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:\.(?P<n>\d+))?\.log(?P<gz>\.gz)?$"
)


def read_env_file(path: Path | None = None) -> dict[str, str] | None:
    """Parse the root .env. None when it is missing or unreadable."""
    f = Path(path or ROOT / ".env")
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# The real process environment, before .env is merged in. Children are started
# from this plus a FRESH read of .env (child_env), so a requested restart picks
# up changed .env values; the supervisor's own settings below are read once.
_BASE_ENV = dict(os.environ)
_ENV_AT_IMPORT: dict[str, str] = {}


def load_env() -> None:
    global _ENV_AT_IMPORT
    _ENV_AT_IMPORT = read_env_file() or {}
    for k, v in _ENV_AT_IMPORT.items():
        os.environ.setdefault(k, v)


def child_env(path: Path | None = None) -> dict[str, str]:
    """Environment for a child: real env vars win over .env (like load_dotenv
    without override); .env is re-read now (falls back to the copy read at
    startup if the file cannot be read right now)."""
    fresh = read_env_file(path)
    if fresh is None:
        fresh = _ENV_AT_IMPORT
    env = dict(fresh)
    env.update(_BASE_ENV)
    # KICKBOT_SUPERVISED tells the bot its stdout is already captured in
    # logs/supervisor/bot.log, so it skips its duplicate bot_startup.log handler.
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", KICKBOT_SUPERVISED="1")
    return env


def _env_num(name: str, default: float, cast=float):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        return default


load_env()
WEBHOOK = os.getenv("SUPERVISOR_WEBHOOK_URL", "").strip()
PING = os.getenv("SUPERVISOR_PING", "").strip() or os.getenv("OWNER_ID", "").strip()
TAIL_LINES = _env_num("SUPERVISOR_TAIL_LINES", 15, int)
INTERNAL_PORT = _env_num("INTERNAL_API_PORT", 8766, int)
CONTROL_PORT = _env_num("SUPERVISOR_CONTROL_PORT", 8767, int)
CONTROL_SECRET = os.getenv("SUPERVISOR_CONTROL_SECRET", "").strip() or os.getenv("INTERNAL_API_SECRET", "").strip()
RESTART_COOLDOWN = max(0.0, _env_num("SUPERVISOR_RESTART_COOLDOWN", 30.0, float))
LOCK_FILE = LOG_DIR / "supervisor.lock"

LOG_DIR.mkdir(parents=True, exist_ok=True)
_sup_log_lock = threading.Lock()


def slog(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    with _sup_log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(LOG_DIR / "supervisor.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def internal_api_ok() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{INTERNAL_PORT}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def send_webhook(title: str, description: str, color: int, ping: bool) -> None:
    if not WEBHOOK:
        return
    content, mentions = "", {"parse": []}
    if ping and PING:
        if PING.startswith("&"):
            content, mentions = f"<@&{PING[1:]}>", {"roles": [PING[1:]]}
        else:
            content, mentions = f"<@{PING}>", {"users": [PING]}
    body = {
        "content": content,
        "allowed_mentions": mentions,
        "embeds": [{
            "title": title,
            "description": description[:3900],
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "kick-bot-supervisor"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # never let alerting kill the supervisor
        slog(f"webhook failed: {e}")


def alert(*args, **kw) -> None:
    threading.Thread(target=send_webhook, args=args, kwargs=kw, daemon=True).start()


# ---------------------------------------------------------------------------
# Secret scrubbing for /logs (port of server/routes/diagnostics.py's _scrub;
# this file must not import the bot's or the dashboard's packages)
# ---------------------------------------------------------------------------

REDACTED = "<redacted>"
_SECRET_NAME_RE = re.compile(
    r"(token|secret|passw(?:or)?d|passwd|pwd|api[_-]?key|apikey|private[_-]?key|credential|"
    r"authorization|bearer|webhook|cookie|(?:^|[_-])key$)",
    re.IGNORECASE,
)
_DISCORD_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_WEBHOOK_URL_RE = re.compile(r"(?i)(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_-]+")
# Capability tokens / PII in the dashboard's uvicorn access log: public appeal
# tokens (/api/public/appeals/<token>, SPA /appeal/<token>), OAuth code/state
# (and other token-ish) query values, and client IPs (anything but loopback).
_APPEAL_PATH_RE = re.compile(r"(?i)(/api/public/appeals/|/appeal/)[^/?#\s\"']+")
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:code|state|token|access_token|refresh_token)=)[^&#\s\"']+")
_ACCESS_IP_RE = re.compile(r"^(\s*INFO:\s+)(?!127\.0\.0\.1:)(\S+):(\d+) - \"")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _secret_values() -> list[str]:
    pairs = list(os.environ.items()) + list((read_env_file() or {}).items())  # .env may have changed since startup
    vals = {v for k, v in pairs if _SECRET_NAME_RE.search(k) and v and len(v) >= 6}
    if CONTROL_SECRET and len(CONTROL_SECRET) >= 6:
        vals.add(CONTROL_SECRET)
    return sorted(vals, key=len, reverse=True)


def scrub_lines(lines: list[str]) -> list[str]:
    secrets = _secret_values()
    out = []
    for s in lines:
        for v in secrets:
            s = s.replace(v, REDACTED)
        s = _JWT_RE.sub(REDACTED, s)
        s = _DISCORD_TOKEN_RE.sub(REDACTED, s)
        s = _WEBHOOK_URL_RE.sub(lambda m: m.group(1) + REDACTED, s)
        s = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
        s = _APPEAL_PATH_RE.sub(lambda m: m.group(1) + REDACTED, s)
        s = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + REDACTED, s)
        s = _ACCESS_IP_RE.sub(lambda m: f'{m.group(1)}<ip>:{m.group(3)} - "', s)
        out.append(s)
    return out


def clean_text(value: str, limit: int) -> str:
    """Single-line text for logs/alerts: runs of control characters (CR/LF,
    tabs, other C0, DEL) become one space, so a reason cannot forge log lines."""
    return _CONTROL_CHARS_RE.sub(" ", value).strip()[:limit]


def _describe_request(req: dict) -> str:
    text = f"restart requested by {req.get('requested_by') or 'unknown'} via {req.get('source') or 'unknown'}"
    return text + (f": {req['reason']}" if req.get("reason") else "")


class LogFile:
    """logs/supervisor/<name>.log, written by a service's pump thread(s).

    Every write and every rotation swap holds `lock`. The handle is opened
    lazily, so after the rotator closed it and renamed the file away, the next
    write creates a fresh file at the same path. While the swap holds the lock
    the pump waits, and the child's output simply waits in its stdout pipe
    (nothing is dropped, the child is not restarted). acquire()/release() count
    the pumps using it (an old process's pump can still be draining when the
    new start's pump begins); the handle is closed when the last one is done.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.lock = threading.Lock()
        self._f = None
        self._users = 0
        self._failing = False

    @property
    def path(self) -> Path:
        return LOG_DIR / self.filename  # looked up per call (tests redirect LOG_DIR)

    def acquire(self) -> None:
        with self.lock:
            self._users += 1

    def release(self) -> None:
        with self.lock:
            self._users = max(0, self._users - 1)
            if self._users == 0:
                self.close_locked()

    def close_locked(self) -> None:
        """Close the handle. Caller holds `lock` (the rotator passes this as on_lock)."""
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None

    def write(self, text: str) -> bool:
        err = None
        with self.lock:
            for attempt in range(LOG_OPEN_ATTEMPTS):
                if attempt:
                    time.sleep(0.1)
                try:
                    if self._f is None:
                        self._f = open(self.path, "a", encoding="utf-8", errors="replace")
                    self._f.write(text)
                    self._f.flush()
                    err = None
                    break
                except OSError as e:
                    err = e
                    self.close_locked()
            was_failing, self._failing = self._failing, err is not None
        # slog outside the lock (slog never takes a LogFile lock; keep it that way).
        if err is not None and not was_failing:
            slog(f"cannot write {self.filename} ({type(err).__name__}: {err}); output is kept in memory only until it works again")
        elif err is None and was_failing:
            slog(f"writing {self.filename} works again")
        return err is None


def move_aside(src: Path, dst: Path, lock=None, on_lock=None,
               attempts: int | None = None, delay: float | None = None) -> bool:
    """Rename src to dst for rotation. False when src does not exist.

    Each attempt holds `lock` (the writer's lock, or None for a file another
    process writes) and first calls on_lock() (e.g. close our own handle). A
    Windows sharing violation (PermissionError: someone has the file open
    without delete sharing) is retried `attempts` times, `delay` apart, with
    the lock RELEASED between attempts so a writer is never blocked for long.
    Raises the last PermissionError when every attempt failed (the file is
    then untouched), or any other OSError at once. os.rename never overwrites
    an existing dst on Windows; callers pick a fresh name anyway.
    """
    attempts = max(1, ROTATE_RENAME_ATTEMPTS if attempts is None else attempts)
    delay = ROTATE_RENAME_DELAY if delay is None else delay
    last: PermissionError | None = None
    for i in range(attempts):
        if i:
            time.sleep(delay)
        with (lock if lock is not None else nullcontext()):
            if on_lock is not None:
                on_lock()
            try:
                os.rename(src, dst)
                return True
            except FileNotFoundError:
                if not Path(src).exists():
                    return False
                raise
            except PermissionError as e:
                last = e
    assert last is not None
    raise last


class Service:
    def __init__(self, name, cmd, cwd, port=None, health=None, needs=None):
        self.name, self.cmd, self.cwd = name, cmd, cwd
        self.port, self.health, self.needs = port, health, needs
        self.proc: subprocess.Popen | None = None
        self.tail: deque[str] = deque(maxlen=TAIL_KEEP)
        self.started_at = 0.0
        self.backoff = BACKOFF_START
        self.quick_crashes = 0
        self.next_start = 0.0
        self.health_fails = 0
        self.next_health = 0.0
        self.skip_reason: str | None = None
        self.forced = False
        # control API bookkeeping (main thread only; the HTTP thread reads snapshots)
        self.starts = 0
        self.waiting = False                    # due to start but its `needs` gate is closed
        self.last_exit: dict | None = None      # {"reason", "at"}
        self.last_request: dict | None = None   # {"by", "source", "reason", "at"}
        self.requested: dict | None = None      # in-flight requested restart (set when we kill it)
        self.logfile = LogFile(f"{name}.log")  # shared by the pump(s) and the log rotator
        self.before_start = None                # main(): rotates logs deferred until this child is down

    def start(self) -> None:
        if self.port and port_open(self.port):
            if self.skip_reason is None:
                slog(f"{self.name}: port {self.port} already in use (already running elsewhere?) - not starting")
                alert(f"{self.name}: not started", f"Port {self.port} is already in use, so the supervisor "
                      "did not start it. Close the other copy (e.g. an old run_*.bat window) and restart the supervisor.",
                      0xF1C40F, False)
            self.skip_reason = "port in use"
            self.next_start = time.time() + 30
            return
        self.skip_reason = None
        if self.before_start is not None:
            # The old process is gone, so files it held open (bot_startup.log)
            # can be rotated now if the weekly run had to defer them.
            try:
                self.before_start()
            except Exception as e:  # never let rotation stop a start
                slog(f"{self.name}: deferred log rotation failed: {type(e).__name__}: {e}")
        env = child_env()
        logf = self.logfile
        logf.acquire()
        logf.write(f"\n===== start {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        try:
            self.proc = subprocess.Popen(
                self.cmd, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW | NEW_GROUP,
            )
        except OSError as e:
            logf.release()
            slog(f"{self.name}: failed to launch: {e}")
            self.proc = None
            self._exited(-1, f"launch failed: {e}")
            return
        self.started_at = time.time()
        self.starts += 1
        self.waiting = False
        self.health_fails = 0
        self.next_health = self.started_at + HEALTH_GRACE
        self.forced = False
        slog(f"{self.name}: started pid {self.proc.pid}")
        threading.Thread(target=self._pump, args=(self.proc, logf), daemon=True).start()

    def _pump(self, proc, logf: LogFile) -> None:
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                self.tail.append(line)
                logf.write(line + "\n")  # waits (child output stays in the pipe) while a rotation swaps the file
        finally:
            logf.release()

    def kill_tree(self) -> bool:
        """Kill the child and its descendants. Returns False (and logs why) when
        the kill command itself failed; callers still watch proc.poll()."""
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":
                try:
                    r = subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=30)
                except (OSError, subprocess.SubprocessError) as e:
                    slog(f"{self.name}: taskkill failed to run: {type(e).__name__}: {e}")
                    return False
                if r.returncode != 0 and self.proc.poll() is None:
                    err = (r.stderr or r.stdout or b"").decode("utf-8", errors="replace")
                    slog(f"{self.name}: taskkill exit {r.returncode}: {clean_text(err, 300) or '(no output)'}")
                    return False
            else:  # pragma: no cover - the live deployment is Windows
                self.proc.kill()
        return True

    def poll(self, now: float) -> None:
        if self.proc is None:
            if now >= self.next_start:
                if self.needs and not self.needs():
                    self.waiting = True
                    return
                self.waiting = False
                self.start()
            return
        code = self.proc.poll()
        if code is not None:
            self._exited(code, None)
            return
        if self.requested is not None and now - self.requested.get("kill_at", now) >= KILL_CONFIRM_AFTER:
            if not self.requested.get("retried"):
                slog(f"{self.name}: requested stop did not take effect after {KILL_CONFIRM_AFTER:.0f}s "
                     f"(pid {self.proc.pid} still running) - retrying the kill once")
                self.requested["retried"] = True
                self.requested["kill_at"] = now
                self.kill_tree()
            else:
                slog(f"{self.name}: requested stop still did not take effect (pid {self.proc.pid}) - giving up; "
                     "the request is dropped and new requests are allowed")
                alert(f"{self.name}: requested restart failed",
                      f"**{_describe_request(self.requested)}**\nThe process (pid {self.proc.pid}) did not stop "
                      "after two kill attempts and is still running. Check the host.", 0xF1C40F, False)
                self.requested = None
        if self.health and now >= self.next_health:
            self.next_health = now + HEALTH_EVERY
            if self.health():
                self.health_fails = 0
            else:
                self.health_fails += 1
                slog(f"{self.name}: health check failed ({self.health_fails}/{HEALTH_FAILS})")
                if self.health_fails >= HEALTH_FAILS:
                    self.forced = True
                    self.kill_tree()  # next poll sees the exit and restarts it

    def request_restart(self, req: dict, now: float) -> None:
        """Apply a queued restart request. MAIN THREAD ONLY (called by Controller.drain)."""
        self.last_request = {
            "by": req.get("requested_by"), "source": req.get("source"),
            "reason": req.get("reason"), "at": iso(now),
        }
        text = _describe_request(req)
        if self.proc is not None:
            if self.proc.poll() is None:
                # Only a kill WE make counts as a requested restart. If the
                # process already exited (crash, or a health-check kill) and the
                # main loop has not seen it yet, the normal crash path in
                # _exited() handles it: red alert, output tail, backoff.
                self.requested = dict(req, at=now, kill_at=now)
                slog(f"{self.name}: {text} - stopping pid {self.proc.pid}")
                self.forced = False
                self.kill_tree()  # the next poll sees the exit; _exited restarts it quickly
            else:
                slog(f"{self.name}: {text}, but pid {self.proc.pid} had already exited - handled as a normal exit")
            return
        # Not running: in backoff after a crash, waiting on its dependency, or
        # skipped because its port is taken. Start it as soon as allowed instead.
        state = self.skip_reason or ("waiting for its dependency" if self.waiting else "in restart backoff")
        self.backoff, self.quick_crashes = BACKOFF_START, 0
        self.next_start = min(self.next_start, now)
        when = ("it will start as soon as the bot's internal API answers" if self.waiting
                else "starting it as soon as possible")
        slog(f"{self.name}: {text} while not running ({state}) - {when}")
        alert(f"{self.name}: start requested", f"**{text}**\nIt was not running ({state}); {when}.",
              INFO_COLOR, False)

    def _exited(self, code: int, note: str | None) -> None:
        uptime = time.time() - self.started_at if self.started_at else 0
        time.sleep(0.3)  # let the pump drain the last lines
        tail = list(self.tail)[-TAIL_LINES:]
        self.proc = None
        now = time.time()
        req, self.requested = self.requested, None
        if req is not None and now - req.get("at", 0) <= REQUEST_KILL_WINDOW:
            # Requested restart: not a crash. No backoff growth, no quick-crash
            # count, info alert without a ping.
            if uptime >= STABLE_AFTER:
                self.backoff, self.quick_crashes = BACKOFF_START, 0
            self.next_start = now + REQUESTED_RESTART_DELAY
            reason = _describe_request(req)
            self.last_exit = {"reason": reason, "at": iso(now)}
            slog(f"{self.name}: {reason} - stopped after {uptime:.0f}s, starting again in {REQUESTED_RESTART_DELAY:.0f}s")
            alert(f"{self.name} restarting (requested)",
                  f"**{reason}**\nStopped after {uptime:.0f}s up; starting again now.", INFO_COLOR, False)
            return
        if uptime >= STABLE_AFTER:
            self.backoff, self.quick_crashes = BACKOFF_START, 0
        else:
            self.quick_crashes += 1
        delay = self.backoff
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)
        self.next_start = now + delay
        reason = note or ("hung (health check failed), killed by supervisor" if self.forced else f"exit code {code}")
        self.last_exit = {"reason": reason, "at": iso(now)}
        slog(f"{self.name}: DOWN ({reason}) after {uptime:.0f}s - restarting in {delay:.0f}s")
        loop = self.quick_crashes and self.quick_crashes % CRASHLOOP_ALERT_EVERY == 0
        desc = (f"**{reason}** after {uptime:.0f}s up.\nRestarting in {delay:.0f}s"
                f"{f' - crash loop: {self.quick_crashes} quick crashes in a row' if loop else ''}.\n"
                f"```\n{chr(10).join(tail)[-3000:] or '(no output captured)'}\n```")
        alert(f"{self.name} went down", desc, 0xE74C3C, True)

    def stop(self) -> None:
        self.kill_tree()


# ---------------------------------------------------------------------------
# Weekly log rotation
# ---------------------------------------------------------------------------

def last_rotation_slot(now: datetime) -> datetime:
    """The most recent Monday 04:00 (local, naive) at or before `now`."""
    slot = now.replace(hour=ROTATE_HOUR, minute=0, second=0, microsecond=0) - timedelta(
        days=(now.weekday() - ROTATE_WEEKDAY) % 7)
    return slot if slot <= now else slot - timedelta(days=7)


def next_rotation_slot(now: datetime) -> datetime:
    """The first Monday 04:00 strictly after `now`."""
    return last_rotation_slot(now) + timedelta(days=7)


def _local_iso(dt: datetime | None) -> str | None:
    return dt.astimezone().isoformat(timespec="seconds") if dt else None


class LogRotator:
    """Weekly rotation of the logs this stack writes (Mondays 04:00 local).

    Files (see _targets): logs/supervisor/{bot,dashboard,tunnel}.log (the
    supervisor's own pumps write them; swapped under the service's LogFile
    lock), logs/supervisor/supervisor.log (slog, under _sup_log_lock) and the
    bot's bot_startup.log at the repo root. The bot keeps that last one open
    for its whole life (logging.FileHandler in core/logging_setup.py) and a
    Windows file held open without delete sharing cannot be renamed, so while
    the bot runs the rename fails: the file is then "deferred" (persisted) and
    rotated by Service.before_start the next time the bot is started, when no
    process holds it. Copy-then-truncate is NOT used: lines the bot writes
    between the copy and the truncate would be lost.

    NEVER touched: kicks.log (line 1 is the bot's kick counter), pot_server.log
    (the bot's Node child holds it as stdout; the bot already rotates it to
    .1 by size on each launch), .pid/.lock files, the databases, cookies.txt.

    A swap is: rename the live file to logs/archive/<key>.<date>[.N].log
    (atomic, same volume; the writer starts a fresh file on its next write),
    then gzip it to <...>.log.gz.tmp, rename that to .log.gz and only then
    delete the uncompressed copy. An uncompressed leftover (supervisor died
    mid-gzip, or gzip failed) is compressed on the next run. Then archives
    whose date is ROTATE_RETENTION_DAYS (28) or more days old are deleted.

    Scheduling: tick() (main loop, every second) runs it in a worker thread
    when the most recent Monday 04:00 is later than the last slot handled,
    which is persisted in logs/supervisor/log_rotation.json, so a slot missed
    while the PC was off runs once at the next tick and a restart never runs
    the same slot twice. With no state file (first start) the current slot is
    recorded as handled WITHOUT rotating; the first automatic run is the next
    Monday (rotate-logs runs one at once). Manual runs (POST /rotate-logs,
    `supervisor.py rotate-logs`) do not change the schedule.
    """

    # file key -> service name whose (re)start may rotate it after a deferral
    DEFER_TO = {"bot_startup": "bot"}

    def __init__(self, services=(), *, root: Path | None = None, log_dir: Path | None = None,
                 archive_dir: Path | None = None, state_path: Path | None = None,
                 retention_days: int = ROTATE_RETENTION_DAYS, clock=None) -> None:
        self.services = {s.name: s for s in services}
        self.root = Path(root or ROOT)
        self.log_dir = Path(log_dir or LOG_DIR)
        self.archive_dir = Path(archive_dir or self.root / "logs" / "archive")
        self.state_path = Path(state_path or self.log_dir / ROTATION_STATE_FILE)
        self.retention_days = retention_days
        self.clock = clock or datetime.now
        self._lock = threading.Lock()       # state, running flag, claimed archive names
        self._run_lock = threading.Lock()   # one swap phase at a time (run vs. deferred-at-start)
        self._claimed: set[str] = set()     # uncompressed archives a thread is compressing
        self.running = False
        self.state: dict = self._load_state()

    # ---- state ---------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            slog(f"log rotation: cannot read {self.state_path.name} ({type(e).__name__}: {e}); starting without it")
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self) -> None:
        with self._lock:
            data = json.dumps(dict(self.state, version=1), indent=2)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as e:
            slog(f"log rotation: cannot save {self.state_path.name} ({type(e).__name__}: {e})")

    def _last_slot(self) -> datetime | None:
        raw = self.state.get("last_slot")
        try:
            return datetime.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            return None

    def is_running(self) -> bool:
        with self._lock:
            return self.running

    # ---- schedule (main thread) -----------------------------------------------
    def start(self) -> None:
        """Called once from main(): records the baseline on a first start, logs
        the schedule and compresses leftovers of an interrupted run."""
        now = self.clock()
        if self._last_slot() is None:
            with self._lock:
                self.state["last_slot"] = last_rotation_slot(now).isoformat(timespec="seconds")
            self._save_state()
            slog(f"log rotation: no previous run recorded; first automatic rotation "
                 f"{next_rotation_slot(now):%a %Y-%m-%d %H:%M} (`supervisor.py rotate-logs` rotates now)")
        else:
            nxt = self._next_run(now)
            slog(f"log rotation: weekly, Mondays {ROTATE_HOUR:02d}:00; next run "
                 f"{'now (overdue)' if nxt <= now else f'{nxt:%a %Y-%m-%d %H:%M}'}; "
                 f"archives in {self._rel(self.archive_dir)}, kept {self.retention_days} days")
        if self._leftovers():
            threading.Thread(target=self._recover_safe, name="log-rotation-recover", daemon=True).start()

    def _next_run(self, now: datetime) -> datetime:
        """When the scheduled run happens: a past slot (<= now) means overdue."""
        last = self._last_slot()
        due = last_rotation_slot(now)
        if last is None:
            return next_rotation_slot(now)  # first start: baseline, then next Monday
        return due if due > last else due + timedelta(days=7)

    def tick(self, now: datetime | None = None) -> bool:
        """Start the scheduled run when a Monday 04:00 slot is due. True when started."""
        try:
            now = now or self.clock()
            slot = last_rotation_slot(now)
            with self._lock:
                if self.running:
                    return False
                last = self._last_slot()
                if last is None:  # start() not called (tests) or state lost: baseline, no rotation
                    self.state["last_slot"] = slot.isoformat(timespec="seconds")
                    baseline = True
                elif slot > last:
                    baseline = False
                else:
                    return False
            if baseline:
                self._save_state()
                return False
            return self.run_async("schedule", slot=slot)
        except Exception as e:  # never let the scheduler kill the main loop
            slog(f"log rotation: scheduler error: {type(e).__name__}: {e}")
            return False

    def run_async(self, trigger: str, *, slot: datetime | None = None, requested_by: str | None = None,
                  source: str | None = None, reason: str | None = None) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run, args=(trigger, slot, requested_by, source, reason),
                         name="log-rotation", daemon=True).start()
        return True

    def run(self, trigger: str = "manual", *, slot: datetime | None = None, requested_by: str | None = None,
            source: str | None = None, reason: str | None = None) -> dict | None:
        """Synchronous run (tests, and the body of run_async). None if one is already running."""
        with self._lock:
            if self.running:
                return None
            self.running = True
        return self._run(trigger, slot, requested_by, source, reason)

    # ---- the run (worker thread) ------------------------------------------------
    def _targets(self) -> list[dict]:
        out = []
        for name in ("bot", "dashboard", "tunnel"):
            svc = self.services.get(name)
            lf = getattr(svc, "logfile", None)
            if lf is not None:
                out.append({"key": name, "path": lf.path, "lock": lf.lock, "on_lock": lf.close_locked})
            else:
                out.append({"key": name, "path": self.log_dir / f"{name}.log"})
        out.append({"key": "supervisor", "path": self.log_dir / "supervisor.log", "lock": _sup_log_lock})
        out.append({"key": "bot_startup", "path": self.root / "bot_startup.log"})
        return out

    def _rel(self, p: Path) -> str:
        try:
            return Path(p).relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    def _run(self, trigger, slot, requested_by, source, reason) -> dict:
        started = self.clock()
        result: dict = {"trigger": trigger, "requested_by": requested_by, "source": source, "reason": reason,
                        "at": _local_iso(started), "finished_at": None, "ok": False, "files": [], "deleted": []}
        try:
            day = started.date().isoformat()
            who = f" requested by {requested_by} via {source}{f': {reason}' if reason else ''}" if requested_by else ""
            slog(f"log rotation: starting ({trigger}{who})")
            moved: list[tuple[dict, Path]] = []
            with self._run_lock:
                self.archive_dir.mkdir(parents=True, exist_ok=True)
                for t in self._targets():
                    res, dst = self._swap(t, day)
                    result["files"].append(res)
                    if dst is not None:
                        moved.append((res, dst))
                with self._lock:
                    if slot is not None:
                        self.state["last_slot"] = slot.isoformat(timespec="seconds")
                    self._set_deferred(result["files"])
                self._save_state()
            for res, dst in moved:
                self._compress_result(res, dst)
            self._recover()
            result["deleted"] = self.prune(started.date())
        except Exception as e:  # never let rotation crash the supervisor
            slog(f"log rotation: FAILED: {type(e).__name__}: {e}")
            result["error"] = f"{type(e).__name__}: {e}"[:300]
        finally:
            problems = [f for f in result["files"] if f["status"] in ("failed", "compress_failed")]
            result["ok"] = not problems and "error" not in result
            result["finished_at"] = _local_iso(self.clock())
            with self._lock:
                if slot is not None:  # also after an error: never retry the same slot every tick
                    self.state["last_slot"] = slot.isoformat(timespec="seconds")
                self.state["last_run"] = result
                self.running = False
            self._save_state()
        summary = ", ".join(f"{f['file']} {f['status']}" for f in result["files"])
        n_deleted = len(result["deleted"])
        slog(f"log rotation: done ({'ok' if result['ok'] else 'with problems'}): {summary}"
             f"{f'; deleted {n_deleted} old archive(s)' if n_deleted else ''}")
        if not result["ok"]:
            lines = [f"**{f['file']}**: {f['status']} ({f.get('error') or ''})" for f in problems] or [result.get("error", "")]
            alert("Log rotation problem", "\n".join(lines) + "\nNo log lines were lost: a file that could not be "
                  "moved was left in place, and an archive that could not be gzipped is kept uncompressed in "
                  f"{self._rel(self.archive_dir)}. Details in supervisor.log.", WARN_COLOR, False)
        return result

    def _set_deferred(self, files: list[dict]) -> None:
        """Update the persisted deferred list from this run's results. Caller holds _lock."""
        deferred = set(self.state.get("deferred") or [])
        for f in files:
            if f["status"] == "deferred":
                deferred.add(f["key"])
            elif f["status"] in ("rotated", "compress_failed", "empty", "missing"):
                deferred.discard(f["key"])
        self.state["deferred"] = sorted(deferred)

    def _archive_name(self, key: str, day: str) -> Path:
        for n in range(1000):
            p = self.archive_dir / (f"{key}.{day}.log" if n == 0 else f"{key}.{day}.{n}.log")
            if not any(q.exists() for q in (p, p.with_name(p.name + ".gz"), p.with_name(p.name + ".gz.tmp"))):
                return p
        raise OSError(f"no free archive name for {key}.{day}")

    def _swap(self, t: dict, day: str) -> tuple[dict, Path | None]:
        """Move one live file aside (under its writer's lock). Caller holds _run_lock."""
        path: Path = t["path"]
        res: dict = {"key": t["key"], "file": path.name, "status": "missing", "bytes": 0, "archive": None, "error": None}
        try:
            if path.stat().st_size == 0:
                res["status"] = "empty"
                return res, None
        except FileNotFoundError:
            return res, None
        except OSError as e:
            res.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
            slog(f"log rotation: FAILED to read {self._rel(path)}: {res['error']}")
            return res, None
        try:
            dst = self._archive_name(t["key"], day)
            if not move_aside(path, dst, t.get("lock"), t.get("on_lock")):
                return res, None
        except PermissionError as e:
            err = f"{type(e).__name__}: {e}"[:300]
            if t["key"] in self.DEFER_TO:
                res.update(status="deferred", error="held open by another process")
                slog(f"log rotation: {self._rel(path)} is held open by another process (the running "
                     f"{self.DEFER_TO[t['key']]} keeps it open via a logging.FileHandler); deferred until the "
                     f"{self.DEFER_TO[t['key']]} is next started by the supervisor. Left untouched, nothing lost.")
            else:
                res.update(status="failed", error=err)
                slog(f"log rotation: FAILED to move {self._rel(path)} (still in use after "
                     f"{ROTATE_RENAME_ATTEMPTS} attempts): {err}; left in place")
            return res, None
        except OSError as e:
            res.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
            slog(f"log rotation: FAILED to move {self._rel(path)}: {res['error']}; left in place")
            return res, None
        with self._lock:
            self._claimed.add(dst.name)
        try:
            res["bytes"] = dst.stat().st_size
        except OSError:
            pass
        res.update(status="rotated", archive=dst.name)
        return res, dst

    def _compress(self, pending: Path) -> Path:
        """gzip pending -> pending.gz (via .gz.tmp), then delete pending. Raises on failure."""
        gz = pending.with_name(pending.name + ".gz")
        tmp = gz.with_name(gz.name + ".tmp")
        with open(pending, "rb") as src, open(tmp, "wb") as raw:
            with gzip.GzipFile(filename=pending.name, mode="wb", fileobj=raw, compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, gz)
        pending.unlink()
        return gz

    def _compress_result(self, res: dict, pending: Path) -> None:
        try:
            gz = self._compress(pending)
            res["archive"] = gz.name
            res["gz_bytes"] = gz.stat().st_size
            slog(f"log rotation: {res['file']} -> {self._rel(gz)} ({res['bytes']} bytes, {res['gz_bytes']} gzipped)")
        except Exception as e:
            res.update(status="compress_failed", error=f"{type(e).__name__}: {e}"[:300])
            slog(f"log rotation: {res['file']} moved to {self._rel(pending)} ({res['bytes']} bytes) but gzip "
                 f"FAILED: {res['error']}; kept uncompressed, retried on the next run")
        finally:
            with self._lock:
                self._claimed.discard(pending.name)

    def _leftovers(self) -> list[Path]:
        try:
            entries = list(os.scandir(self.archive_dir))
        except OSError:
            return []
        out = []
        for e in entries:
            m = _ARCHIVE_RE.match(e.name)
            if m and not m["gz"] and e.is_file():
                out.append(Path(e.path))
        return sorted(out)

    def _recover(self) -> None:
        """Compress uncompressed archives left by an interrupted or failed earlier run."""
        for p in self._leftovers():
            with self._lock:
                if p.name in self._claimed:
                    continue
                self._claimed.add(p.name)
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            self._compress_result({"file": p.name, "bytes": size}, p)

    def _recover_safe(self) -> None:
        try:
            self._recover()
        except Exception as e:
            slog(f"log rotation: recovering leftovers failed: {type(e).__name__}: {e}")

    def prune(self, today: date) -> list[str]:
        """Delete <key>.<date>[.N].log.gz archives whose date is retention_days or more before today."""
        removed = []
        try:
            entries = list(os.scandir(self.archive_dir))
        except OSError:
            return removed
        for e in entries:
            m = _ARCHIVE_RE.match(e.name)
            if not m or not m["gz"] or not e.is_file():
                continue
            try:
                age = (today - date.fromisoformat(m["date"])).days
            except ValueError:
                continue
            if age >= self.retention_days:
                try:
                    os.unlink(e.path)
                    removed.append(e.name)
                except OSError as err:
                    slog(f"log rotation: could not delete old archive {e.name}: {err}")
        if removed:
            slog(f"log rotation: deleted {len(removed)} archive(s) {self.retention_days}+ days old: {', '.join(sorted(removed))}")
        return sorted(removed)

    # ---- deferred files (main thread, from Service.start) -----------------------
    def before_start(self, service_name: str) -> list[dict]:
        """Rotate files deferred because `service_name` held them open; called by
        Service.start right before launching it (the old process is gone)."""
        with self._lock:
            keys = [k for k in (self.state.get("deferred") or []) if self.DEFER_TO.get(k) == service_name]
        if not keys:
            return []
        if not self._run_lock.acquire(timeout=ROTATE_DEFER_WAIT):
            slog(f"log rotation: a rotation is running; {', '.join(keys)} stays deferred until the next {service_name} start")
            return []
        try:
            day = self.clock().date().isoformat()
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            results, moved = [], []
            for t in self._targets():
                if t["key"] in keys:
                    res, dst = self._swap(t, day)
                    results.append(res)
                    if dst is not None:
                        moved.append((res, dst))
            with self._lock:
                self._set_deferred(results)
            self._save_state()
        finally:
            self._run_lock.release()
        if moved:
            threading.Thread(target=self._compress_moved, args=(moved,), name="log-rotation-gzip", daemon=True).start()
        return results

    def _compress_moved(self, moved: list[tuple[dict, Path]]) -> None:
        for res, dst in moved:
            self._compress_result(res, dst)
            if res["status"] != "rotated":
                alert("Log rotation problem", f"**{res['file']}**: {res['status']} ({res.get('error') or ''})\n"
                      "No log lines were lost. Details in supervisor.log.", WARN_COLOR, False)

    # ---- status (HTTP thread) ----------------------------------------------------
    def status(self) -> dict:
        now = self.clock()
        with self._lock:
            nxt = self._next_run(now)
            last = self.state.get("last_run")
            return {
                "schedule": f"weekly, Monday {ROTATE_HOUR:02d}:00 local time (catch-up when overdue)",
                "retention_days": self.retention_days,
                "archive_dir": self._rel(self.archive_dir),
                "running": self.running,
                "last_run": json.loads(json.dumps(last)) if isinstance(last, dict) else None,
                "next_run_at": _local_iso(now if nxt <= now else nxt),  # overdue: at the next tick
                "overdue": nxt <= now,
                "deferred": list(self.state.get("deferred") or []),
            }


# ---------------------------------------------------------------------------
# Control API: Controller (thread-safe state shared with the HTTP thread)
# ---------------------------------------------------------------------------

class Controller:
    """Bridge between the HTTP thread and the main loop.

    Main thread: publish() snapshots every Service; drain() applies queued
    requests whose delay has passed (the only place a request reaches a Service).
    HTTP thread: status()/submit()/logs() read the snapshot under the lock and
    enqueue requests. It never calls kill_tree() or mutates a Service.
    """

    def __init__(self, services, *, cooldown: float = RESTART_COOLDOWN, secret: str = "",
                 rotator: LogRotator | None = None) -> None:
        self.services = {s.name: s for s in services}
        self.rotator = rotator
        self._rotate_pending = False               # POST /rotate-logs queued, not yet applied
        self._rotate_accepted_at: float | None = None
        self.order = [s.name for s in services]
        self.cooldown = cooldown
        self.secret = secret
        self.started_at = time.time()
        self.requests: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._snapshot: dict[str, dict] = {}
        self._tails = {s.name: s.tail for s in services}
        self._accepted_at: dict[str, float] = {}   # cooldown bookkeeping
        self._pending: set[str] = set()            # queued, not yet applied
        self._due: list[dict] = []                 # main thread only
        self.publish()

    # ---- main thread -------------------------------------------------------
    def publish(self) -> None:
        snap = {}
        for name in self.order:
            s = self.services[name]
            proc = s.proc
            running = proc is not None
            snap[name] = {
                "name": name,
                "running": running,
                "pid": proc.pid if running else None,
                "started_at": s.started_at if running else None,
                "restarts": max(0, s.starts - 1),
                "last_exit": s.last_exit,
                "last_request": s.last_request,
                "health_fails": s.health_fails,
                "waiting_on_dependency": bool(not running and s.waiting),
                "skip_reason": None if running else s.skip_reason,
                "next_start_at": None if running else s.next_start,
                "restart_requested": s.requested is not None,
            }
        with self._lock:
            self._snapshot = snap

    def drain(self, now: float | None = None) -> list[str]:
        """Apply every queued request whose not-before time has passed. Returns
        the names it applied. MAIN THREAD ONLY."""
        now = time.time() if now is None else now
        while True:
            try:
                self._due.append(self.requests.get_nowait())
            except queue.Empty:
                break
        applied, keep = [], []
        for req in self._due:
            if req["not_before"] > now:
                keep.append(req)
                continue
            if req.get("kind") == "rotate":
                try:
                    if self.rotator is not None and self.rotator.run_async(
                            "manual", requested_by=req["requested_by"], source=req["source"], reason=req["reason"]):
                        applied.append("log-rotation")
                    else:
                        slog("control: log rotation request dropped: a rotation is already running")
                except Exception as e:  # never let a request kill the loop
                    slog(f"control: applying log rotation request failed: {type(e).__name__}: {e}")
                with self._lock:
                    self._rotate_pending = False
                continue
            svc = self.services.get(req["name"])
            if svc is not None:
                try:
                    svc.request_restart(req, now)
                    applied.append(req["name"])
                except Exception as e:  # never let a request kill the loop
                    slog(f"{req['name']}: applying restart request failed: {type(e).__name__}: {e}")
            with self._lock:
                self._pending.discard(req["name"])
        self._due = keep
        if applied:
            self.publish()
        return applied

    # ---- HTTP thread -------------------------------------------------------
    def status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            snap = [dict(self._snapshot[n]) for n in self.order if n in self._snapshot]
            accepted = dict(self._accepted_at)
            pending = set(self._pending)
        out = []
        for s in snap:
            started = s.pop("started_at")
            nxt = s.pop("next_start_at")
            last = accepted.get(s["name"])
            s["uptime_s"] = round(max(0.0, now - started), 1) if started else None
            s["cooldown_remaining_s"] = round(max(0.0, self.cooldown - (now - last)), 1) if last else 0.0
            s["next_start_in_s"] = round(max(0.0, nxt - now), 1) if nxt and not s["running"] else None
            requested = s.pop("restart_requested")
            s["restart_pending"] = s["name"] in pending or requested
            out.append(s)
        return {
            "supervisor": {
                "pid": os.getpid(),
                "started_at": iso(self.started_at),
                "version": VERSION,
                "control": True,
                "restart_cooldown_s": self.cooldown,
            },
            "services": out,
            "log_rotation": self._rotation_status(),
        }

    def _rotation_status(self) -> dict | None:
        if self.rotator is None:
            return None
        with self._lock:
            pending = self._rotate_pending
        return dict(self.rotator.status(), pending=pending)

    @staticmethod
    def _requester(body: dict) -> tuple[tuple[int, dict] | None, str, str, str | None]:
        """Validate requested_by/source/reason (shared by /restart and /rotate-logs);
        returns (error, by, source, reason) with the text flattened to one line."""
        by = body.get("requested_by")
        if not isinstance(by, str) or not clean_text(by, 120):
            return (400, {"error": "invalid_requested_by", "message": "requested_by is required"}), "", "", None
        by = clean_text(by, 120)
        source = body.get("source")
        if source not in SOURCES:
            return (400, {"error": "invalid_source", "message": f"source must be one of {', '.join(SOURCES)}"}), by, "", None
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            return (400, {"error": "invalid_reason", "message": "reason must be a string or null"}), by, source, None
        return None, by, source, clean_text(reason or "", 300) or None

    def submit_rotate(self, body: dict, now: float | None = None) -> tuple[int, dict]:
        """POST /rotate-logs: validate and queue; the main loop starts the run (drain)."""
        now = time.time() if now is None else now
        err, by, source, reason = self._requester(body)
        if err:
            return err
        if self.rotator is None:
            return 409, {"error": "not_managed", "message": "log rotation is not enabled in this supervisor."}
        with self._lock:
            if self._rotate_pending or self.rotator.is_running():
                return 409, {"error": "rotation_in_progress", "message": "a log rotation is already in progress."}
            last = self._rotate_accepted_at
            if last is not None and now - last < ROTATE_COOLDOWN:
                retry = max(1, math.ceil(ROTATE_COOLDOWN - (now - last)))
                return 429, {"error": "cooldown", "retry_after_s": retry,
                             "message": f"logs were rotated on request moments ago; try again in {retry}s."}
            self._rotate_pending = True
            self._rotate_accepted_at = now
        self.requests.put({"kind": "rotate", "name": "log-rotation", "requested_by": by, "source": source,
                           "reason": reason, "not_before": now})
        slog(f"control: accepted log rotation (requested by {by} via {source}{f': {reason}' if reason else ''})")
        return 202, {"queued": True,
                     "message": "Log rotation queued; it starts within a second. The result appears in "
                                "GET /status under log_rotation.last_run."}

    def submit(self, body: dict, now: float | None = None) -> tuple[int, dict]:
        now = time.time() if now is None else now
        target = body.get("target")
        if not isinstance(target, str) or target not in TARGETS:
            return 400, {"error": "invalid_target", "message": f"target must be one of {', '.join(TARGETS)}"}
        err, by, source, reason = self._requester(body)
        if err:
            return err
        delay = body.get("delay_s")
        if delay is None:
            delay = DEFAULT_REQUEST_DELAY
        elif isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay):
            return 400, {"error": "invalid_delay", "message": "delay_s must be a number"}
        delay = float(min(MAX_REQUEST_DELAY, max(0.0, delay)))

        names = [n for n in TARGETS[target] if n in self.services]
        if not names:
            return 409, {"error": "not_managed", "message": f"{target} is not managed by this supervisor."}
        with self._lock:
            waits = [self.cooldown - (now - self._accepted_at[n]) for n in names
                     if n in self._accepted_at and now - self._accepted_at[n] < self.cooldown]
            if waits:
                retry = max(1, math.ceil(max(waits)))
                return 429, {"error": "cooldown", "retry_after_s": retry,
                             "message": f"{target} was restarted on request moments ago; try again in {retry}s."}
            accepted, skipped = [], []
            for n in names:
                snap = self._snapshot.get(n, {})
                if n in self._pending or snap.get("restart_requested"):
                    skipped.append({"name": n, "error": "restart_in_progress",
                                    "message": f"a restart of {n} is already in progress."})
                elif not snap.get("running") and snap.get("skip_reason") == "port in use":
                    port = getattr(self.services[n], "port", None)
                    skipped.append({"name": n, "error": "not_managed",
                                    "message": f"{n} is not running under the supervisor: port {port} is held by "
                                               "another process it did not start, so a restart would do nothing. "
                                               "Close that process; the supervisor then starts its own copy."})
                else:
                    accepted.append(n)
            if not accepted:
                first = skipped[0]
                return 409, {"error": first["error"], "message": first["message"], "skipped": skipped}
            for n in accepted:
                self._accepted_at[n] = now
                self._pending.add(n)
        for n in accepted:
            self.requests.put({"name": n, "target": target, "requested_by": by, "source": source,
                               "reason": reason, "not_before": now + delay})
        slog(f"control: accepted restart of {', '.join(accepted)} (requested by {by} via {source}"
             f"{f': {reason}' if reason else ''}; in {delay:.1f}s)"
             f"{'; skipped ' + ', '.join(s['name'] for s in skipped) if skipped else ''}")
        return 202, {"accepted": accepted, "skipped": skipped, "delay_s": delay,
                     "eta_s": round(delay + REQUESTED_RESTART_DELAY + 1.0, 1)}

    def logs(self, name: str | None, lines: int) -> tuple[int, dict]:
        tail = self._tails.get(name or "")
        if tail is None:
            return 404, {"error": "unknown_service", "message": f"service must be one of {', '.join(self.order)}"}
        lines = max(1, min(MAX_LOG_LINES, lines))
        copy: list[str] = []
        for _ in range(5):  # the pump thread may append while we copy
            try:
                copy = list(tail)
                break
            except RuntimeError:
                time.sleep(0.01)
        return 200, {"service": name, "lines": scrub_lines(copy[-lines:])}


# ---------------------------------------------------------------------------
# Control API: HTTP server (loopback, bearer auth, JSON only)
# ---------------------------------------------------------------------------

_ROUTES = {"/status": "GET", "/restart": "POST", "/logs": "GET", "/rotate-logs": "POST"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "kick-bot-supervisor"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 5  # per-recv socket timeout; the whole connection is also capped by REQUEST_DEADLINE (setup)

    def setup(self) -> None:
        super().setup()
        # socket.settimeout only bounds each recv, so a client dribbling one
        # header byte every few seconds could hold this thread forever. A timer
        # shuts the socket down after the deadline whatever the client does.
        deadline = getattr(self.server, "request_deadline", REQUEST_DEADLINE)
        self._deadline_timer = threading.Timer(deadline, self._abort_connection)
        self._deadline_timer.daemon = True
        self._deadline_timer.start()

    def _abort_connection(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self) -> None:
        timer = getattr(self, "_deadline_timer", None)
        if timer is not None:
            timer.cancel()
        try:
            super().finish()
        except OSError:
            pass

    # BaseHTTPRequestHandler logs to stderr (pythonw has none): route to slog,
    # and skip routine successful GETs (the dashboard polls /status every 5s).
    def log_message(self, fmt, *args) -> None:
        slog("control: " + (fmt % args))

    def log_request(self, code="-", size="-") -> None:
        try:
            c = int(code)
        except (TypeError, ValueError):
            c = 0
        if self.command != "GET" or c >= 400:
            self.log_message('"%s" %s', self.requestline, str(code))

    def _send(self, code: int, payload: dict, headers: dict | None = None) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _authorized(self) -> bool:
        secret = getattr(self.server, "secret", "")
        header = self.headers.get("Authorization", "")
        if not secret or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode("utf-8"), secret.encode("utf-8"))

    def _read_json(self) -> tuple[dict | None, tuple[int, dict] | None]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return None, (415, {"error": "unsupported_media_type", "message": "send application/json"})
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None, (400, {"error": "bad_request", "message": "invalid Content-Length"})
        if length > MAX_BODY:
            self.close_connection = True
            return None, (413, {"error": "payload_too_large", "message": f"body limit is {MAX_BODY} bytes"})
        if length <= 0:
            return None, (400, {"error": "bad_request", "message": "empty body"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, (400, {"error": "bad_request", "message": "body is not valid JSON"})
        if not isinstance(body, dict):
            return None, (400, {"error": "bad_request", "message": "body must be a JSON object"})
        return body, None

    def _dispatch(self, method: str) -> None:
        try:
            if not self._authorized():
                self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
                return
            url = urlsplit(self.path)
            path = url.path.rstrip("/") or "/"
            if path not in _ROUTES:
                self._send(404, {"error": "not_found"})
                return
            if method != _ROUTES[path]:
                self._send(405, {"error": "method_not_allowed"}, {"Allow": _ROUTES[path]})
                return
            ctrl: Controller = self.server.controller  # type: ignore[attr-defined]
            if path == "/status":
                self._send(200, ctrl.status())
            elif path == "/logs":
                q = parse_qs(url.query)
                try:
                    lines = int((q.get("lines") or ["50"])[0])
                except ValueError:
                    lines = 50
                code, payload = ctrl.logs((q.get("service") or [""])[0], lines)
                self._send(code, payload)
            else:
                body, err = self._read_json()
                if err:
                    self._send(*err)
                    return
                code, payload = ctrl.submit_rotate(body) if path == "/rotate-logs" else ctrl.submit(body)
                headers = {"Retry-After": str(payload["retry_after_s"])} if code == 429 else None
                self._send(code, payload, headers)
        except Exception as e:  # pragma: no cover - defensive
            slog(f"control: error handling {method} {self.path.split('?')[0]}: {type(e).__name__}: {e}")
            try:
                self._send(500, {"error": "internal_error"})
            except Exception:
                pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    max_connections = MAX_CONTROL_CONNECTIONS
    request_deadline = REQUEST_DEADLINE
    # On Windows SO_REUSEADDR lets a second process bind the same port; use
    # SO_EXCLUSIVEADDRUSE there instead so a duplicate cannot share 8767.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # skip HTTPServer's getfqdn() lookup
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port

    def server_activate(self) -> None:
        super().server_activate()
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._last_full_log = 0.0

    def process_request(self, request, client_address) -> None:
        # Bounded concurrency: past max_connections a new connection is closed
        # at once (no thread) instead of spawning threads without limit.
        if not self._slots.acquire(blocking=False):
            if time.time() - self._last_full_log > 60:
                self._last_full_log = time.time()
                slog(f"control: {self.max_connections} connections already open - refusing new ones")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        slog(f"control: connection error from {client_address[0]}: {type(exc).__name__}: {exc}")


class ControlServer:
    """Runs the control API in a daemon thread. start() returns False (and logs
    why) when there is no secret or the port cannot be bound; the supervisor
    keeps running without it."""

    def __init__(self, controller: Controller, secret: str, port: int = CONTROL_PORT) -> None:
        self.controller, self.secret, self.requested_port = controller, secret, port
        self.httpd: _HTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        return self.httpd.server_address[1] if self.httpd else None

    def start(self) -> bool:
        if not self.secret:
            slog("control API disabled: neither SUPERVISOR_CONTROL_SECRET nor INTERNAL_API_SECRET is set")
            return False
        try:
            httpd = _HTTPServer((CONTROL_HOST, self.requested_port), _Handler)
        except OSError as e:
            slog(f"control API disabled: cannot bind {CONTROL_HOST}:{self.requested_port} ({e})")
            return False
        httpd.controller = self.controller  # type: ignore[attr-defined]
        httpd.secret = self.secret  # type: ignore[attr-defined]
        self.httpd = httpd
        self.thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
                                       name="supervisor-control", daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        if self.httpd is None:
            return
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        finally:
            self.httpd = None
            if self.thread is not None:
                self.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

def acquire_instance_lock(path: Path | None = None):
    """Take an exclusive, non-blocking OS lock on `path` (kept open for the
    process lifetime; the OS drops it when the process dies).

    Returns (handle, None) on success, (None, holder_pid_text) when another
    process holds it, or (None, None) plus a log line if the lock file itself
    could not be used (the caller then continues without the guard)."""
    path = Path(path or LOCK_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        f = os.fdopen(fd, "r+b", buffering=0)
    except OSError as e:
        slog(f"single-instance lock unavailable ({e}); continuing without it")
        return None, None
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(LOCK_OFFSET)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - the live deployment is Windows
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = ""
        try:
            f.seek(0)
            parts = f.read(64).decode("ascii", errors="replace").split()
            holder = parts[0] if parts else ""
        except Exception:
            pass
        f.close()
        return None, holder or "unknown"
    f.seek(0)
    f.truncate()
    f.write(f"{os.getpid()} {datetime.now():%Y-%m-%d %H:%M:%S}\n".encode("ascii"))
    return f, None


def release_instance_lock(handle) -> None:
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# CLI client (talks to the RUNNING supervisor; never takes the lock)
# ---------------------------------------------------------------------------

def _cli_call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://{CONTROL_HOST}:{CONTROL_PORT}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {CONTROL_SECRET}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


CLI_USAGE = """usage: supervisor.py <command>   (sv.bat <command> does the same, plus a menu)
  summary                    readable status of the supervisor and its three services
  status                     the same, as raw JSON (GET /status)
  health                     probe the bot, the dashboard and the public site over HTTP
  restart <target> [reason]  target: bot|dashboard|tunnel|all (or b|d|t|a)
  rotate-logs [reason]       archive the logs now
  tail <service> [lines]     last output lines of bot|dashboard|tunnel (secrets scrubbed)
  follow <log>               live view of logs/supervisor/<log>.log (supervisor|bot|dashboard|tunnel);
                             works with the supervisor down; Ctrl+C stops
  pid                        print the running supervisor's pid"""

CLI_ALIASES = {"s": "summary", "j": "status", "json": "status", "c": "health", "r": "restart",
               "rotate": "rotate-logs", "o": "rotate-logs", "t": "tail", "f": "follow"}
CLI_TARGETS = {"b": "bot", "d": "dashboard", "dash": "dashboard", "t": "tunnel", "a": "all"}
FOLLOW_LOGS = ("supervisor", "bot", "dashboard", "tunnel")


def _cli_who() -> str:
    return f"cli ({os.getenv('USERNAME') or os.getenv('USER') or 'local'})"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    for unit, size, sub, subsize in (("d", 86400, "h", 3600), ("h", 3600, "m", 60), ("m", 60, "s", 1)):
        if s >= size:
            return f"{s // size}{unit} {(s % size) // subsize}{sub}"
    return f"{s}s"


def _fmt_when(value: str | None) -> str:
    """ISO timestamp (UTC or with offset) -> local 'YYYY-MM-DD HH:MM'."""
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _fmt_exit(reason: str) -> str:
    m = re.fullmatch(r"exit code (\d+)", reason)
    if m and int(m.group(1)) > 0x7FFFFFFF:  # Windows NTSTATUS codes read better in hex
        return f"{reason} (0x{int(m.group(1)):08X})"
    return reason


def _service_state(s: dict) -> str:
    if s.get("restart_pending"):
        state = "restarting"
    elif s.get("running"):
        state = "running"
    elif s.get("skip_reason"):
        state = "not managed"
    elif s.get("waiting_on_dependency"):
        state = "waiting for bot"
    elif s.get("next_start_in_s") is not None:
        state = f"down, retry in {int(s['next_start_in_s'])}s"
    else:
        state = "down"
    if s.get("health_fails"):
        state += f" (health {s['health_fails']}/{HEALTH_FAILS})"
    return state


def format_summary(payload: dict) -> str:
    sup = payload.get("supervisor") or {}
    started = sup.get("started_at")
    up = None
    if started:
        try:
            up = (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds()
        except ValueError:
            pass
    out = [f"Supervisor v{sup.get('version', '?')}  pid {sup.get('pid', '?')}  "
           f"up {_fmt_duration(up)}  (since {_fmt_when(started)})", ""]
    rows = [("SERVICE", "STATE", "PID", "UPTIME", "RESTARTS")]
    for s in payload.get("services") or []:
        rows.append((s.get("name", "?"), _service_state(s), str(s.get("pid") or "-"),
                     _fmt_duration(s.get("uptime_s")) if s.get("running") else "-", str(s.get("restarts", 0))))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    out.append("")
    for s in payload.get("services") or []:
        last = s.get("last_exit")
        if last:
            out.append(f"{s.get('name')}: last exit {_fmt_when(last.get('at'))}: {_fmt_exit(last.get('reason') or '?')}")
        req = s.get("last_request")
        if req:
            reason = f" ({req['reason']})" if req.get("reason") else ""
            out.append(f"{s.get('name')}: last requested restart {_fmt_when(req.get('at'))} "
                       f"by {req.get('by')} via {req.get('source')}{reason}")
    rot = payload.get("log_rotation")
    if rot:
        last = rot.get("last_run") or {}
        last_txt = f"{_fmt_when(last.get('at'))} ({'ok' if last.get('ok') else 'problems'})" if last else "never"
        extra = "  OVERDUE" if rot.get("overdue") else ""
        if rot.get("deferred"):
            extra += f"  deferred: {', '.join(map(str, rot['deferred']))}"
        out.append(f"log rotation: last {last_txt}, next {_fmt_when(rot.get('next_run_at'))}{extra}")
    return "\n".join(out)


def _probe(url: str, timeout: float) -> tuple[bool, str, dict | None]:
    t0 = time.monotonic()
    # Cloudflare answers 403 to urllib's default "Python-urllib" User-Agent.
    req = urllib.request.Request(url, headers={"User-Agent": f"kick-bot-supervisor/{VERSION} (sv health)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ms = int((time.monotonic() - t0) * 1000)
            try:
                body = json.loads(r.read() or b"{}")
            except ValueError:
                body = None
            return r.status < 400, f"HTTP {r.status} in {ms} ms", body
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, f"no answer ({reason})", None


def cli_health() -> int:
    """HTTP probes, unlike the supervisor's own dashboard check (TCP only), so a hung
    process that still accepts connections shows up here as a timeout."""
    public = (os.getenv("APPEAL_BASE_URL", "").strip() or "https://dash.idlesys.xyz").rstrip("/")
    checks = [
        ("supervisor", f"http://{CONTROL_HOST}:{CONTROL_PORT}/status", None),
        ("bot (internal API)", f"http://127.0.0.1:{INTERNAL_PORT}/health", 5),
        ("dashboard (local)", "http://127.0.0.1:8080/api/status", 5),
        ("public site (tunnel)", f"{public}/api/status", 10),
    ]
    all_ok = True
    for label, url, timeout in checks:
        if timeout is None:
            try:
                code, _ = _cli_call("GET", "/status")
                ok, detail = code < 400, f"control API HTTP {code}"
            except (urllib.error.URLError, OSError) as e:
                ok, detail = False, f"control API not reachable ({getattr(e, 'reason', e)})"
        else:
            ok, detail, body = _probe(url, timeout)
            if ok and isinstance(body, dict) and "bot" in body:
                parts = [f"{k} {(body.get(k) or {}).get('status', '?')}" for k in ("dashboard", "internal_api", "bot")]
                detail += f"  [{', '.join(parts)}]"
        all_ok = all_ok and ok
        print(f"  {'OK  ' if ok else 'FAIL'}  {label:<21} {detail}")
    return 0 if all_ok else 1


def cli_follow(name: str, lines: int = 40, poll: float = 1.0, max_polls: int | None = None) -> int:
    """Print the end of logs/supervisor/<name>.log, then new lines as they arrive. The
    file is only open during each read, never held, so the weekly rotation can still
    rename it; a file that got smaller (rotated) is read again from the start."""
    name = {"s": "supervisor", **CLI_TARGETS}.get(name, name)
    if name not in FOLLOW_LOGS:
        print(f"follow: log must be one of {', '.join(FOLLOW_LOGS)}")
        return 2
    path = LOG_DIR / f"{name}.log"
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass
    pos = 0
    try:
        with open(path, "rb") as f:
            start = max(0, path.stat().st_size - 64 * 1024)
            f.seek(start)
            data = f.read()
        cut = data.rfind(b"\n") + 1
        pos = start + cut
        shown = data[:cut].decode("utf-8", "replace").splitlines()
        if start:
            shown = shown[1:]  # the first line was cut by the seek
        print("\n".join(shown[-lines:]))
    except FileNotFoundError:
        pass
    print(f"--- following {path} (Ctrl+C to stop) ---", flush=True)
    polls = 0
    try:
        while max_polls is None or polls < max_polls:
            polls += 1
            time.sleep(poll)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue  # mid-rotation: the next write recreates it
            if size < pos:
                pos = 0
                print("--- log rotated ---", flush=True)
            if size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    data = f.read()
                cut = data.rfind(b"\n") + 1  # keep a half-written line for the next read
                if cut:
                    sys.stdout.write(data[:cut].decode("utf-8", "replace"))
                    sys.stdout.flush()
                    pos += cut
    except KeyboardInterrupt:
        pass
    return 0


def cli(argv: list[str]) -> int:
    cmd = CLI_ALIASES.get(argv[0], argv[0])
    if cmd in ("help", "-h", "--help", "h"):
        print(CLI_USAGE)
        return 0
    if cmd == "health":
        return cli_health()
    if cmd == "follow":
        return cli_follow(argv[1] if len(argv) >= 2 else "supervisor")
    try:
        if cmd in ("status", "summary", "pid"):
            code, payload = _cli_call("GET", "/status")
            if code < 400 and cmd == "summary":
                print(format_summary(payload))
                return 0
            if code < 400 and cmd == "pid":
                print((payload.get("supervisor") or {}).get("pid", ""))
                return 0
        elif cmd == "restart" and len(argv) >= 2:
            code, payload = _cli_call("POST", "/restart", {
                "target": CLI_TARGETS.get(argv[1], argv[1]), "requested_by": _cli_who(),
                "source": "cli", "reason": " ".join(argv[2:]).strip() or None,
            })
        elif cmd == "rotate-logs":
            code, payload = _cli_call("POST", "/rotate-logs", {
                "requested_by": _cli_who(), "source": "cli", "reason": " ".join(argv[1:]).strip() or None,
            })
        elif cmd == "tail" and len(argv) >= 2:
            lines = argv[2] if len(argv) >= 3 and argv[2].isdigit() else "50"
            code, payload = _cli_call("GET", f"/logs?service={CLI_TARGETS.get(argv[1], argv[1])}&lines={lines}")
            if code < 400:
                print("\n".join(payload.get("lines") or []))
                return 0
        else:
            print(CLI_USAGE)
            return 2
    except (urllib.error.URLError, OSError) as e:
        print(f"supervisor control API not reachable on {CONTROL_HOST}:{CONTROL_PORT}: {e}")
        return 1
    print(json.dumps(payload, indent=2))
    return 0 if code < 400 else 1


def main() -> int:
    if not (ROOT / ".env").exists():
        print("ERROR: .env not found in", ROOT)
        return 1
    if not Path(PY).exists():
        print("ERROR: venv python not found:", PY)
        return 1

    lock, holder = acquire_instance_lock()
    if lock is None and holder is not None:
        slog(f"another supervisor is already running (pid {holder}) - exiting without starting anything")
        return 3

    bot = Service("bot", [PY, "main.py"], ROOT, port=INTERNAL_PORT, health=internal_api_ok)
    dash = Service(
        "dashboard",
        [PY, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", "8080"],
        ROOT, port=8080, health=lambda: port_open(8080), needs=internal_api_ok,
    )
    tunnel = Service("tunnel", ["cloudflared", "tunnel", "run", "idlesys"], ROOT / "server")
    services = [bot, dash, tunnel]

    rotator = LogRotator(services)
    for s in services:
        s.before_start = lambda name=s.name: rotator.before_start(name)
    controller = Controller(services, cooldown=RESTART_COOLDOWN, secret=CONTROL_SECRET, rotator=rotator)
    control = ControlServer(controller, CONTROL_SECRET, CONTROL_PORT)
    control_on = control.start()

    slog(f"supervisor up v{VERSION} pid {os.getpid()} (webhook={'on' if WEBHOOK else 'off'}, ping={PING or 'none'}, "
         f"control={'127.0.0.1:%d' % control.port if control_on else 'off'})")
    alert("Supervisor started", "Starting bot, dashboard and tunnel."
          + (f"\nControl API on 127.0.0.1:{control.port}." if control_on else "\nControl API off."), 0x2ECC71, False)
    try:
        try:
            rotator.start()
        except Exception as e:  # rotation must never stop the supervisor
            slog(f"log rotation: start failed: {type(e).__name__}: {e}")
        while True:
            now = time.time()
            controller.drain(now)
            rotator.tick()
            for s in services:
                s.poll(now)
            controller.publish()
            time.sleep(1)
    except KeyboardInterrupt:
        slog("supervisor stopping (Ctrl+C)")
    finally:
        control.stop()
        for s in reversed(services):
            s.stop()
        alert("Supervisor stopped", "All managed processes were stopped.", 0x95A5A6, False)
        time.sleep(1.5)
        release_instance_lock(lock)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(cli(sys.argv[1:]))
    sys.exit(main())
