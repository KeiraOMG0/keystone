"""Manages the bgutil PO Token provider's companion Node server.

yt-dlp needs a proof-of-origin token to get playable stream URLs out of
YouTube for many videos now (see yt-dlp issue #12482 / the SABR rollout).
The bgutil-ytdlp-pot-provider plugin talks to a small local Node HTTP
server to generate those tokens -- this service starts/stops/supervises that
server alongside the bot so nothing has to be run manually.

Safety model for process handling: we only ever kill a PID that (a) we
spawned ourselves, or (b) is named by our PID file AND whose command line
still contains the pot server script path. Never by port or process name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import aiohttp

import config as _config

log = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"
_LOG_ROTATE_BYTES = 1_000_000
_STARTUP_POLLS = 20
_STARTUP_POLL_INTERVAL = 0.5
_STOP_WAIT_SECONDS = 5.0
_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 300.0
_FAIL_THRESHOLD = 2  # consecutive failed pings before the watchdog restarts


# ---------------------------------------------------------------------------
# Helpers (module level so cogs/tests can reuse them)
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """Project root: the executable's directory when frozen, else the repo root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_path(p: str) -> Path:
    """Resolve a (possibly relative) config path against the project root."""
    path = Path(os.path.expanduser(p))
    if not path.is_absolute():
        path = project_root() / path
    return path


def _connect_host(host: str) -> str:
    """Host to *connect* to for a server bound to `host`."""
    if host in ("", "0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def pot_base_url() -> str:
    """Base URL yt-dlp's bgutil HTTP provider should use, from config."""
    return f"http://{_connect_host(_config.MUSIC_POT_SERVER_HOST)}:{_config.MUSIC_POT_SERVER_PORT}"


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _get_cmdline_sync(pid: int) -> Optional[str]:
    """Best-effort command line of `pid`, or None if unknown/not running."""
    try:
        import psutil  # type: ignore[import-not-found]

        from services.psutil_guard import PSUTIL_LOCK

        try:
            with PSUTIL_LOCK:  # psutil is never called from two threads at once
                return " ".join(psutil.Process(pid).cmdline())
        except psutil.Error:
            return None
    except ImportError:
        pass
    try:
        if _IS_WINDOWS:
            out = subprocess.run(
                [
                    "powershell", "-NoProfile", "-NonInteractive", "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
                ],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            text = out.stdout.strip()
            return text or None
        proc = Path(f"/proc/{int(pid)}/cmdline")
        if proc.exists():
            return proc.read_bytes().replace(b"\0", b" ").decode(errors="replace").strip() or None
        out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "args="], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        log.debug("Could not read command line for pid %s", pid, exc_info=True)
        return None


def _norm(s: str) -> str:
    return s.replace("\\", "/").lower()


def _kill_tree_sync(pid: int) -> None:
    """Kill `pid` and its children. Caller has already verified the PID is ours."""
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(int(pid))],
                capture_output=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            log.warning("taskkill failed for pid %s", pid, exc_info=True)
    else:
        import signal

        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            except Exception:
                log.warning("Could not signal pid %s", pid, exc_info=True)
                return
            time.sleep(0.5)


