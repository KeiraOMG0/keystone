"""supervisor.py control API, driven in-process.

Never runs main() for real, never launches cloudflared/the bot/the dashboard,
never binds 8766/8080/4416/8767 (the control server uses an ephemeral port),
never posts to the webhook (alert is stubbed). The only real child is a
`python -c "time.sleep(30)"` started and killed by the kill_tree test.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import supervisor as sup

SECRET = "test-control-secret-123456"
ROOT = Path(sup.__file__).resolve().parent


class FakeProc:
    _next_pid = 40000

    def __init__(self):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.code = None

    def poll(self):
        return self.code


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Logs to tmp, webhook off, alerts recorded instead of sent."""
    alerts = []
    monkeypatch.setattr(sup, "LOG_DIR", tmp_path)
    monkeypatch.setattr(sup, "WEBHOOK", "")
    monkeypatch.setattr(sup, "alert", lambda *a, **k: alerts.append((a, k)))
    monkeypatch.setattr(sup, "send_webhook", lambda *a, **k: (_ for _ in ()).throw(AssertionError("webhook called")))
    return alerts


def make_service(name, *, running=True, port=None, needs=None):
    s = sup.Service(name, ["never-run"], ROOT, port=port, needs=needs)
    s.kills = 0

    def kill_tree():
        s.kills += 1
        if s.proc is not None:
            s.proc.code = 1

    s.kill_tree = kill_tree
    if running:
        s.proc = FakeProc()
        s.started_at = time.time() - 50
        s.starts = 1
    return s


@pytest.fixture
def stack():
    services = [make_service("bot"), make_service("dashboard"), make_service("tunnel")]
    ctrl = sup.Controller(services, cooldown=30, secret=SECRET)
    server = sup.ControlServer(ctrl, SECRET, port=0)
    assert server.start()
    yield {"services": {s.name: s for s in services}, "ctrl": ctrl, "server": server}
    server.stop()


