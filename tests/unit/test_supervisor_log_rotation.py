"""supervisor.py weekly log rotation (LogRotator, LogFile, move_aside, POST /rotate-logs).

Everything runs in tmp dirs: LOG_DIR is redirected, the rotator gets an explicit
root/archive/state path, alerts are recorded instead of sent. The only real
children are short `python -c` printers started by the end-to-end pump test.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import supervisor as sup

SECRET = "test-control-secret-123456"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    alerts = []
    logs = tmp_path / "logs" / "supervisor"
    logs.mkdir(parents=True)
    monkeypatch.setattr(sup, "LOG_DIR", logs)
    monkeypatch.setattr(sup, "WEBHOOK", "")
    monkeypatch.setattr(sup, "alert", lambda *a, **k: alerts.append((a, k)))
    monkeypatch.setattr(sup, "send_webhook", lambda *a, **k: (_ for _ in ()).throw(AssertionError("webhook called")))
    monkeypatch.setattr(sup, "ROTATE_RENAME_DELAY", 0.01)
    return alerts


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_rotator(tmp_path, services=(), now=datetime(2026, 10, 19, 4, 0, 5)):
    clock = Clock(now)
    rot = sup.LogRotator(
        services, root=tmp_path, log_dir=sup.LOG_DIR, archive_dir=tmp_path / "logs" / "archive",
        state_path=sup.LOG_DIR / "log_rotation.json", clock=clock,
    )
    return rot, clock


def wait_idle(rot, timeout=15.0):
    deadline = time.time() + timeout
    while rot.is_running() and time.time() < deadline:
        time.sleep(0.02)
    assert not rot.is_running()


def gunzip_text(p: Path) -> str:
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 10, 19, 4, 0, 0), datetime(2026, 10, 19, 4, 0)),    # Monday 04:00 exactly
    (datetime(2026, 10, 19, 3, 59, 59), datetime(2026, 10, 12, 4, 0)),  # just before: last week's slot
    (datetime(2026, 10, 18, 23, 0), datetime(2026, 10, 12, 4, 0)),      # Sunday
    (datetime(2026, 10, 21, 12, 0), datetime(2026, 10, 19, 4, 0)),      # Wednesday
])
def test_last_slot(now, expected):
    assert sup.last_rotation_slot(now) == expected
    assert sup.next_rotation_slot(now) == expected + timedelta(days=7)
    assert sup.last_rotation_slot(now).weekday() == 0


def test_first_start_records_baseline_without_rotating(tmp_path):
    (sup.LOG_DIR / "bot.log").write_text("old\n", encoding="utf-8")
    rot, clock = make_rotator(tmp_path, now=datetime(2026, 10, 14, 10, 0))  # Wednesday
    rot.start()
    assert rot.tick() is False
    assert (sup.LOG_DIR / "bot.log").read_text(encoding="utf-8") == "old\n"
    state = json.loads((sup.LOG_DIR / "log_rotation.json").read_text(encoding="utf-8"))
    assert state["last_slot"] == "2026-10-12T04:00:00"
    st = rot.status()
    assert st["next_run_at"].startswith("2026-10-19T04:00:00") and st["overdue"] is False
    assert "first automatic rotation Mon 2026-10-19 04:00" in (sup.LOG_DIR / "supervisor.log").read_text(encoding="utf-8")
    # A manual run still works on a first start.
    res = rot.run("manual", requested_by="cli (k)", source="cli")
    assert any(f["file"] == "bot.log" and f["status"] == "rotated" for f in res["files"])


def test_catch_up_when_overdue_and_no_double_run_after_restart(tmp_path):
    state_path = sup.LOG_DIR / "log_rotation.json"
    state_path.write_text(json.dumps({"last_slot": "2026-10-12T04:00:00"}), encoding="utf-8")
    (sup.LOG_DIR / "bot.log").write_text("week of the 12th\n", encoding="utf-8")
    # The PC was off on Monday the 19th; the supervisor comes up on Wednesday.
    rot, clock = make_rotator(tmp_path, now=datetime(2026, 10, 21, 9, 30))
    assert rot.status()["overdue"] is True
    assert rot.tick() is True
    wait_idle(rot)
    assert gunzip_text(tmp_path / "logs" / "archive" / "bot.2026-10-21.log.gz") == "week of the 12th\n"
    assert json.loads(state_path.read_text(encoding="utf-8"))["last_slot"] == "2026-10-19T04:00:00"
    assert rot.tick() is False

    # Supervisor restart: a fresh rotator reads the state and does not run again.
    (sup.LOG_DIR / "bot.log").write_text("new\n", encoding="utf-8")
    rot2, clock2 = make_rotator(tmp_path, now=datetime(2026, 10, 21, 9, 45))
    rot2.start()
    assert rot2.tick() is False and rot2.status()["next_run_at"].startswith("2026-10-26T04:00:00")
    clock2.now = datetime(2026, 10, 26, 3, 59, 59)
    assert rot2.tick() is False
    clock2.now = datetime(2026, 10, 26, 4, 0, 1)
    assert rot2.tick() is True
    wait_idle(rot2)
    assert gunzip_text(tmp_path / "logs" / "archive" / "bot.2026-10-26.log.gz") == "new\n"


# ---------------------------------------------------------------------------
# The swap: zero lines lost
# ---------------------------------------------------------------------------

def test_writer_thread_loses_no_lines_during_swap(tmp_path):
    svc = sup.Service("bot", ["never-run"], tmp_path)
    rot, _ = make_rotator(tmp_path, [svc])
    lf = svc.logfile
    lf.acquire()
    stop = threading.Event()
    written = []

    def writer():
        i = 0
        while not stop.is_set() or i < 2000:
            line = f"line {i}"
            lf.write(line + "\n")
            written.append(line)
            i += 1
            if i >= 200_000:
                break

    t = threading.Thread(target=writer)
    t.start()
    while len(written) < 500:
        time.sleep(0.001)
    res = rot.run("manual", requested_by="test", source="cli")
    n_at_rotation = len(written)
    time.sleep(0.05)
    stop.set()
    t.join(timeout=30)
    lf.release()

    bot = next(f for f in res["files"] if f["file"] == "bot.log")
    assert bot["status"] == "rotated" and bot["archive"] == "bot.2026-10-19.log.gz" and bot["bytes"] > 0
    archived = gunzip_text(tmp_path / "logs" / "archive" / "bot.2026-10-19.log.gz").splitlines()
    current = (sup.LOG_DIR / "bot.log").read_text(encoding="utf-8").splitlines()
    assert archived and current, "the swap must have happened mid-stream"
    assert len(written) > n_at_rotation
    assert archived + current == written
    assert not list((tmp_path / "logs" / "archive").glob("*.log"))  # uncompressed copy removed
    assert not list((tmp_path / "logs" / "archive").glob("*.tmp"))


def test_real_child_output_through_pump_survives_rotation(tmp_path):
    code = "import time\nfor i in range(4000):\n    print(f'out {i}', flush=True)\n    time.sleep(0.0003)\n"
    svc = sup.Service("dashboard", [sys.executable, "-c", code], tmp_path)
    rot, _ = make_rotator(tmp_path, [svc])
    svc.start()
    try:
        deadline = time.time() + 20
        while len(svc.tail) < 150 and time.time() < deadline:  # tail keeps at most TAIL_KEEP (200)
            time.sleep(0.01)
        rot.run("manual", requested_by="test", source="cli")
        svc.proc.wait(timeout=60)
        deadline = time.time() + 10
        while svc.logfile._f is not None and time.time() < deadline:  # pump released the handle at EOF
            time.sleep(0.02)
    finally:
        if svc.proc and svc.proc.poll() is None:
            svc.proc.kill()
    assert svc.logfile._f is None
    archived = gunzip_text(tmp_path / "logs" / "archive" / "dashboard.2026-10-19.log.gz")
    current = (sup.LOG_DIR / "dashboard.log").read_text(encoding="utf-8")
    lines = [ln for ln in (archived + current).splitlines() if ln.startswith("out ")]
    assert lines == [f"out {i}" for i in range(4000)]
    assert "===== start" in archived and current.strip()

    # Child down at rotation time: no handle open, the file is simply moved; next start makes a fresh one.
    rot.run("manual", requested_by="test", source="cli")
    assert not (sup.LOG_DIR / "dashboard.log").exists()
    assert (tmp_path / "logs" / "archive" / "dashboard.2026-10-19.1.log.gz").exists()


def test_supervisor_log_rotated_and_new_lines_go_to_fresh_file(tmp_path):
    sup.slog("before rotation")
    rot, _ = make_rotator(tmp_path)
    rot.run("manual", requested_by="test", source="cli")
    archived = gunzip_text(tmp_path / "logs" / "archive" / "supervisor.2026-10-19.log.gz")
    assert "before rotation" in archived
    fresh = (sup.LOG_DIR / "supervisor.log").read_text(encoding="utf-8")
    assert "before rotation" not in fresh and "log rotation: done" in fresh
    assert "supervisor.log -> logs/archive/supervisor.2026-10-19.log.gz" in fresh


def test_empty_and_missing_files_are_skipped(tmp_path):
    (sup.LOG_DIR / "tunnel.log").write_text("", encoding="utf-8")
    rot, _ = make_rotator(tmp_path)
    res = rot.run("manual", requested_by="test", source="cli")
    by = {f["file"]: f["status"] for f in res["files"]}
    assert by["tunnel.log"] == "empty" and by["bot.log"] == "missing" and by["bot_startup.log"] == "missing"
    assert (sup.LOG_DIR / "tunnel.log").exists()
    assert not list((tmp_path / "logs" / "archive").glob("tunnel*"))


# ---------------------------------------------------------------------------
# Retention and files that must never be touched
# ---------------------------------------------------------------------------

def test_retention_deletes_only_old_known_archives(tmp_path):
    arch = tmp_path / "logs" / "archive"
    arch.mkdir(parents=True)
    today = date(2026, 10, 19)
    keep = [f"bot.{today - timedelta(days=27)}.log.gz", f"supervisor.{today - timedelta(days=7)}.log.gz",
            f"bot_startup.{today}.log.gz", "kicks.2020-01-01.log.gz", "notes.txt", "bot.2020-01-01.log.gz.tmp"]
    old = [f"bot.{today - timedelta(days=28)}.log.gz", f"tunnel.{today - timedelta(days=40)}.2.log.gz",
           "dashboard.2020-01-01.log.gz"]
    for n in keep + old:
        (arch / n).write_bytes(b"x")
    rot, _ = make_rotator(tmp_path)
    assert rot.prune(today) == sorted(old)
    assert sorted(p.name for p in arch.iterdir()) == sorted(keep)


def test_kicks_log_pid_and_lock_files_never_touched(tmp_path):
    protected = {
        tmp_path / "kicks.log": "TOTAL_KICKS:42\nsomeone kicked\n",
        sup.LOG_DIR / "kicks.log": "TOTAL_KICKS:1\n",
        sup.LOG_DIR / "supervisor.lock": "1234 2026-10-19\n",
        tmp_path / "logs" / "pot_server.pid": "{\"pid\": 5}",
        tmp_path / "logs" / "pot_server.log": "pot output\n",
        tmp_path / "bot.db": "sqlite",
        tmp_path / "cookies.txt": "cookie",
    }
    for p, text in protected.items():
        p.write_text(text, encoding="utf-8")
    before = {p: p.stat().st_mtime_ns for p in protected}
    (sup.LOG_DIR / "bot.log").write_text("bot\n", encoding="utf-8")
    (tmp_path / "bot_startup.log").write_text("startup\n", encoding="utf-8")
    rot, _ = make_rotator(tmp_path)
    rot.run("manual", requested_by="test", source="cli")
    for p, text in protected.items():
        assert p.read_text(encoding="utf-8") == text and p.stat().st_mtime_ns == before[p], p
    names = sorted(p.name for p in (tmp_path / "logs" / "archive").iterdir())
    assert names == ["bot.2026-10-19.log.gz", "bot_startup.2026-10-19.log.gz", "supervisor.2026-10-19.log.gz"]


# ---------------------------------------------------------------------------
# Sharing violations (Windows) and deferral of bot_startup.log
# ---------------------------------------------------------------------------

def test_rename_retries_on_permission_error(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.log", tmp_path / "b.log"
    src.write_text("data\n", encoding="utf-8")
    real, calls = os.rename, []

    def flaky(a, b):
        calls.append(a)
        if len(calls) < 3:
            raise PermissionError(32, "being used by another process")
        return real(a, b)

    monkeypatch.setattr(sup.os, "rename", flaky)
    lock = threading.Lock()
    assert sup.move_aside(src, dst, lock) is True
    assert len(calls) == 3 and dst.read_text(encoding="utf-8") == "data\n" and not lock.locked()
    assert sup.move_aside(tmp_path / "missing.log", tmp_path / "c.log") is False


def test_rename_gives_up_and_leaves_file(tmp_path, monkeypatch, _isolate):
    (sup.LOG_DIR / "tunnel.log").write_text("keep me\n", encoding="utf-8")

    def always(a, b):
        raise PermissionError(32, "being used by another process")

    monkeypatch.setattr(sup.os, "rename", always)
    rot, _ = make_rotator(tmp_path)
    res = rot.run("manual", requested_by="test", source="cli")
    tunnel = next(f for f in res["files"] if f["file"] == "tunnel.log")
    assert tunnel["status"] == "failed" and res["ok"] is False
    assert (sup.LOG_DIR / "tunnel.log").read_text(encoding="utf-8") == "keep me\n"
    (title, desc, color, ping), _ = _isolate[-1]
    assert title == "Log rotation problem" and "tunnel.log" in desc and ping is False


def test_bot_startup_deferred_while_held_then_rotated_on_bot_start(tmp_path, monkeypatch):
    startup = tmp_path / "bot_startup.log"
    startup.write_text("held line\n", encoding="utf-8")
    held = {"on": True}
    real = os.rename

    def rename(a, b):
        if held["on"] and Path(a).name == "bot_startup.log":
            raise PermissionError(32, "being used by another process")  # the bot's logging.FileHandler
        return real(a, b)

    monkeypatch.setattr(sup.os, "rename", rename)
    rot, clock = make_rotator(tmp_path)
    res = rot.run("schedule")
    st = next(f for f in res["files"] if f["file"] == "bot_startup.log")
    assert st["status"] == "deferred" and res["ok"] is True  # deferral is expected, not a failure
    assert startup.read_text(encoding="utf-8") == "held line\n"
    assert rot.status()["deferred"] == ["bot_startup"]
    assert json.loads((sup.LOG_DIR / "log_rotation.json").read_text(encoding="utf-8"))["deferred"] == ["bot_startup"]

    # Other services' starts do not touch it; a restarted supervisor remembers the deferral.
    assert rot.before_start("dashboard") == []
    rot2, clock2 = make_rotator(tmp_path, now=datetime(2026, 10, 20, 12, 0))
    held["on"] = False                       # the bot exited: nothing holds the file
    out = rot2.before_start("bot")
    assert [r["status"] for r in out] == ["rotated"]
    deadline = time.time() + 10
    gz = tmp_path / "logs" / "archive" / "bot_startup.2026-10-20.log.gz"
    while not gz.exists() and time.time() < deadline:
        time.sleep(0.02)
    assert gunzip_text(gz) == "held line\n"
    assert not startup.exists() and rot2.status()["deferred"] == []


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing violation semantics")
def test_real_open_handle_blocks_rename_and_defers(tmp_path):
    startup = tmp_path / "bot_startup.log"
    f = open(startup, "a", encoding="utf-8")  # like logging.FileHandler: no FILE_SHARE_DELETE
    try:
        f.write("line\n")
        f.flush()
        rot, _ = make_rotator(tmp_path)
        res = rot.run("manual", requested_by="test", source="cli")
        assert next(x for x in res["files"] if x["file"] == "bot_startup.log")["status"] == "deferred"
        f.write("still writing\n")
        f.flush()
    finally:
        f.close()
    assert startup.read_text(encoding="utf-8") == "line\nstill writing\n"


def test_service_start_runs_before_start_hook(tmp_path):
    calls = []
    svc = sup.Service("tunnel", [sys.executable, "-c", "print('hi')"], tmp_path)
    svc.before_start = lambda: calls.append("hook") or (_ for _ in ()).throw(RuntimeError("boom"))
    svc.start()  # a failing hook is logged, the start goes on
    try:
        assert calls == ["hook"] and svc.proc is not None
        svc.proc.wait(timeout=20)
    finally:
        if svc.proc and svc.proc.poll() is None:
            svc.proc.kill()
    assert "deferred log rotation failed: RuntimeError: boom" in (sup.LOG_DIR / "supervisor.log").read_text(encoding="utf-8")


def test_gzip_failure_keeps_uncompressed_then_next_run_recovers(tmp_path, monkeypatch, _isolate):
    (sup.LOG_DIR / "bot.log").write_text("precious\n", encoding="utf-8")
    rot, _ = make_rotator(tmp_path)
    real = sup.LogRotator._compress
    monkeypatch.setattr(sup.LogRotator, "_compress", lambda self, p: (_ for _ in ()).throw(OSError("disk full")))
    res = rot.run("manual", requested_by="test", source="cli")
    assert next(f for f in res["files"] if f["file"] == "bot.log")["status"] == "compress_failed"
    pending = tmp_path / "logs" / "archive" / "bot.2026-10-19.log"
    assert pending.read_text(encoding="utf-8") == "precious\n"
    assert _isolate and _isolate[-1][0][0] == "Log rotation problem"
    monkeypatch.setattr(sup.LogRotator, "_compress", real)
    rot.run("manual", requested_by="test", source="cli")
    assert not pending.exists() and gunzip_text(pending.with_name(pending.name + ".gz")) == "precious\n"


# ---------------------------------------------------------------------------
# Control API: POST /rotate-logs queues, the main loop applies it
# ---------------------------------------------------------------------------

@pytest.fixture
def stack(tmp_path):
    rot, clock = make_rotator(tmp_path)
    ctrl = sup.Controller([], cooldown=30, secret=SECRET, rotator=rot)
    server = sup.ControlServer(ctrl, SECRET, port=0)
    assert server.start()
    yield {"rot": rot, "ctrl": ctrl, "server": server, "tmp": tmp_path}
    server.stop()
    wait_idle(rot)


def call(server, method, path, body=None, token=SECRET):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


BODY = {"requested_by": "keira (123)", "source": "dashboard", "reason": "big\nlogs"}


def test_rotate_logs_endpoint_queues_and_main_loop_applies(stack):
    (sup.LOG_DIR / "bot.log").write_text("x\n", encoding="utf-8")
    srv, ctrl, rot = stack["server"], stack["ctrl"], stack["rot"]
    assert call(srv, "POST", "/rotate-logs", BODY, token=None)[0] == 401
    assert call(srv, "GET", "/rotate-logs")[0] == 405
    assert call(srv, "POST", "/rotate-logs", {"source": "dashboard"})[1]["error"] == "invalid_requested_by"

    code, body = call(srv, "POST", "/rotate-logs", BODY)
    assert code == 202 and body["queued"] is True
    # Only queued: nothing moved yet.
    assert (sup.LOG_DIR / "bot.log").exists() and not rot.is_running()
    assert call(srv, "GET", "/status")[1]["log_rotation"]["pending"] is True
    assert call(srv, "POST", "/rotate-logs", BODY)[1]["error"] == "rotation_in_progress"

    assert ctrl.drain(time.time()) == ["log-rotation"]
    wait_idle(rot)
    assert not (sup.LOG_DIR / "bot.log").exists()
    _, status = call(srv, "GET", "/status")
    lr = status["log_rotation"]
    assert lr["pending"] is False and lr["running"] is False and lr["retention_days"] == 28
    assert lr["archive_dir"] == "logs/archive" and lr["next_run_at"]
    last = lr["last_run"]
    assert last["trigger"] == "manual" and last["requested_by"] == "keira (123)" and last["reason"] == "big logs"
    assert last["ok"] is True and {"file": "bot.log", "status": "rotated"}.items() <= next(
        f for f in last["files"] if f["file"] == "bot.log").items()

    code, body = call(srv, "POST", "/rotate-logs", BODY)
    assert code == 429 and body["error"] == "cooldown"


def test_status_without_rotator_is_null():
    ctrl = sup.Controller([], secret=SECRET)
    assert ctrl.status()["log_rotation"] is None
    assert ctrl.submit_rotate(BODY)[0] == 409


def test_cli_rotate_logs(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(sup, "_cli_call", lambda m, p, b=None: seen.append((m, p, b)) or (202, {"queued": True}))
    assert sup.cli(["rotate-logs", "too", "big"]) == 0
    method, path, body = seen[0]
    assert (method, path) == ("POST", "/rotate-logs") and body["source"] == "cli" and body["reason"] == "too big"


async def test_dashboard_client_rotate_logs_roundtrip(stack, monkeypatch):
    """server/services/supervisor_client.rotate_logs against the real control server."""
    from server import config as dash_config
    from server.services import supervisor_client as dash_client

    monkeypatch.setattr(dash_config, "SUPERVISOR_BASE_URL", f"http://127.0.0.1:{stack['server'].port}")
    monkeypatch.setattr(dash_config, "SUPERVISOR_CONTROL_SECRET", SECRET)
    res = await dash_client.rotate_logs(requested_by="keira (1)", reason="now")
    assert res["queued"] is True
    with pytest.raises(dash_client.SupervisorError) as exc:
        await dash_client.rotate_logs(requested_by="keira (1)")
    assert exc.value.status_code == 409 and exc.value.body["error"] == "rotation_in_progress"
    stack["ctrl"].drain(time.time())