class PotServerService:
    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._log_fh = None
        self._lock = asyncio.Lock()
        self._watchdog_task: Optional[asyncio.Task] = None
        self._stopping = False
        self.state: str = "stopped"
        self.last_error: Optional[str] = None
        self.restart_count: int = 0  # total restarts by the watchdog
        self._consecutive_restarts: int = 0
        self._ping_failures: int = 0
        self._started_at: Optional[float] = None  # monotonic
        self._adopted: bool = False  # server is up but not spawned by us

    # ------------------------------------------------------------------
    # Config accessors (read live so tests / hot config changes work)
    # ------------------------------------------------------------------

    @property
    def _host(self) -> str:
        return _config.MUSIC_POT_SERVER_HOST

    @property
    def _port(self) -> int:
        return _config.MUSIC_POT_SERVER_PORT

    def _script(self) -> Path:
        return resolve_path(_config.MUSIC_POT_SERVER_SCRIPT)

    def _node(self) -> str:
        return _config.MUSIC_POT_SERVER_NODE or _config.MUSIC_NODE_PATH

    def _log_path(self) -> Path:
        return resolve_path(_config.MUSIC_POT_SERVER_LOG)

    def _pid_path(self) -> Path:
        return self._log_path().with_suffix(".pid")

    @property
    def pid(self) -> Optional[int]:
        if self._process is not None and self._process.poll() is None:
            return self._process.pid
        return None

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def _ping(self) -> tuple[str, Optional[dict]]:
        """Return ("ok"|"wrong"|"down", json). "wrong" = something answered
        on the port but it does not look like the bgutil pot server."""
        url = f"{pot_base_url()}/ping"
        try:
            timeout = aiohttp.ClientTimeout(total=2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return "wrong", None
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        return "wrong", None
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return "down", None
        if isinstance(data, dict) and "server_uptime" in data and "version" in data:
            return "ok", data
        return "wrong", data if isinstance(data, dict) else None

    async def _is_up(self) -> bool:
        return (await self._ping())[0] == "ok"

    # ------------------------------------------------------------------
    # PID file / orphan handling
    # ------------------------------------------------------------------

    def _write_pid_file(self, pid: int) -> None:
        try:
            p = self._pid_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"pid": pid, "script": str(self._script()), "port": self._port}))
        except OSError:
            log.warning("Could not write PID file %s", self._pid_path(), exc_info=True)

    def _remove_pid_file(self) -> None:
        try:
            self._pid_path().unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("Could not remove PID file %s", self._pid_path(), exc_info=True)

    def _read_pid_file(self) -> Optional[int]:
        try:
            data = json.loads(self._pid_path().read_text())
            pid = int(data["pid"])
            return pid if pid > 0 else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("Ignoring unreadable PID file %s", self._pid_path())
            return None

    async def _find_verified_orphan(self) -> Optional[int]:
        """PID named by the PID file whose command line still contains our
        script path -- the only kind of stray process we're willing to kill.
        A PID file that doesn't check out is deleted (never acted on)."""
        pid = self._read_pid_file()
        if pid is None:
            return None
        if self._process is not None and self._process.pid == pid:
            return None  # that's our own live child, not an orphan
        cmdline = await asyncio.to_thread(_get_cmdline_sync, pid)
        # Script path alone is not enough (a reused PID could be e.g. an editor with the file open):
        # the process must also be a node process running our script.
        if cmdline and "node" in _norm(cmdline) and _norm(str(self._script())) in _norm(cmdline):
            return pid
        log.info("Stale PID file (pid %s no longer a pot server); removing it", pid)
        self._remove_pid_file()
        return None

    async def _reap_orphan(self, pid: int) -> None:
        log.warning("Replacing orphaned PO token server from a previous run (pid %s)", pid)
        await asyncio.to_thread(_kill_tree_sync, pid)
        self._remove_pid_file()
        for _ in range(10):
            await _sleep(0.3)
            if (await self._ping())[0] != "ok":
                break

    # ------------------------------------------------------------------
    # Logging of the child's output
    # ------------------------------------------------------------------

    def _open_log(self):
        path = self._log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.stat().st_size > _LOG_ROTATE_BYTES:
                backup = path.with_name(path.name + ".1")
                if backup.exists():
                    backup.unlink()
                path.rename(backup)
        except OSError:
            log.warning("Could not rotate %s", path, exc_info=True)
        fh = open(path, "ab")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"\n===== pot server launch {stamp} (port {self._port}) =====\n".encode())
        fh.flush()
        return fh

    def _close_log(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    def log_tail(self, lines: int = 15) -> list[str]:
        """Last `lines` lines of the pot server's log file (may be empty)."""
        try:
            path = self._log_path()
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 65536))
                data = f.read()
            text = data.decode(errors="replace").splitlines()
            return [ln for ln in text if ln.strip()][-lines:]
        except OSError:
            return []

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    def _fail(self, msg: str) -> bool:
        self.last_error = msg
        self.state = "failed"
        log.warning("PO token server: %s", msg)
        return False

    async def start(self) -> bool:
        """Start the PO token server if it isn't already running, and start
        the watchdog. Returns True if a valid server is up afterwards.
        Never raises. Safe to call repeatedly (e.g. on every on_ready)."""
        async with self._lock:
            try:
                self._stopping = False
                ok = await self._start_locked()
            except Exception as e:
                log.exception("Unexpected error starting PO token server")
                return self._fail(f"unexpected error: {e}")
            if ok and _config.MUSIC_POT_SERVER_WATCHDOG:
                self._ensure_watchdog()
            return ok

    async def _start_locked(self) -> bool:
        if not _config.MUSIC_POT_SERVER_ENABLED:
            self.state = "disabled"
            log.info("PO token server disabled via MUSIC_POT_SERVER_ENABLED")
            return False

        status, _data = await self._ping()
        orphan = await self._find_verified_orphan() if self._process is None else None

        if status == "ok" and orphan is None:
            self.state = "running"
            self._adopted = self._process is None
            self.last_error = None
            log.info("PO token server already running at %s", pot_base_url())
            return True
        if status == "wrong":
            return self._fail(
                f"something else is answering on {pot_base_url()} but it is not the bgutil pot server "
                "(/ping had the wrong shape). Change MUSIC_POT_SERVER_PORT or free the port."
            )
        if orphan is not None:
            await self._reap_orphan(orphan)

        node = self._node()
        if not node:
            return self._fail(
                "no Node.js binary found on PATH. Age/sign-in-gated videos may fail to play. "
                "Install Node.js or set MUSIC_POT_SERVER_NODE / MUSIC_NODE_PATH."
            )
        script = self._script()
        if not script.is_file():
            return self._fail(
                f"script not found at {script}. Run the setup steps in pot_server/README "
                "(npm ci && npx tsc) first, or set MUSIC_POT_SERVER_SCRIPT."
            )
        return await self._launch(node, script)

    async def _launch(self, node: str, script: Path) -> bool:
        self.state = "starting"
        self._close_log()
        try:
            self._log_fh = self._open_log()
        except OSError:
            log.warning("Could not open pot server log file; output will be discarded", exc_info=True)
            self._log_fh = None
        sink = self._log_fh if self._log_fh is not None else subprocess.DEVNULL
        cmd = [node, str(script), "--port", str(self._port), "--host", self._host]
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=sink,
                stderr=subprocess.STDOUT if self._log_fh is not None else subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except FileNotFoundError:
            self._close_log()
            return self._fail(f"could not execute Node at {node!r} (not found)")
        except Exception as e:
            self._close_log()
            log.exception("Failed to start PO token server")
            return self._fail(f"failed to spawn process: {e}")

        proc = self._process
        self._write_pid_file(proc.pid)
        self._started_at = time.monotonic()
        self._adopted = False

        for _ in range(_STARTUP_POLLS):
            await _sleep(_STARTUP_POLL_INTERVAL)
            if (await self._ping())[0] == "ok":
                self.state = "running"
                self.last_error = None
                self._ping_failures = 0
                log.info("PO token server started at %s (pid %d)", pot_base_url(), proc.pid)
                return True
            if proc.poll() is not None:
                tail = " | ".join(self.log_tail(3))
                self._process = None
                self._close_log()
                self._remove_pid_file()
                return self._fail(
                    f"exited immediately (code {proc.returncode}); last output: {tail or '<none>'}"
                )

        # Never became healthy: don't leave a half-alive child behind.
        await asyncio.to_thread(self._terminate_process)
        return self._fail(
            f"did not respond to /ping within {_STARTUP_POLLS * _STARTUP_POLL_INTERVAL:.0f}s after starting"
        )

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    def _terminate_process(self) -> None:
        """Terminate our own child: terminate -> wait -> kill (+ tree on Windows)."""
        proc = self._process
        self._process = None
        if proc is not None and proc.poll() is None:
            pid = proc.pid
            log.info("Stopping PO token server (pid %d)", pid)
            try:
                proc.terminate()
                proc.wait(timeout=_STOP_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                log.warning("PO token server (pid %d) ignored terminate; killing", pid)
                if _IS_WINDOWS and proc.poll() is None:
                    # Still alive and not yet reaped (we hold its handle, so the PID is still ours):
                    # drop the whole tree, node may have spawned children. This is deliberately NOT
                    # done after a clean exit, when the PID could already belong to someone else.
                    _kill_tree_sync(pid)
                try:
                    proc.kill()
                except OSError:
                    pass
            except OSError:
                pass
            try:
                proc.wait(timeout=_STOP_WAIT_SECONDS)
            except Exception:
                log.warning("PO token server (pid %d) may still be running", pid)
        self._close_log()
        if proc is not None and self._read_pid_file() in (None, proc.pid):
            # Only delete the PID file of a server WE spawned (an adopted/foreign one keeps its file).
            self._remove_pid_file()

    def stop(self) -> None:
        """Stop the watchdog and the server we spawned. Synchronous so it can
        be called from a `finally`. Never touches a server we didn't spawn."""
        self._stopping = True
        task, self._watchdog_task = self._watchdog_task, None
        if task is not None and not task.done():
            task.cancel()
        self._terminate_process()
        self._started_at = None
        if self.state != "disabled":
            self.state = "stopped"

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def _ensure_watchdog(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="pot-server-watchdog")

    async def _watchdog_loop(self) -> None:
        try:
            while not self._stopping:
                await _sleep(_config.MUSIC_POT_SERVER_HEALTH_INTERVAL)
                if self._stopping:
                    return
                if not await self._check_and_heal():
                    return
        except asyncio.CancelledError:
            log.debug("PO token server watchdog cancelled")
            raise
        except Exception:
            log.exception("PO token server watchdog crashed")

    async def _check_and_heal(self) -> bool:
        """One watchdog tick. Returns False when the watchdog should stop
        (restart cap reached)."""
        status, _ = await self._ping()
        exited = self._process is not None and self._process.poll() is not None
        if status == "ok" and not exited:
            self._ping_failures = 0
            self._consecutive_restarts = 0
            if self.state != "running":
                self.state = "running"
            return True

        self._ping_failures += 1
        if not exited and self._ping_failures < _FAIL_THRESHOLD:
            log.warning("PO token server health check failed (%s); will retry", status)
            return True

        cap = _config.MUSIC_POT_SERVER_MAX_RESTARTS
        if self._consecutive_restarts >= cap:
            self.state = "gave_up"
            self.last_error = f"unhealthy; gave up after {cap} restart attempt(s)"
            log.error(
                "PO token server is unhealthy and the restart cap (%d) was reached; giving up. "
                "Fix the problem and restart the bot (see %s).", cap, self._log_path(),
            )
            return False

        delay = min(_BACKOFF_BASE ** self._consecutive_restarts, _BACKOFF_CAP)
        self._consecutive_restarts += 1
        self.restart_count += 1
        log.warning(
            "PO token server is %s (status=%s). Restart %d/%d in %.0fs",
            "dead" if exited else "unresponsive", status, self._consecutive_restarts, cap, delay,
        )
        await _sleep(delay)
        if self._stopping:
            return False
        async with self._lock:
            if self._stopping:
                return False
            await asyncio.to_thread(self._terminate_process)
            try:
                ok = await self._start_locked()
            except Exception as e:
                log.exception("PO token server restart failed")
                ok = self._fail(f"restart error: {e}")
        if ok:
            self._ping_failures = 0
        else:
            log.error("PO token server restart attempt %d failed: %s", self._consecutive_restarts, self.last_error)
        return True

    # ------------------------------------------------------------------
    # Introspection (for /musicstatus and the dashboard)
    # ------------------------------------------------------------------

    def snapshot(self, log_lines: int = 10) -> dict[str, Any]:
        """Cheap synchronous state (no network)."""
        uptime = None
        if self._started_at is not None and self.pid is not None:
            uptime = round(time.monotonic() - self._started_at, 1)
        return {
            "enabled": bool(_config.MUSIC_POT_SERVER_ENABLED),
            "state": self.state,
            "running": self.state == "running",
            "pid": self.pid,
            "adopted_external": self._adopted,
            "host": self._host,
            "port": self._port,
            "url": pot_base_url(),
            "uptime_seconds": uptime,
            "restart_count": self.restart_count,
            "watchdog": bool(_config.MUSIC_POT_SERVER_WATCHDOG),
            "watchdog_active": self._watchdog_task is not None and not self._watchdog_task.done(),
            "last_error": self.last_error,
            "log_path": str(self._log_path()),
            "log_tail": self.log_tail(log_lines),
        }

    async def status(self, log_lines: int = 10) -> dict[str, Any]:
        """snapshot() plus a live /ping and environment versions."""
        info = self.snapshot(log_lines)
        state, data = await self._ping()
        info["ping"] = state
        info["responding"] = state == "ok"
        if data and state == "ok":
            info["server_version"] = data.get("version")
            info["server_uptime_seconds"] = data.get("server_uptime")
        info["cookies"] = _cookies_info()
        info.update(await asyncio.to_thread(_versions))
        return info


def _cookies_info() -> dict[str, Any]:
    """Cookies-file presence and age. NEVER reads/prints the contents."""
    path = resolve_path(_config.MUSIC_COOKIES_FILE)
    try:
        st = path.stat()
        return {"present": True, "path": str(path), "age_hours": round((time.time() - st.st_mtime) / 3600, 1)}
    except OSError:
        return {"present": False, "path": str(path), "age_hours": None}


def _versions() -> dict[str, Optional[str]]:
    out: dict[str, Optional[str]] = {"node_version": None, "ytdlp_version": None, "bgutil_plugin_version": None}
    node = _config.MUSIC_POT_SERVER_NODE or _config.MUSIC_NODE_PATH
    if node:
        try:
            r = subprocess.run(
                [node, "--version"], capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out["node_version"] = r.stdout.strip() or None
        except Exception:
            pass
    try:
        from yt_dlp.version import __version__ as ydv

        out["ytdlp_version"] = ydv
    except Exception:
        pass
    try:
        from importlib import metadata

        out["bgutil_plugin_version"] = metadata.version("bgutil-ytdlp-pot-provider")
    except Exception:
        pass
    return out