def call(server, method, path, body=None, *, token=SECRET, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {"Content-Type": ctype}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def restart_body(target="bot", **kw):
    return {"target": target, "requested_by": "Mod (123456789012345678)", "source": "dashboard", **kw}


# ---------------------------------------------------------------------------
# Import is side-effect light
# ---------------------------------------------------------------------------

def test_import_has_no_side_effects():
    code = (
        "import threading, supervisor; "
        "assert callable(supervisor.main); "
        "print(threading.active_count())"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1"  # no threads, no server, no children


def test_binds_loopback_only(stack):
    assert stack["server"].httpd.server_address[0] == "127.0.0.1"


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def test_auth_required(stack):
    srv = stack["server"]
    assert call(srv, "GET", "/status", token=None)[0] == 401
    assert call(srv, "GET", "/status", token="wrong")[0] == 401
    assert call(srv, "GET", "/nope", token=None)[0] == 401  # auth before routing


def test_unknown_path_and_method(stack):
    srv = stack["server"]
    assert call(srv, "GET", "/nope")[0] == 404
    code, _, headers = call(srv, "GET", "/restart")
    assert code == 405 and headers.get("Allow") == "POST"
    assert call(srv, "POST", "/status", {"x": 1})[0] == 405


def test_body_limits(stack):
    srv = stack["server"]
    assert call(srv, "POST", "/restart", raw=b"x" * (sup.MAX_BODY + 1))[0] == 413
    assert call(srv, "POST", "/restart", raw=b"target=bot", ctype="application/x-www-form-urlencoded")[0] == 415
    assert call(srv, "POST", "/restart", raw=b"{not json")[0] == 400
    assert call(srv, "POST", "/restart", raw=b"[1,2]")[0] == 400


def test_status_shape(stack):
    stack["services"]["dashboard"].last_exit = {"reason": "exit code 1", "at": "2026-10-13T00:00:00+00:00"}
    stack["ctrl"].publish()
    code, body, _ = call(stack["server"], "GET", "/status")
    assert code == 200
    assert body["supervisor"]["control"] is True and body["supervisor"]["pid"] == os.getpid()
    assert body["supervisor"]["version"] == sup.VERSION
    names = [s["name"] for s in body["services"]]
    assert names == ["bot", "dashboard", "tunnel"]
    bot = body["services"][0]
    for key in ("running", "pid", "uptime_s", "restarts", "last_exit", "last_request", "health_fails",
                "cooldown_remaining_s", "waiting_on_dependency"):
        assert key in bot
    assert bot["running"] is True and bot["uptime_s"] >= 49
    assert body["services"][1]["last_exit"]["reason"] == "exit code 1"


def test_invalid_requests(stack):
    srv = stack["server"]
    assert call(srv, "POST", "/restart", restart_body("everything"))[1]["error"] == "invalid_target"
    assert call(srv, "POST", "/restart", restart_body(requested_by=""))[1]["error"] == "invalid_requested_by"
    assert call(srv, "POST", "/restart", restart_body(source="web"))[1]["error"] == "invalid_source"
    assert call(srv, "POST", "/restart", restart_body(reason=5))[1]["error"] == "invalid_reason"
    assert call(srv, "POST", "/restart", restart_body(delay_s="soon"))[1]["error"] == "invalid_delay"
    assert stack["ctrl"].requests.empty()


# ---------------------------------------------------------------------------
# Restart semantics
# ---------------------------------------------------------------------------

def test_restart_is_queued_then_applied_by_main_loop(stack, _isolate):
    bot = stack["services"]["bot"]
    bot.backoff, bot.quick_crashes = 12.0, 2
    code, body, _ = call(stack["server"], "POST", "/restart", restart_body("bot", reason="stuck", delay_s=1.5))
    assert code == 202 and body["accepted"] == ["bot"] and body["delay_s"] == 1.5 and body["eta_s"] > 1.5
    # The HTTP thread only queued it.
    assert bot.kills == 0 and bot.proc.code is None
    ctrl = stack["ctrl"]
    assert ctrl.drain(time.time()) == []          # not before its delay
    assert bot.kills == 0
    assert ctrl.drain(time.time() + 2) == ["bot"]  # main loop applies it
    assert bot.kills == 1 and bot.requested is not None
    assert bot.last_request["by"] == "Mod (123456789012345678)" and bot.last_request["reason"] == "stuck"

    before = time.time()
    bot.poll(before)  # sees the exit
    assert bot.proc is None
    assert bot.backoff == 12.0 and bot.quick_crashes == 2          # no backoff growth, no crash count
    assert before + 0.5 < bot.next_start <= time.time() + sup.REQUESTED_RESTART_DELAY + 0.1
    assert "restart requested by Mod (123456789012345678) via dashboard: stuck" in bot.last_exit["reason"]
    (title, desc, color, ping), _ = _isolate[-1]
    assert color == sup.INFO_COLOR and ping is False and "requested" in title


def test_crash_still_uses_backoff_and_red_alert(_isolate):
    s = make_service("bot")
    s.backoff = 6.0
    s.proc.code = 1
    s.poll(time.time())
    assert s.backoff == 12.0 and s.quick_crashes == 1
    (_, _, color, ping), _ = _isolate[-1]
    assert color == 0xE74C3C and ping is True


def test_cooldown_returns_429(stack):
    srv = stack["server"]
    assert call(srv, "POST", "/restart", restart_body("tunnel"))[0] == 202
    code, body, headers = call(srv, "POST", "/restart", restart_body("tunnel"))
    assert code == 429 and body["error"] == "cooldown" and 1 <= body["retry_after_s"] <= 30
    assert headers.get("Retry-After") == str(body["retry_after_s"])
    # "all" includes the cooling-down tunnel -> refused as a whole
    assert call(srv, "POST", "/restart", restart_body("all"))[0] == 429
    status = call(srv, "GET", "/status")[1]
    tunnel = next(s for s in status["services"] if s["name"] == "tunnel")
    assert tunnel["cooldown_remaining_s"] > 0 and tunnel["restart_pending"] is True


def test_restart_all_and_in_progress(stack):
    stack["ctrl"].cooldown = 0
    srv = stack["server"]
    code, body, _ = call(srv, "POST", "/restart", restart_body("all", delay_s=99))
    assert code == 202 and body["accepted"] == ["bot", "dashboard", "tunnel"] and body["delay_s"] == sup.MAX_REQUEST_DELAY
    code, body, _ = call(srv, "POST", "/restart", restart_body("bot"))
    assert code == 409 and body["error"] == "restart_in_progress"
    applied = stack["ctrl"].drain(time.time() + 11)
    assert sorted(applied) == ["bot", "dashboard", "tunnel"]
    assert all(s.kills == 1 for s in stack["services"].values())


def test_negative_delay_clamped_to_zero(stack):
    code, body, _ = call(stack["server"], "POST", "/restart", restart_body("tunnel", delay_s=-4))
    assert code == 202 and body["delay_s"] == 0.0
    assert stack["ctrl"].drain(time.time() + 0.01) == ["tunnel"]


def test_port_taken_by_external_process_is_409(stack):
    dash = stack["services"]["dashboard"]
    dash.proc, dash.skip_reason = None, "port in use"
    dash.port = 8080
    stack["ctrl"].publish()
    code, body, _ = call(stack["server"], "POST", "/restart", restart_body("dashboard"))
    assert code == 409 and body["error"] == "not_managed" and "8080" in body["message"]
    code, body, _ = call(stack["server"], "POST", "/restart", restart_body("all"))
    assert code == 202 and body["accepted"] == ["bot", "tunnel"]
    assert [s["name"] for s in body["skipped"]] == ["dashboard"]


def test_request_for_service_in_backoff_starts_it_sooner(_isolate):
    s = make_service("tunnel", running=False)
    s.next_start, s.backoff, s.quick_crashes = time.time() + 100, 64.0, 4
    ctrl = sup.Controller([s], cooldown=30, secret=SECRET)
    code, _ = ctrl.submit(restart_body("tunnel", delay_s=0))
    assert code == 202
    now = time.time()
    assert ctrl.drain(now + 0.01) == ["tunnel"]
    assert s.next_start <= now + 0.01 and s.backoff == sup.BACKOFF_START and s.quick_crashes == 0
    assert s.kills == 0
    (_, _, color, ping), _ = _isolate[-1]
    assert color == sup.INFO_COLOR and ping is False


def test_waiting_on_dependency_is_reported():
    s = make_service("dashboard", running=False, needs=lambda: False)
    s.poll(time.time())
    ctrl = sup.Controller([s], secret=SECRET)
    svc = ctrl.status()["services"][0]
    assert svc["waiting_on_dependency"] is True and svc["running"] is False


# ---------------------------------------------------------------------------
# /logs
# ---------------------------------------------------------------------------

def test_logs_tail_is_capped_and_scrubbed(stack, monkeypatch):
    monkeypatch.setenv("SOME_API_TOKEN", "planted-secret-value-xyz")
    bot = stack["services"]["bot"]
    for i in range(250):
        bot.tail.append(f"line {i}")
    bot.tail.append("token=planted-secret-value-xyz and Bearer abcdefghijklmnop")
    bot.tail.append("https://discord.com/api/webhooks/123/SECRETWEBHOOKTOKEN")
    code, body, _ = call(stack["server"], "GET", "/logs?service=bot&lines=500")
    assert code == 200 and len(body["lines"]) == sup.MAX_LOG_LINES
    text = "\n".join(body["lines"])
    assert "planted-secret-value-xyz" not in text and "abcdefghijklmnop" not in text
    assert "SECRETWEBHOOKTOKEN" not in text and SECRET not in text
    assert call(stack["server"], "GET", "/logs?service=nope")[0] == 404


# ---------------------------------------------------------------------------
# Startup pieces
# ---------------------------------------------------------------------------

def test_no_secret_means_no_control_server():
    ctrl = sup.Controller([make_service("bot")], secret="")
    server = sup.ControlServer(ctrl, "", port=0)
    assert server.start() is False and server.httpd is None


def test_single_instance_lock(tmp_path):
    path = tmp_path / "supervisor.lock"
    first, holder = sup.acquire_instance_lock(path)
    assert first is not None and holder is None
    try:
        second, holder = sup.acquire_instance_lock(path)
        assert second is None and holder == str(os.getpid())
    finally:
        sup.release_instance_lock(first)
    again, _ = sup.acquire_instance_lock(path)
    assert again is not None
    sup.release_instance_lock(again)


def test_main_exits_without_starting_anything_when_lock_held(monkeypatch, tmp_path):
    monkeypatch.setattr(sup, "acquire_instance_lock", lambda path=None: (None, "4242"))

    def boom(*a, **k):
        raise AssertionError("must not start anything")

    monkeypatch.setattr(sup.Service, "start", boom)
    monkeypatch.setattr(sup.ControlServer, "start", boom)
    assert sup.main() != 0
    log = (tmp_path / "supervisor.log").read_text(encoding="utf-8")
    assert "another supervisor is already running (pid 4242)" in log


def test_kill_tree_on_real_child_then_requested_restart(tmp_path):
    s = sup.Service("sleeper", [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path)
    s.start()
    try:
        assert s.proc is not None and s.proc.poll() is None
        ctrl = sup.Controller([s], secret=SECRET)
        # Controller only knows bot/dashboard/tunnel targets; apply directly like drain() does.
        s.request_restart({"requested_by": "test", "source": "cli", "reason": None}, time.time())
        deadline = time.time() + 15
        while s.proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        assert s.proc.poll() is not None
        s.poll(time.time())
        assert s.proc is None and "restart requested by test via cli" in s.last_exit["reason"]
        ctrl.publish()
        assert ctrl.status()["services"][0]["running"] is False
    finally:
        if s.proc is not None:
            s.proc.kill()
            s.proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------

def test_logs_scrub_appeal_tokens_oauth_codes_and_ips(stack):
    dash = stack["services"]["dashboard"]
    dash.tail.append('INFO:     24.112.160.150:0 - "GET /api/public/appeals/AbCdEf123_-xyzAbCdEf123_-xyzAbCd HTTP/1.1" 200 OK')
    dash.tail.append('INFO:     24.112.160.151:0 - "GET /appeal/QWERTYtoken987 HTTP/1.1" 200 OK')
    dash.tail.append('INFO:     10.0.0.7:5555 - "GET /api/auth/callback?code=VCcxAkSECRETCODE&state=STATEVALUE42 HTTP/1.1" 307')
    dash.tail.append('INFO:     127.0.0.1:61000 - "GET /api/status HTTP/1.1" 200 OK')
    code, body, _ = call(stack["server"], "GET", "/logs?service=dashboard&lines=10")
    text = "\n".join(body["lines"])
    for leaked in ("AbCdEf123_-xyz", "QWERTYtoken987", "VCcxAkSECRETCODE", "STATEVALUE42",
                   "24.112.160.150", "24.112.160.151", "10.0.0.7"):
        assert leaked not in text, leaked
    assert "/api/public/appeals/<redacted>" in text and "code=<redacted>&state=<redacted>" in text
    assert "127.0.0.1:61000" in text  # loopback kept (useful, not PII)


def test_reason_and_requester_cannot_forge_log_lines(stack, tmp_path):
    forged = "ok\r\n[2026-01-01 00:00:00] control: accepted restart of bot (requested by Owner"
    code, body, _ = call(stack["server"], "POST", "/restart",
                         restart_body("tunnel", reason=forged, requested_by="Mod\n(1)\x00"))
    assert code == 202
    stack["ctrl"].drain(time.time() + 5)
    tunnel = stack["services"]["tunnel"]
    assert "\n" not in tunnel.last_request["reason"] and "\r" not in tunnel.last_request["reason"]
    assert tunnel.last_request["by"] == "Mod (1)"
    log = (tmp_path / "supervisor.log").read_text(encoding="utf-8").splitlines()
    assert not any(line.startswith("[2026-01-01 00:00:00]") for line in log)


def test_request_for_already_exited_process_stays_a_crash(_isolate):
    s = make_service("bot")
    s.proc.code = 3  # crashed; the main loop has not seen it yet
    ctrl = sup.Controller([s], cooldown=0, secret=SECRET)
    assert ctrl.submit(restart_body("bot", delay_s=0))[0] == 202
    ctrl.drain(time.time() + 0.01)
    assert s.requested is None and s.kills == 0
    s.backoff = 6.0
    s.poll(time.time())
    assert s.last_exit["reason"] == "exit code 3" and s.backoff == 12.0 and s.quick_crashes == 1
    (_, _, color, ping), _ = _isolate[-1]
    assert color == 0xE74C3C and ping is True


def test_requested_kill_that_does_nothing_is_retried_then_dropped(_isolate):
    s = make_service("bot")
    s.kill_tree = lambda: setattr(s, "kills", s.kills + 1)  # the kill never takes effect
    ctrl = sup.Controller([s], cooldown=0, secret=SECRET)
    assert ctrl.submit(restart_body("bot", delay_s=0))[0] == 202
    t0 = time.time() + 0.01
    ctrl.drain(t0)
    assert s.kills == 1 and s.requested is not None
    s.poll(t0 + 5)
    assert s.kills == 1                                    # still within the window
    s.poll(t0 + sup.KILL_CONFIRM_AFTER + 0.1)
    assert s.kills == 2 and s.requested is not None       # retried once
    s.poll(t0 + 2 * sup.KILL_CONFIRM_AFTER + 0.2)
    assert s.kills == 2 and s.requested is None           # given up
    ctrl.publish()
    assert ctrl.status()["services"][0]["restart_pending"] is False
    assert ctrl.submit(restart_body("bot", delay_s=0))[0] == 202  # not stuck on restart_in_progress
    (title, _, _, ping), _ = _isolate[-1]
    assert "failed" in title and ping is False


def test_waiting_dashboard_request_wording(_isolate):
    s = make_service("dashboard", running=False, needs=lambda: False)
    s.poll(time.time())
    assert s.waiting is True
    s.request_restart({"requested_by": "x", "source": "cli", "reason": None}, time.time())
    (_, desc, _, _), _ = _isolate[-1]
    assert "as soon as the bot's internal API answers" in desc and "starting it now" not in desc


def test_child_env_rereads_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ROTATED_TOKEN=new-value\nREAL_ENV_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setattr(sup, "_BASE_ENV", {"REAL_ENV_KEY": "from-real-env", "PATH": "x"})
    env = sup.child_env(env_file)
    assert env["ROTATED_TOKEN"] == "new-value"          # fresh .env value
    assert env["REAL_ENV_KEY"] == "from-real-env"       # real env wins, like load_dotenv()
    assert env["PYTHONUNBUFFERED"] == "1"
    monkeypatch.setattr(sup, "_ENV_AT_IMPORT", {"ROTATED_TOKEN": "startup-value"})
    assert sup.child_env(tmp_path / "missing.env")["ROTATED_TOKEN"] == "startup-value"


def test_slow_client_is_cut_off_by_deadline(stack):
    httpd = stack["server"].httpd
    httpd.request_deadline = 1.0
    sock = socket.create_connection(("127.0.0.1", stack["server"].port), timeout=10)
    try:
        start = time.time()
        sock.sendall(b"GET /status HTTP/1.0\r\n")
        closed = False
        while time.time() - start < 8:
            try:
                sock.sendall(b"X")       # dribble a header byte every 0.3 s
            except OSError:
                closed = True
                break
            sock.settimeout(0.3)
            try:
                if sock.recv(1024) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
        assert closed and time.time() - start < 5
    finally:
        sock.close()


def test_connection_cap(monkeypatch):
    monkeypatch.setattr(sup._HTTPServer, "max_connections", 2)
    ctrl = sup.Controller([make_service("bot")], secret=SECRET)
    server = sup.ControlServer(ctrl, SECRET, port=0)
    assert server.start()
    held = []
    try:
        for _ in range(2):
            held.append(socket.create_connection(("127.0.0.1", server.port), timeout=5))
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        try:
            extra.settimeout(3)
            try:
                data = extra.recv(1024)
            except OSError:
                data = b""
            assert data == b""  # closed at once, no thread spawned for it
        finally:
            extra.close()
        for h in held:
            h.close()
        held.clear()
        deadline = time.time() + 5
        code = None
        while time.time() < deadline:
            try:
                code = call(server, "GET", "/status")[0]
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.2)
        assert code == 200  # slots are released again
    finally:
        for h in held:
            h.close()
        server.stop()
