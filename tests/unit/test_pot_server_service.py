"""PotServerService tests -- everything mocked (no node, no sockets, no real PIDs)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("INTERNAL_API_SECRET", "test-secret")

import config  # noqa: E402
from services import pot_server_service as pss  # noqa: E402


class FakeProc:
    _next_pid = 5000

    def __init__(self, exit_code=None, ignore_terminate=False):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.returncode = exit_code
        self.ignore_terminate = ignore_terminate
        self.calls: list[str] = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        if not self.ignore_terminate:
            self.returncode = 0

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("node", timeout)
        return self.returncode


@pytest.fixture
def env(tmp_path, monkeypatch):
    script = tmp_path / "build" / "main.js"
    script.parent.mkdir()
    script.write_text("// fake")
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_ENABLED", True)
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_PORT", 4499)
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_SCRIPT", str(script))
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_NODE", "node-fake")
    monkeypatch.setattr(config, "MUSIC_NODE_PATH", "node-fake")
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_LOG", str(tmp_path / "logs" / "pot.log"))
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_WATCHDOG", False)
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_HEALTH_INTERVAL", 30)
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_MAX_RESTARTS", 3)
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(pss, "_sleep", fake_sleep)
    killed: list[int] = []
    monkeypatch.setattr(pss, "_kill_tree_sync", lambda pid: killed.append(pid))
    return {"script": script, "tmp": tmp_path, "sleeps": sleeps, "killed": killed}


def _ping_seq(svc, *results):
    """Make svc._ping return results in order (last one repeats)."""
    seq = list(results)

    async def fake():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    svc._ping = fake


OK = ("ok", {"server_uptime": 1.0, "version": "1.0"})
DOWN = ("down", None)
WRONG = ("wrong", {"hello": "world"})


@pytest.mark.asyncio
async def test_disabled(env, monkeypatch):
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_ENABLED", False)
    svc = pss.PotServerService()
    assert await svc.start() is False
    assert svc.state == "disabled"


@pytest.mark.asyncio
async def test_already_up_and_valid_is_reused(env, monkeypatch):
    svc = pss.PotServerService()
    _ping_seq(svc, OK)
    popen = []
    monkeypatch.setattr(pss.subprocess, "Popen", lambda *a, **k: popen.append(1))
    assert await svc.start() is True
    assert svc.state == "running" and svc.snapshot()["adopted_external"] is True
    assert not popen


@pytest.mark.asyncio
async def test_up_but_wrong_responder(env, monkeypatch):
    svc = pss.PotServerService()
    _ping_seq(svc, WRONG)
    popen = []
    monkeypatch.setattr(pss.subprocess, "Popen", lambda *a, **k: popen.append(1))
    assert await svc.start() is False
    assert svc.state == "failed" and "not the bgutil pot server" in svc.last_error
    assert not popen and not env["killed"]  # never kill by port


@pytest.mark.asyncio
async def test_ping_shape_validation(env, monkeypatch):
    class Resp:
        def __init__(self, status, payload):
            self.status, self._p = status, payload

        async def json(self, content_type=None):
            if isinstance(self._p, Exception):
                raise self._p
            return self._p

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def make_session(resp):
        class Sess:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url):
                assert url == "http://127.0.0.1:4499/ping"
                return resp

        return Sess

    svc = pss.PotServerService()
    monkeypatch.setattr(pss.aiohttp, "ClientSession", make_session(Resp(200, {"server_uptime": 3, "version": "x"})))
    assert (await svc._ping())[0] == "ok"
    monkeypatch.setattr(pss.aiohttp, "ClientSession", make_session(Resp(200, {"pong": True})))
    assert (await svc._ping())[0] == "wrong"
    monkeypatch.setattr(pss.aiohttp, "ClientSession", make_session(Resp(200, ValueError("html"))))
    assert (await svc._ping())[0] == "wrong"
    monkeypatch.setattr(pss.aiohttp, "ClientSession", make_session(Resp(404, {})))
    assert (await svc._ping())[0] == "wrong"


@pytest.mark.asyncio
async def test_node_missing(env, monkeypatch):
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_NODE", "")
    monkeypatch.setattr(config, "MUSIC_NODE_PATH", "")
    svc = pss.PotServerService()
    _ping_seq(svc, DOWN)
    assert await svc.start() is False
    assert "Node.js" in svc.last_error


@pytest.mark.asyncio
async def test_script_missing(env):
    env["script"].unlink()
    svc = pss.PotServerService()
    _ping_seq(svc, DOWN)
    assert await svc.start() is False
    assert "script not found" in svc.last_error


@pytest.mark.asyncio
async def test_immediate_exit(env, monkeypatch):
    svc = pss.PotServerService()
    _ping_seq(svc, DOWN)
    monkeypatch.setattr(pss.subprocess, "Popen", lambda *a, **k: FakeProc(exit_code=1))
    assert await svc.start() is False
    assert "exited immediately" in svc.last_error
    assert svc.pid is None
    assert not svc._pid_path().exists()


@pytest.mark.asyncio
async def test_start_success_passes_port_host_and_writes_pid(env, monkeypatch):
    svc = pss.PotServerService()
    _ping_seq(svc, DOWN, OK)  # not up before launch, healthy after
    seen = {}
    proc = FakeProc()

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        return proc

    monkeypatch.setattr(pss.subprocess, "Popen", popen)
    assert await svc.start() is True
    assert seen["cmd"][0] == "node-fake"
    assert seen["cmd"][1:] == [str(env["script"]), "--port", "4499", "--host", "127.0.0.1"]
    assert svc.state == "running" and svc.pid == proc.pid
    assert json.loads(svc._pid_path().read_text())["pid"] == proc.pid
    snap = svc.snapshot()
    assert snap["running"] and snap["port"] == 4499 and snap["url"] == "http://127.0.0.1:4499"
    svc.stop()
    assert not svc._pid_path().exists()


@pytest.mark.asyncio
async def test_failed_start_can_be_retried(env, monkeypatch):
    svc = pss.PotServerService()
    _ping_seq(svc, DOWN)
    monkeypatch.setattr(pss.subprocess, "Popen", lambda *a, **k: FakeProc(exit_code=2))
    assert await svc.start() is False
    _ping_seq(svc, DOWN, OK)
    monkeypatch.setattr(pss.subprocess, "Popen", lambda *a, **k: FakeProc())
    assert await svc.start() is True


def test_stop_terminates_waits_kills_and_kills_tree_on_windows(env, monkeypatch):
    monkeypatch.setattr(pss, "_IS_WINDOWS", True)
    svc = pss.PotServerService()
    proc = FakeProc(ignore_terminate=True)
    svc._process = proc
    svc.stop()
    assert proc.calls[:3] == ["terminate", "wait", "kill"]
    assert env["killed"] == [proc.pid]  # taskkill /T /F equivalent
    assert svc._process is None and svc.state == "stopped"


def test_stop_never_touches_unspawned_server(env):
    svc = pss.PotServerService()
    svc.stop()
    assert env["killed"] == []


def test_clean_exit_does_not_taskkill_a_possibly_reused_pid(env, monkeypatch):
    monkeypatch.setattr(pss, "_IS_WINDOWS", True)
    svc = pss.PotServerService()
    proc = FakeProc()  # exits on terminate()
    svc._process = proc
    svc.stop()
    assert proc.calls[:2] == ["terminate", "wait"] and "kill" not in proc.calls
    assert env["killed"] == []


def test_stop_keeps_pid_file_of_server_it_did_not_spawn(env):
    svc = pss.PotServerService()
    svc._pid_path().parent.mkdir(parents=True, exist_ok=True)
    svc._pid_path().write_text(json.dumps({"pid": 4242}))
    svc.stop()  # adopted/foreign: self._process is None
    assert svc._pid_path().exists()


def test_stop_removes_pid_file_of_spawned_server(env):
    svc = pss.PotServerService()
    proc = FakeProc()
    svc._process = proc
    svc._write_pid_file(proc.pid)
    svc.stop()
    assert not svc._pid_path().exists()


@pytest.mark.asyncio
async def test_orphan_cmdline_from_other_program_is_not_killed(env, monkeypatch):
    svc = pss.PotServerService()
    svc._pid_path().parent.mkdir(parents=True, exist_ok=True)
    svc._pid_path().write_text(json.dumps({"pid": 4242}))
    # script path present, but the process is not node (e.g. an editor with the file open)
    monkeypatch.setattr(pss, "_get_cmdline_sync", lambda pid: f"code.exe {env['script']}")
    assert await svc._find_verified_orphan() is None
    assert env["killed"] == [] and not svc._pid_path().exists()


@pytest.mark.asyncio
async def test_stale_orphan_verified_is_replaced(env, monkeypatch):
    svc = pss.PotServerService()
    svc._pid_path().parent.mkdir(parents=True, exist_ok=True)
    svc._pid_path().write_text(json.dumps({"pid": 4242}))
    monkeypatch.setattr(pss, "_get_cmdline_sync", lambda pid: f"node {env['script']} --port 4499")
    # up (orphan) -> after kill: down -> after launch: ok
    _ping_seq(svc, OK, DOWN, OK)
    monkeypatch.setattr(pss.subprocess, "Popen", lambda *a, **k: FakeProc())
    assert await svc.start() is True
    assert env["killed"] == [4242]
    assert svc.snapshot()["adopted_external"] is False


@pytest.mark.asyncio
async def test_pid_file_with_unrelated_process_is_not_killed(env, monkeypatch):
    svc = pss.PotServerService()
    svc._pid_path().parent.mkdir(parents=True, exist_ok=True)
    svc._pid_path().write_text(json.dumps({"pid": 4242}))
    monkeypatch.setattr(pss, "_get_cmdline_sync", lambda pid: "chrome.exe --something")
    _ping_seq(svc, OK)
    assert await svc.start() is True  # reused as external
    assert env["killed"] == []
    assert not svc._pid_path().exists()


@pytest.mark.asyncio
async def test_pid_file_unknown_cmdline_is_not_killed(env, monkeypatch):
    svc = pss.PotServerService()
    svc._pid_path().parent.mkdir(parents=True, exist_ok=True)
    svc._pid_path().write_text("garbage{{")
    monkeypatch.setattr(pss, "_get_cmdline_sync", lambda pid: None)
    _ping_seq(svc, OK)
    assert await svc.start() is True
    assert env["killed"] == []


@pytest.mark.asyncio
async def test_watchdog_restarts_with_backoff_and_cap(env, monkeypatch):
    svc = pss.PotServerService()
    svc.state = "running"
    _ping_seq(svc, DOWN)
    monkeypatch.setattr(svc, "_terminate_process", lambda: None)
    starts = []

    async def failing_start():
        starts.append(1)
        return False

    monkeypatch.setattr(svc, "_start_locked", failing_start)
    assert await svc._check_and_heal() is True  # 1st failed ping: below threshold
    assert starts == []
    for _ in range(3):
        assert await svc._check_and_heal() is True
    assert env["sleeps"] == [1.0, 2.0, 4.0]  # exponential backoff
    assert len(starts) == 3 and svc.restart_count == 3
    assert await svc._check_and_heal() is False  # cap (3) reached
    assert svc.state == "gave_up"
    assert len(starts) == 3


@pytest.mark.asyncio
async def test_watchdog_recovery_resets_counters(env, monkeypatch):
    svc = pss.PotServerService()
    svc._consecutive_restarts = 2
    svc._ping_failures = 5
    _ping_seq(svc, OK)
    assert await svc._check_and_heal() is True
    assert svc._consecutive_restarts == 0 and svc._ping_failures == 0


@pytest.mark.asyncio
async def test_watchdog_restarts_dead_child_immediately(env, monkeypatch):
    svc = pss.PotServerService()
    svc._process = FakeProc(exit_code=1)
    _ping_seq(svc, DOWN)
    monkeypatch.setattr(svc, "_terminate_process", lambda: None)

    async def good_start():
        return True

    monkeypatch.setattr(svc, "_start_locked", good_start)
    assert await svc._check_and_heal() is True
    assert svc.restart_count == 1


@pytest.mark.asyncio
async def test_watchdog_task_cancelled_cleanly_on_stop(env, monkeypatch):
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_WATCHDOG", True)
    svc = pss.PotServerService()
    _ping_seq(svc, OK)
    gate = asyncio.Event()

    async def blocking_sleep(s):
        await gate.wait()

    monkeypatch.setattr(pss, "_sleep", blocking_sleep)
    assert await svc.start() is True
    task = svc._watchdog_task
    assert task is not None and not task.done()
    svc.stop()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert svc._watchdog_task is None


def test_log_tail_and_status_dict(env):
    svc = pss.PotServerService()
    p = svc._log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(f"line {i}" for i in range(30)))
    assert svc.log_tail(3) == ["line 27", "line 28", "line 29"]
    snap = svc.snapshot(log_lines=2)
    assert snap["log_tail"] == ["line 28", "line 29"]
    for key in ("state", "pid", "port", "uptime_seconds", "restart_count", "last_error"):
        assert key in snap


@pytest.mark.asyncio
async def test_status_reports_cookies_without_contents(env, monkeypatch):
    cookies = env["tmp"] / "cookies.txt"
    cookies.write_text("SECRET-COOKIE-VALUE")
    monkeypatch.setattr(config, "MUSIC_COOKIES_FILE", str(cookies))
    svc = pss.PotServerService()
    _ping_seq(svc, OK)
    monkeypatch.setattr(pss, "_versions", lambda: {"node_version": "v0", "ytdlp_version": "1", "bgutil_plugin_version": None})
    st = await svc.status()
    assert st["cookies"]["present"] is True and st["cookies"]["age_hours"] is not None
    assert "SECRET" not in json.dumps(st)


def test_resolve_path_relative_to_project_root(monkeypatch):
    p = pss.resolve_path(os.path.join("pot_server", "x.js"))
    assert p.is_absolute() and p.parent.parent == pss.project_root()
    assert pss.resolve_path(str(p)) == p


def test_pot_base_url(monkeypatch):
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_PORT", 1234)
    assert pss.pot_base_url() == "http://127.0.0.1:1234"
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_HOST", "::1")
    assert pss.pot_base_url() == "http://[::1]:1234"


# ---- env parsing -----------------------------------------------------------


def test_env_int_bad_input_falls_back_with_warning(monkeypatch, caplog):
    monkeypatch.setenv("X_INT", "abc")
    with caplog.at_level("WARNING"):
        assert config._env_int("X_INT", 7) == 7
    assert "X_INT" in caplog.text
    monkeypatch.setenv("X_INT", "-3")
    assert config._env_int("X_INT", 7, minimum=0) == 7
    monkeypatch.setenv("X_INT", " 12 ")
    assert config._env_int("X_INT", 7) == 12
    monkeypatch.delenv("X_INT")
    assert config._env_int("X_INT", 7) == 7
    monkeypatch.setenv("X_INT", "")
    assert config._env_int("X_INT", 7) == 7


def test_env_bool_parsing(monkeypatch, caplog):
    for raw, expected in [("true", True), ("YES", True), ("0", False), ("off", False)]:
        monkeypatch.setenv("X_BOOL", raw)
        assert config._env_bool("X_BOOL", not expected) is expected
    monkeypatch.setenv("X_BOOL", "maybe")
    with caplog.at_level("WARNING"):
        assert config._env_bool("X_BOOL", True) is True
    assert "X_BOOL" in caplog.text
