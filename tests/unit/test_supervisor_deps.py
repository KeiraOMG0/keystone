"""supervisor.py dependency updates (DependencyUpdater, POST /update-deps, Service.hold).

Never runs pip, npm or pytest for real: every command goes through a fake
runner that records it and plays back canned output. Projects and
requirements files live in tmp_path; services are fakes whose kill_tree ends
the fake process and whose start() is recorded instead of launching anything.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

import supervisor as sup

SECRET = "test-control-secret-123456"
ROOT = Path(sup.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(sup, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(sup, "WEBHOOK", "")
    monkeypatch.setattr(sup, "alert", lambda *a, **k: alerts.append((a, k)))
    return alerts


class FakeProc:
    _next = 50000

    def __init__(self):
        FakeProc._next += 1
        self.pid = FakeProc._next
        self.code = None

    def poll(self):
        return self.code


def fake_service(name, *, running=True):
    s = sup.Service(name, ["never-run"], ROOT)
    s.kills, s.started = 0, 0

    def kill_tree():
        s.kills += 1
        if s.proc is not None:
            s.proc.code = 1
        return True

    def start():
        s.started += 1

    s.kill_tree, s.start = kill_tree, start
    if running:
        s.proc = FakeProc()
        s.started_at = time.time() - 300
        s.starts = 1
    return s


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

PIP_AUDIT = json.dumps({"dependencies": [
    {"name": "aiohttp", "version": "3.14.5", "vulns": [
        {"id": "GHSA-aaaa", "fix_versions": ["3.14.6"]}, {"id": "PYSEC-1", "fix_versions": ["3.14.10", "3.14.9"]}]},
    {"name": "Brotli", "version": "1.2.1", "vulns": [{"id": "GHSA-bbbb", "fix_versions": []}]},
    {"name": "Ignored_Pkg", "version": "1.0", "vulns": [{"id": "X", "fix_versions": ["2.0"]}]},
    {"name": "clean", "version": "1.0", "vulns": []},
]})

NPM_AUDIT = json.dumps({"vulnerabilities": {
    "minimatch": {"severity": "moderate", "fixAvailable": True},
    "vite": {"severity": "high", "fixAvailable": {"name": "vite", "version": "8.1.2", "isSemVerMajor": False}},
    "shadcn": {"severity": "moderate", "fixAvailable": {"name": "shadcn", "version": "1.0.0", "isSemVerMajor": True}},
    "ts-morph": {"severity": "critical", "fixAvailable": False},
    "deepmerge": {"severity": "low", "fixAvailable": True},
    "lodash.merge": {"severity": "high", "fixAvailable": True},
}})


def test_plan_python_picks_highest_fix_and_lists_no_fix():
    ups, nofix = sup.plan_python(PIP_AUDIT, {"ignored-pkg"})
    assert ups == [{"package": "aiohttp", "from": "3.14.5", "to": "3.14.10", "why": "GHSA-aaaa, PYSEC-1"}]
    assert nofix == [{"package": "Brotli", "version": "1.2.1", "ids": ["GHSA-bbbb"]}]
    with pytest.raises(ValueError):
        sup.plan_python("not json")


def test_ytdlp_outdated_is_merged_into_the_upgrades():
    raw = json.dumps([{"name": "yt_dlp", "version": "2026.9.1", "latest_version": "2026.10.30"}])
    yt = sup.ytdlp_upgrade(raw)
    assert yt["package"] == "yt-dlp" and yt["to"] == "2026.10.30"
    assert sup.ytdlp_upgrade(json.dumps([{"name": "aiohttp", "latest_version": "9"}])) is None
    merged = sup.merge_upgrades([{"package": "yt-dlp", "from": "1", "to": "2026.10.1", "why": "GHSA"}], yt)
    assert len(merged) == 1 and merged[0]["to"] == "2026.10.30" and merged[0]["why"] == "GHSA"
    assert len(sup.merge_upgrades([], yt)) == 1


def test_plan_npm_splits_auto_fixes_from_majors_and_applies_filters():
    p = sup.plan_npm(NPM_AUDIT, "moderate", {"lodash-merge"})
    assert p["fixable"] == ["minimatch", "vite"]
    assert {"package": "shadcn", "fix": "shadcn@1.0.0 (major)"} in p["manual"]
    assert {"package": "ts-morph", "fix": None} in p["manual"]
    assert p["ignored"] == 2  # deepmerge (low) + lodash.merge (ignore list, normalised)


def test_update_pins_keeps_extras_comments_and_line_endings():
    text = "aiohttp==3.14.5\r\nsqlalchemy[asyncio]==2.1.6  # orm\r\nYT_DLP==2026.9.1\r\nother==1.0\r\n"
    new, changed = sup.update_pins(text, {"aiohttp": "3.14.10", "sqlalchemy": "2.1.7", "yt-dlp": "2026.10.30"})
    assert new == ("aiohttp==3.14.10\r\nsqlalchemy[asyncio]==2.1.7  # orm\r\nYT_DLP==2026.10.30\r\nother==1.0\r\n")
    assert sorted(changed) == ["aiohttp", "sqlalchemy", "yt-dlp"]
    same, changed = sup.update_pins("aiohttp==3.14.10\n", {"aiohttp": "3.14.10"})
    assert same == "aiohttp==3.14.10\n" and changed == ["aiohttp"]  # already pinned counts


def test_freeze_lines_and_lock_changes():
    assert sup.freeze_lines("a==1\n-e git+https://x#egg=b\nc @ file:///x\n# c\nD_e==2.0\n") == ["a==1", "D_e==2.0"]
    before = json.dumps({"packages": {"": {"version": "0"}, "node_modules/minimatch": {"version": "10.2.6"},
                                      "node_modules/old": {"version": "1.0.0"}}})
    after = json.dumps({"packages": {"": {"version": "0"}, "node_modules/minimatch": {"version": "11.0.1"},
                                     "node_modules/new": {"version": "2.0.0"}}})
    assert sup.lock_changes(before, after) == ["minimatch 10.2.6 -> 11.0.1", "new (new) -> 2.0.0", "old 1.0.0 -> (removed)"]
    assert sup.lock_changes("{}", "{}") == []


# ---------------------------------------------------------------------------
# Service.hold
# ---------------------------------------------------------------------------

def test_held_service_is_not_restarted_and_its_exit_is_not_a_crash(_isolate):
    s = fake_service("bot")
    s.hold = sup.DEPS_HOLD
    s.kill_tree()
    s.poll(time.time())  # sees the exit
    assert s.proc is None and s.last_exit["reason"] == "stopped for the dependency update"
    assert _isolate == []  # no red alert
    s.poll(time.time() + 3600)
    assert s.started == 0  # held: never started
    s.hold = None
    s.poll(time.time())
    assert s.started == 1


# ---------------------------------------------------------------------------
# Control API
# ---------------------------------------------------------------------------

class FakeUpdater:
    def __init__(self):
        self.calls, self.running = [], False

    def run_async(self, mode, **kw):
        self.calls.append((mode, kw))
        return True

    def is_running(self):
        return self.running

    def status(self):
        return {"running": self.running, "step": None, "current": None, "last_run": None, "held": []}


def _call(server, method, path, body=None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {SECRET}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def stack():
    services = [fake_service("bot"), fake_service("dashboard"), fake_service("tunnel")]
    upd = FakeUpdater()
    ctrl = sup.Controller(services, cooldown=30, secret=SECRET, updater=upd)
    server = sup.ControlServer(ctrl, SECRET, port=0)
    assert server.start()
    yield {"ctrl": ctrl, "server": server, "upd": upd, "services": {s.name: s for s in services}}
    server.stop()


BODY = {"requested_by": "keira (123456789012345678)", "source": "dashboard"}


def test_update_deps_is_queued_then_started_by_the_main_loop(stack):
    srv, ctrl, upd = stack["server"], stack["ctrl"], stack["upd"]
    code, body = _call(srv, "POST", "/update-deps", dict(BODY, mode="check", reason="weekly\nreport"))
    assert code == 202 and body["queued"] is True and body["mode"] == "check"
    assert upd.calls == []  # the HTTP thread never starts it
    status = _call(srv, "GET", "/status")[1]
    assert status["dependency_update"]["pending"] is True
    assert _call(srv, "POST", "/update-deps", dict(BODY, mode="apply"))[1]["error"] == "update_in_progress"
    assert ctrl.drain() == ["dependency-update"]
    assert upd.calls == [("check", {"requested_by": "keira (123456789012345678)", "source": "dashboard",
                                    "reason": "weekly report"})]
    assert _call(srv, "GET", "/status")[1]["dependency_update"]["pending"] is False
    code, body = _call(srv, "POST", "/update-deps", dict(BODY, mode="apply"))
    assert code == 429 and body["error"] == "cooldown"


def test_update_deps_validation(stack):
    srv = stack["server"]
    assert _call(srv, "POST", "/update-deps", dict(BODY, mode="force"))[1]["error"] == "invalid_mode"
    assert _call(srv, "POST", "/update-deps", {"mode": "check", "source": "dashboard"})[1]["error"] == "invalid_requested_by"
    assert _call(srv, "GET", "/update-deps")[0] == 405
    stack["upd"].running = True
    assert _call(srv, "POST", "/update-deps", dict(BODY, mode="check"))[0] == 409


def test_update_deps_without_updater_is_409():
    ctrl = sup.Controller([fake_service("bot")], secret=SECRET)
    code, body = ctrl.submit_update(dict(BODY, mode="check"))
    assert code == 409 and body["error"] == "not_managed"
    assert ctrl.status()["dependency_update"] is None


def test_restart_of_a_held_service_is_refused(stack):
    stack["services"]["bot"].hold = sup.DEPS_HOLD
    stack["ctrl"].publish()
    code, body = _call(stack["server"], "POST", "/restart", dict(BODY, target="bot"))
    assert code == 409 and body["error"] == "update_in_progress"
    status = _call(stack["server"], "GET", "/status")[1]
    assert status["services"][0]["held"] == sup.DEPS_HOLD


# ---------------------------------------------------------------------------
# DependencyUpdater runs (fake runner)
# ---------------------------------------------------------------------------

def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "server" / "frontend" / "dist").mkdir(parents=True)
    (root / "server" / "frontend" / "dist" / "index.html").write_text("old build", encoding="utf-8")
    (root / "pot_server" / "server").mkdir(parents=True)
    for proj in (root / "server" / "frontend", root / "pot_server" / "server"):
        (proj / "package.json").write_text('{"name": "x"}', encoding="utf-8")
        (proj / "package-lock.json").write_text(json.dumps({"packages": {"node_modules/minimatch": {"version": "10.2.6"}}}),
                                                encoding="utf-8")
    (root / ".env").write_text("", encoding="utf-8")
    (root / "requirements.txt").write_bytes(b"aiohttp==3.14.5\r\nyt-dlp==2026.9.1\r\n")
    (root / "server" / "requirements.txt").write_text("aiohttp==3.14.5\nuvicorn==0.54.0\n", encoding="utf-8")
    return root


class Runner:
    """Records commands; answers from `script` (a list of (predicate, handler))."""

    def __init__(self, root: Path, *, pip_audit=None, outdated="[]", npm_audit=None, fail=()):
        self.root, self.calls, self.fail = root, [], set(fail)
        self.pip_audit = pip_audit if pip_audit is not None else json.dumps({"dependencies": []})
        self.outdated = outdated
        self.npm_audit = npm_audit or {}

    def __call__(self, cmd, cwd, timeout, env):
        cmd = [str(c) for c in cmd]
        line = " ".join(cmd)
        self.calls.append((line, Path(cwd)))
        for key in self.fail:
            if key in line:
                return 1, "", f"{key} failed"
        if "pip_audit" in line:
            return 1, self.pip_audit, "warning on stderr"
        if "list --outdated" in line:
            return 0, self.outdated, ""
        if "pip freeze" in line or line.endswith("freeze --disable-pip-version-check"):
            return 0, "aiohttp==3.14.5\nyt-dlp==2026.9.1\n-e git+x#egg=y\n", ""
        if " audit --omit=dev --json" in line:
            proj = "frontend" if Path(cwd).name == "frontend" else "pot_server"
            return 1, self.npm_audit.get(proj, json.dumps({"vulnerabilities": {}})), ""
        if " audit fix" in line:
            (Path(cwd) / "package-lock.json").write_text(
                json.dumps({"packages": {"node_modules/minimatch": {"version": "11.0.1"}}}), encoding="utf-8")
            return 0, "changed 1 package", ""
        if "vite build" in line:
            out = Path(cwd) / "dist-next"
            out.mkdir()
            (out / "index.html").write_text("new build", encoding="utf-8")
            return 0, "built", ""
        return 0, "ok", ""

    def ran(self, fragment: str) -> bool:
        return any(fragment in c for c, _ in self.calls)


def make_updater(root, runner, services=()):
    return sup.DependencyUpdater(services, root=root, log_dir=sup.LOG_DIR, python="py", runner=runner,
                                 which=lambda name: f"/bin/{name}")


def drive(upd: sup.DependencyUpdater, services, mode="apply", timeout=20.0) -> dict:
    """Run like main() does: the update in its worker thread, tick + poll here."""
    assert upd.run_async(mode, requested_by="keira", source="dashboard", reason=None)
    deadline = time.time() + timeout
    while upd.is_running() and time.time() < deadline:
        upd.tick()
        for s in services:
            s.poll(time.time())
        time.sleep(0.01)
    upd.tick()  # release
    assert not upd.is_running()
    return upd.last_run


def test_check_mode_only_audits(tmp_path, _isolate):
    root = make_root(tmp_path)
    runner = Runner(root, pip_audit=PIP_AUDIT, npm_audit={"frontend": NPM_AUDIT})
    upd = make_updater(root, runner)
    run = upd.run("check", requested_by="keira", source="cli")
    assert run["ok"] and run["mode"] == "check"
    assert [u["package"] for u in run["plan"]["python"]] == ["aiohttp", "Ignored_Pkg"]
    assert run["plan"]["npm"]["frontend"]["fixable"] == ["lodash.merge", "minimatch", "vite"]
    assert not runner.ran("audit fix") and not runner.ran("install")
    assert any("shadcn" in m for m in run["manual"]) and any("Brotli" in m for m in run["manual"])
    assert _isolate == []  # a check posts no webhook
    saved = json.loads((sup.LOG_DIR / sup.DEPS_STATE_FILE).read_text(encoding="utf-8"))
    assert saved["last_run"]["mode"] == "check"
    assert make_updater(root, runner).last_run["at"] == run["at"]  # survives a supervisor restart


def test_audit_failure_is_reported_not_raised(tmp_path):
    root = make_root(tmp_path)
    upd = make_updater(root, Runner(root, pip_audit="ModuleNotFoundError: pip_audit"))
    run = upd.run("check")
    assert run["ok"] is False and run["error"] is None
    assert not run["steps"][0]["ok"] and "pip-audit did not run" in run["steps"][0]["detail"]


def test_apply_python_stops_installs_tests_pins_and_restarts(tmp_path, _isolate):
    root = make_root(tmp_path)
    bot, dash, tunnel = fake_service("bot"), fake_service("dashboard"), fake_service("tunnel")
    outdated = json.dumps([{"name": "yt-dlp", "version": "2026.9.1", "latest_version": "2026.10.30"}])
    pip_audit = json.dumps({"dependencies": [
        {"name": "aiohttp", "version": "3.14.5", "vulns": [{"id": "GHSA-a", "fix_versions": ["3.14.6"]}]},
        {"name": "multidict", "version": "7.2.1", "vulns": [{"id": "GHSA-m", "fix_versions": ["7.2.2"]}]},
    ]})
    runner = Runner(root, pip_audit=pip_audit, outdated=outdated)
    upd = make_updater(root, runner, [bot, dash, tunnel])
    run = drive(upd, [bot, dash, tunnel])
    assert run["ok"], run["steps"]
    assert bot.kills == 1 and dash.kills == 1 and tunnel.kills == 0
    assert bot.hold is None and dash.hold is None and run["restarted"] == ["bot", "dashboard"]
    assert bot.last_exit["reason"] == "stopped for the dependency update"
    assert runner.ran("pip --disable-pip-version-check --no-input install aiohttp==3.14.6 multidict==7.2.2 yt-dlp==2026.10.30")
    assert runner.ran("pip --disable-pip-version-check --no-input check") and runner.ran("-m pytest -q -x")
    assert (root / "requirements.txt").read_bytes() == b"aiohttp==3.14.6\r\nyt-dlp==2026.10.30\r\nmultidict==7.2.2\r\n"
    assert (root / "server" / "requirements.txt").read_text(encoding="utf-8") == "aiohttp==3.14.6\nuvicorn==0.54.0\n"
    freeze = (sup.LOG_DIR / sup.DEPS_BACKUP_DIR / "pip-freeze-before.txt").read_text(encoding="utf-8")
    assert freeze == "aiohttp==3.14.5\nyt-dlp==2026.9.1\n"
    assert any("aiohttp 3.14.5 -> 3.14.6" in c for c in run["changes"])
    (title, _desc, color, ping), _ = _isolate[-1]
    assert title == "Dependency update finished" and color == sup.INFO_COLOR and ping is False


def test_apply_python_rolls_back_when_the_tests_fail(tmp_path, _isolate):
    root = make_root(tmp_path)
    bot, dash = fake_service("bot"), fake_service("dashboard")
    pip_audit = json.dumps({"dependencies": [
        {"name": "aiohttp", "version": "3.14.5", "vulns": [{"id": "GHSA-a", "fix_versions": ["3.14.6"]}]}]})
    runner = Runner(root, pip_audit=pip_audit, fail={"-m pytest"})
    before = (root / "requirements.txt").read_bytes()
    upd = make_updater(root, runner, [bot, dash])
    run = drive(upd, [bot, dash])
    assert run["ok"] is False
    assert runner.ran("install -r") and "pip-freeze-before.txt" in [c for c, _ in runner.calls if "install -r" in c][0]
    assert (root / "requirements.txt").read_bytes() == before  # not pinned
    assert any("rolled back" in c for c in run["changes"])
    assert bot.hold is None and dash.hold is None  # released even on failure
    assert _isolate[-1][0][0] == "Dependency update had problems"


def test_apply_frontend_rebuilds_and_swaps_without_stopping_anything(tmp_path):
    root = make_root(tmp_path)
    bot, dash = fake_service("bot"), fake_service("dashboard")
    runner = Runner(root, npm_audit={"frontend": json.dumps({"vulnerabilities": {
        "minimatch": {"severity": "moderate", "fixAvailable": True}}})})
    upd = make_updater(root, runner, [bot, dash])
    run = drive(upd, [bot, dash])
    fe = root / "server" / "frontend"
    assert run["ok"], run["steps"]
    assert (fe / "dist" / "index.html").read_text(encoding="utf-8") == "new build"
    assert not (fe / "dist-next").exists() and not (fe / "dist-old").exists()
    assert runner.ran("/bin/npx --no-install tsc -b") and runner.ran("vite build --outDir dist-next --emptyOutDir")
    assert bot.kills == 0 and dash.kills == 0 and run["restarted"] == []
    assert any("minimatch 10.2.6 -> 11.0.1" in c for c in run["changes"])


def test_apply_frontend_build_failure_restores_the_package_files(tmp_path):
    root = make_root(tmp_path)
    runner = Runner(root, fail={"tsc -b"}, npm_audit={"frontend": json.dumps({"vulnerabilities": {
        "minimatch": {"severity": "moderate", "fixAvailable": True}}})})
    upd = make_updater(root, runner)
    run = drive(upd, [])
    fe = root / "server" / "frontend"
    assert run["ok"] is False
    assert "10.2.6" in (fe / "package-lock.json").read_text(encoding="utf-8")  # restored
    assert runner.ran("npm ci") and not runner.ran("vite build")
    assert (fe / "dist" / "index.html").read_text(encoding="utf-8") == "old build"


def test_apply_skips_stop_work_when_the_bot_runs_outside_the_supervisor(tmp_path):
    root = make_root(tmp_path)
    bot, dash = fake_service("bot", running=False), fake_service("dashboard")
    bot.skip_reason = "port in use"
    pip_audit = json.dumps({"dependencies": [
        {"name": "aiohttp", "version": "3.14.5", "vulns": [{"id": "GHSA-a", "fix_versions": ["3.14.6"]}]}]})
    runner = Runner(root, pip_audit=pip_audit)
    upd = make_updater(root, runner, [bot, dash])
    run = drive(upd, [bot, dash])
    assert run["ok"] is False and not runner.ran("install")
    assert "outside the supervisor" in next(s for s in run["steps"] if s["name"] == "stop bot and dashboard")["detail"]
    assert dash.kills == 0 and bot.hold is None and dash.hold is None and run["restarted"] == []


def test_nothing_to_do(tmp_path):
    root = make_root(tmp_path)
    upd = make_updater(root, Runner(root))
    run = drive(upd, [])
    assert run["ok"] and run["changes"] == ["Nothing to update automatically."]


def test_status_shows_progress_without_output(tmp_path):
    root = make_root(tmp_path)
    gate = threading.Event()

    class SlowRunner(Runner):
        def __call__(self, cmd, cwd, timeout, env):
            if "list --outdated" in " ".join(map(str, cmd)):
                gate.wait(5)
            return super().__call__(cmd, cwd, timeout, env)

    upd = make_updater(root, SlowRunner(root))
    assert upd.run_async("check", requested_by="keira", source="cli")
    deadline = time.time() + 5
    while upd.status()["step"] != "checking yt-dlp" and time.time() < deadline:
        time.sleep(0.01)
    st = upd.status()
    assert st["running"] and st["current"]["mode"] == "check"
    assert st["current"]["steps"][0]["name"] == "audit Python (pip-audit)" and "output" not in st["current"]["steps"][0]
    assert upd.run_async("check") is False  # one at a time
    gate.set()
    while upd.is_running() and time.time() < deadline + 5:
        time.sleep(0.01)
    assert upd.status()["last_run"]["ok"]


def test_cli_update_deps(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(sup, "_cli_call", lambda m, p, b=None: (calls.append((m, p, b)), (202, {"queued": True}))[1])
    assert sup.cli(["update-deps"]) == 0
    assert sup.cli(["u", "apply", "fix", "minimatch"]) == 0
    assert calls[0][2]["mode"] == "check" and calls[0][2]["reason"] is None
    assert calls[1][2]["mode"] == "apply" and calls[1][2]["reason"] == "fix minimatch"
    text = sup.format_summary({"supervisor": {}, "services": [{"name": "bot", "held": "dependency update"}],
                               "dependency_update": {"running": True, "step": "pip check", "last_run": None}})
    assert "paused (dependency update)" in text and "dependency update: running (pip check)" in text
