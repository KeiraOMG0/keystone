"""Diagnostics Logs section: which files are tailed, missing files, redaction, read_tail cache."""

from __future__ import annotations

import os

import pytest

import config
from services import diagnostics_service as ds
from tests.unit.test_diagnostics_redaction import BOT_TOKEN, INTERNAL_SECRET, _set_secret_env

EXPECTED_ORDER = ["bot", "dashboard", "tunnel", "supervisor", "pot_server", "kicks", "bot_startup"]


@pytest.fixture(autouse=True)
def _fresh_log_cache():
    ds.clear_log_cache()
    yield
    ds.clear_log_cache()


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "MUSIC_POT_SERVER_LOG", os.path.join("logs", "pot_server.log"), raising=False)
    (tmp_path / "logs" / "supervisor").mkdir(parents=True)
    return tmp_path


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def test_logs_section_lists_all_files_in_order(root):
    sup = root / "logs" / "supervisor"
    for name in ("bot.log", "dashboard.log", "tunnel.log", "supervisor.log"):
        _write(sup / name, f"{name} line 1\n")
    _write(root / "logs" / "pot_server.log", "pot line\n")
    _write(root / "kicks.log", "kick line\n")
    _write(root / "bot_startup.log", "startup line\n")
    _write(sup / "supervisor.lock", "123")
    _write(root / "logs" / "pot_server.pid", "456")

    sec = ds.logs_section()
    files = sec["files"]
    assert [f["key"] for f in files] == EXPECTED_ORDER
    assert [f["name"] for f in files] == [
        "bot.log", "dashboard.log", "tunnel.log", "supervisor.log", "pot_server.log", "kicks.log", "bot_startup.log",
    ]
    assert all(f["exists"] and f["label"] for f in files)
    assert files[0]["tail"] == ["bot.log line 1"]
    assert files[4]["path"] == os.path.join(str(root), "logs", "pot_server.log")
    # Tailed files are not repeated under "other files"; .pid/.lock are.
    assert sorted(o["name"] for o in sec["other_files"]) == ["pot_server.pid", "supervisor/supervisor.lock"]


def test_logs_section_missing_files(root):
    _write(root / "logs" / "supervisor" / "bot.log", "hello\n")
    files = {f["key"]: f for f in ds.logs_section()["files"]}
    assert files["bot"]["exists"] is True
    for key in EXPECTED_ORDER[1:]:
        assert files[key]["exists"] is False
        assert "tail" not in files[key]
        assert files[key]["path"]


def test_logs_section_bot_log_tail_is_longer(root):
    _write(root / "logs" / "supervisor" / "bot.log", "".join(f"b{i}\n" for i in range(400)))
    _write(root / "logs" / "supervisor" / "dashboard.log", "".join(f"d{i}\n" for i in range(400)))
    files = {f["key"]: f for f in ds.logs_section()["files"]}
    assert len(files["bot"]["tail"]) == 150 and files["bot"]["tail"][-1] == "b399"
    assert len(files["dashboard"]["tail"]) == 60


def test_logs_section_redacts_every_file(root, monkeypatch):
    _set_secret_env(monkeypatch)
    bad = f"ERROR leaked {BOT_TOKEN} and Authorization: Bearer abcdefgh12345678 and {INTERNAL_SECRET}\n"
    sup = root / "logs" / "supervisor"
    for name in ("bot.log", "dashboard.log", "tunnel.log", "supervisor.log"):
        _write(sup / name, bad)
    _write(root / "logs" / "pot_server.log", bad)
    _write(root / "kicks.log", bad)
    _write(root / "bot_startup.log", bad)
    for f in ds.logs_section()["files"]:
        joined = "\n".join(f["tail"])
        assert BOT_TOKEN not in joined and INTERNAL_SECRET not in joined and "abcdefgh12345678" not in joined
        assert ds.REDACTED in joined
        assert f["level_counts"]["ERROR"] == 1


