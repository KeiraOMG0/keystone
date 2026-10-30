"""supervisor.py CLI helpers used by sv.bat: summary formatting, aliases, health, follow.

No supervisor runs: _cli_call / _probe are replaced, and follow reads a tmp LOG_DIR.
"""

from __future__ import annotations

import pytest

import supervisor as sup

STATUS = {
    "supervisor": {"pid": 9164, "started_at": "2026-10-21T08:56:53+00:00", "version": "2.1"},
    "services": [
        {"name": "bot", "running": True, "pid": 24716, "uptime_s": 90061, "restarts": 23, "health_fails": 0,
         "last_exit": {"reason": "exit code 3221225477", "at": "2026-10-22T09:36:41+00:00"}, "last_request": None},
        {"name": "dashboard", "running": True, "pid": 7880, "uptime_s": 150, "restarts": 3, "health_fails": 2,
         "last_exit": {"reason": "hung (health check failed), killed by supervisor", "at": "2026-10-28T01:19:35+00:00"},
         "last_request": None},
        {"name": "tunnel", "running": False, "pid": None, "uptime_s": None, "restarts": 22, "health_fails": 0,
         "next_start_in_s": 12.4, "last_exit": None,
         "last_request": {"by": "someone", "source": "dashboard", "reason": "flaky", "at": "2026-10-22T07:45:49+00:00"}},
    ],
    "log_rotation": {"last_run": {"at": "2026-10-26T04:00:00-04:00", "ok": True},
                     "next_run_at": "2026-11-02T04:00:00-05:00", "overdue": False, "deferred": []},
}


def test_format_summary():
    text = sup.format_summary(STATUS)
    assert "Supervisor v2.1  pid 9164" in text
    assert "running (health 2/3)" in text
    assert "down, retry in 12s" in text
    assert "1d 1h" in text and "2m 30s" in text
    assert "exit code 3221225477 (0xC0000005)" in text
    assert "last requested restart" in text and "via dashboard (flaky)" in text
    assert "log rotation: last" in text and "(ok)" in text


@pytest.mark.parametrize("svc, expected", [
    ({"running": True, "restart_pending": True}, "restarting"),
    ({"running": False, "skip_reason": "port taken"}, "not managed"),
    ({"running": False, "waiting_on_dependency": True}, "waiting for bot"),
    ({"running": False}, "down"),
])
def test_service_state(svc, expected):
    assert sup._service_state(svc) == expected


def _record(monkeypatch, code=200, payload=None):
    seen = []
    monkeypatch.setattr(sup, "_cli_call", lambda m, p, b=None: seen.append((m, p, b)) or (code, payload or {}))
    return seen


def test_restart_aliases(monkeypatch):
    seen = _record(monkeypatch, 202, {"accepted": ["dashboard"]})
    assert sup.cli(["r", "d", "it", "froze"]) == 0
    method, path, body = seen[0]
    assert (method, path) == ("POST", "/restart")
    assert body["target"] == "dashboard" and body["reason"] == "it froze" and body["source"] == "cli"


def test_empty_reason_is_none(monkeypatch):
    # sv.bat's menu always passes the reason as one quoted (possibly empty) argument
    seen = _record(monkeypatch, 202)
    sup.cli(["restart", "bot", ""])
    sup.cli(["rotate-logs", ""])
    assert seen[0][2]["reason"] is None and seen[1][2]["reason"] is None


def test_summary_pid_tail(monkeypatch, capsys):
    _record(monkeypatch, 200, STATUS)
    assert sup.cli(["s"]) == 0
    assert "SERVICE" in capsys.readouterr().out
    assert sup.cli(["pid"]) == 0
    assert capsys.readouterr().out.strip() == "9164"
    seen = _record(monkeypatch, 200, {"service": "tunnel", "lines": ["a", "b"]})
    assert sup.cli(["t", "t", "5"]) == 0
    assert seen[0][1] == "/logs?service=tunnel&lines=5"
    assert capsys.readouterr().out.splitlines() == ["a", "b"]


def test_unreachable_and_unknown(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("refused")
    monkeypatch.setattr(sup, "_cli_call", boom)
    assert sup.cli(["pid"]) == 1
    assert "not reachable" in capsys.readouterr().out
    assert sup.cli(["nope"]) == 2
    assert sup.cli(["help"]) == 0
    assert "follow <log>" in capsys.readouterr().out


def test_health(monkeypatch, capsys):
    _record(monkeypatch, 200, STATUS)
    monkeypatch.setenv("APPEAL_BASE_URL", "https://public.example")
    results = {
        f"http://127.0.0.1:{sup.INTERNAL_PORT}/health": (True, "HTTP 200 in 5 ms", {"status": "ok"}),
        "http://127.0.0.1:8080/api/status": (False, "no answer (timed out)", None),
        "https://public.example/api/status": (True, "HTTP 200 in 9 ms", {"bot": {"status": "up"}}),
    }
    monkeypatch.setattr(sup, "_probe", lambda url, timeout: results[url])
    assert sup.cli(["health"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  dashboard (local)" in out and "OK    bot (internal API)" in out
    assert "[dashboard ?, internal_api ?, bot up]" in out


def test_follow_prints_tail_new_lines_and_rotation(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sup, "LOG_DIR", tmp_path)
    log = tmp_path / "bot.log"
    log.write_bytes(b"one\ntwo\nthree\n")
    steps = iter([
        lambda: log.open("ab").write(b"four\nhal"),       # half line is held back
        lambda: log.open("ab").write(b"f\n"),
        lambda: log.write_bytes(b"new\n"),                # rotated: smaller file
    ])
    monkeypatch.setattr(sup.time, "sleep", lambda s: next(steps, lambda: None)())
    assert sup.cli_follow("b", lines=2, poll=0, max_polls=3) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["two", "three"] and lines[2].startswith("--- following ")
    assert lines[3:] == ["four", "half", "--- log rotated ---", "new"]


def test_follow_rejects_unknown_log(capsys):
    assert sup.cli_follow("kicks") == 2