def test_read_tail_cache_hit_growth_and_clear(tmp_path, monkeypatch):
    path = tmp_path / "x.log"
    _write(path, "one\ntwo\n")
    calls = []
    real = ds._read_tail_uncached

    def counting(*a, **kw):
        calls.append(a[0])
        return real(*a, **kw)

    monkeypatch.setattr(ds, "_read_tail_uncached", counting)

    first = ds.read_tail(str(path), 60)
    assert first["tail"] == ["one", "two"]
    second = ds.read_tail(str(path), 60)
    assert second["tail"] == ["one", "two"]
    assert len(calls) == 1  # unchanged file: served from the cache
    second["tail"].append("mutated")  # a caller's copy can't poison the cache
    assert ds.read_tail(str(path), 60)["tail"] == ["one", "two"]
    assert len(calls) == 1

    _write(path, "three\n")  # file grows -> size (and mtime) change -> re-read
    grown = ds.read_tail(str(path), 60)
    assert grown["tail"] == ["one", "two", "three"]
    assert len(calls) == 2

    assert ds._TAIL_CACHE
    ds.clear_log_cache()
    assert not ds._TAIL_CACHE
    ds.read_tail(str(path), 60)
    assert len(calls) == 3


def test_read_tail_cache_keys_on_line_count(tmp_path, monkeypatch):
    path = tmp_path / "y.log"
    _write(path, "".join(f"l{i}\n" for i in range(10)))
    assert len(ds.read_tail(str(path), 3)["tail"]) == 3
    assert len(ds.read_tail(str(path), 5)["tail"]) == 5


def test_read_tail_counts_cloudflared_levels(tmp_path):
    path = tmp_path / "tunnel.log"
    _write(path, "2026-10-14T07:04:58Z ERR Request failed\n2026-10-14T07:04:59Z WRN slow\n2026 INF ok\n")
    assert ds.read_tail(str(path), 60)["level_counts"] == {"WARNING": 1, "ERROR": 1, "CRITICAL": 0}


def test_dashboard_log_access_lines_are_scrubbed(root):
    ipv4 = "24.112.160.150"
    ipv6 = "2a02:2a40:81fe:1a00:bdbb:d388:b3f7:cabc"
    code, state = "OAuthCodeValue123abc", "OAuthStateValue456def"
    appeal = "AppealTokenValue789xyz"
    _write(
        root / "logs" / "supervisor" / "dashboard.log",
        f'INFO:     {ipv4}:0 - "GET /api/supervisor/status HTTP/1.1" 200 OK\n'
        f'INFO:     {ipv6}:0 - "GET /api/auth/callback?code={code}&state={state}&iss=x HTTP/1.1" 302 Found\n'
        f'INFO:     {ipv4}:51234 - "GET /appeal/{appeal} HTTP/1.1" 200 OK\n'
        f'INFO:     {ipv4}:0 - "POST /api/public/appeals/{appeal}/submit HTTP/1.1" 200 OK\n'
        'INFO:     127.0.0.1:60000 - "GET /api/status HTTP/1.1" 200 OK\n',
    )
    files = {f["key"]: f for f in ds.logs_section()["files"]}
    tail = files["dashboard"]["tail"]
    joined = "\n".join(tail)
    for raw in (ipv4, ipv6, "2a02:2a40", code, state, appeal):
        assert raw not in joined, raw
    assert tail[0].startswith('INFO:     <ip>:0 - "GET /api/supervisor/status')
    assert "?code=<redacted>&state=<redacted>&iss=x" in tail[1]
    assert "/appeal/<redacted> " in tail[2] and "/api/public/appeals/<redacted>/submit" in tail[3]
    assert tail[4].startswith("INFO:     127.0.0.1:60000 - ")  # loopback stays readable, as in supervisor.py
    # the cache holds the scrubbed text, not the raw lines
    cached = ds._TAIL_CACHE[files["dashboard"]["path"]][1]
    assert ipv4 not in "\n".join(cached["tail"]) and code not in "\n".join(cached["tail"])


def test_scrub_log_line_matches_supervisor():
    import supervisor

    lines = [
        'INFO:     24.112.160.150:0 - "GET /x?token=abcdefgh HTTP/1.1" 200 OK',
        'INFO:     ::1:5555 - "GET /appeal/tok123 HTTP/1.1" 200 OK',
        'INFO:     127.0.0.1:1 - "GET /api/auth/callback?code=c0de&state=st4te HTTP/1.1" 302',
    ]
    assert [ds.scrub_log_line(ln) for ln in lines] == supervisor.scrub_lines(lines)
