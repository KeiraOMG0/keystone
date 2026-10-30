"""Cookie-extension listener: auth, CORS, filtering, limits, atomic write, no value logging.

Binds only 127.0.0.1 on an ephemeral port (port 0) with a temp cookies path.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os

import aiohttp
import pytest

from services import cookie_listener_service as cls
from services.cookie_listener_service import (
    CookieListenerConfigError,
    CookieListenerService,
    parse_netscape,
    write_atomic,
)

SECRET = "unit-test-secret-not-real"
H = {cls.SECRET_HEADER: SECRET}
EXT = "chrome-extension://abcdefghijklmnop"
VALUE = "SUPERSECRETCOOKIEVALUE123"


def line(domain=".youtube.com", name="SID", value=VALUE, path="/", secure="TRUE", expiry="1893456000", incl="TRUE"):
    return "\t".join([domain, incl, path, secure, expiry, name, value])


def body(*lines):
    return "# Netscape HTTP Cookie File\n\n" + "\n".join(lines) + "\n"


@pytest.fixture
async def svc(tmp_path):
    s = CookieListenerService(
        host="127.0.0.1", port=0, secret=SECRET, cookies_path=str(tmp_path / "cookies.txt"),
        max_bytes=4096, rate_per_min=1000,
    )
    await s.start()
    yield s
    await s.stop()
    assert not s.running


def url(s, path):
    return f"http://127.0.0.1:{s.port}{path}"


async def req(method, u, **kw):
    async with aiohttp.ClientSession() as session:
        async with session.request(method, u, **kw) as r:
            return r.status, dict(r.headers), await r.text()


# -- startup -----------------------------------------------------------------


async def test_starts_without_secret_in_origin_mode(tmp_path):
    s = CookieListenerService(port=0, secret="", cookies_path=str(tmp_path / "c.txt"))
    await s.start()
    try:
        assert s.running and s.auth_mode == "origin"
    finally:
        await s.stop()


async def test_binds_loopback_only_by_default():
    assert CookieListenerService().host == "127.0.0.1"


# -- auth --------------------------------------------------------------------


async def test_auth_required_on_both_endpoints(svc):
    status, _, _ = await req("POST", url(svc, "/cookies"), data=body(line()))
    assert status == 401
    assert not os.path.exists(svc.cookies_path)
    # the read-only poll stays open so the stock extension works either way
    status, _, text = await req("GET", url(svc, "/needs-refresh"))
    assert status == 200 and "needsRefresh" in text


async def test_wrong_secret_rejected_and_compare_is_constant_time(svc, monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(cls.hmac, "compare_digest", spy)
    for bad in ("", "x", SECRET[:-1], SECRET + "x", "y" * 500):
        status, _, _ = await req("POST", url(svc, "/cookies"), headers={cls.SECRET_HEADER: bad}, data=body(line()))
        assert status == 401
    status, _, _ = await req("POST", url(svc, "/cookies"), headers=H, data=body(line()))
    assert status == 200
    # every comparison used compare_digest on fixed-length digests, whatever the input length
    assert len(calls) == 6 and set(calls) == {(32, 32)}


async def test_needs_refresh_flow(svc):
    _, _, text = await req("GET", url(svc, "/needs-refresh"), headers=H)
    assert '"needsRefresh": false' in text
    svc.request_refresh()
    _, _, text = await req("GET", url(svc, "/needs-refresh"), headers=H)
    assert '"needsRefresh": true' in text
    await req("POST", url(svc, "/cookies"), headers=H, data=body(line()))
    _, _, text = await req("GET", url(svc, "/needs-refresh"), headers=H)
    assert '"needsRefresh": false' in text


async def test_request_refresh_and_wait(svc):
    async def push():
        await asyncio.sleep(0.1)
        await req("POST", url(svc, "/cookies"), headers=H, data=body(line()))

    task = asyncio.create_task(push())
    assert await svc.request_refresh_and_wait(timeout=5) is True
    await task
    assert await svc.request_refresh_and_wait(timeout=0.1) is False


# -- CORS --------------------------------------------------------------------


async def test_preflight_allowed_for_extension_origin(svc):
    status, headers, _ = await req(
        "OPTIONS", url(svc, "/cookies"),
        headers={"Origin": EXT, "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type,x-cookie-secret"},
    )
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == EXT
    assert cls.SECRET_HEADER in headers["Access-Control-Allow-Headers"]
    assert "POST" in headers["Access-Control-Allow-Methods"]
    status, headers, _ = await req("OPTIONS", url(svc, "/cookies"), headers={"Origin": "moz-extension://x"})
    assert status == 204 and headers["Access-Control-Allow-Origin"] == "moz-extension://x"


async def test_web_origin_rejected_even_with_secret_and_no_wildcard_cors(svc):
    for method in ("OPTIONS", "GET", "POST"):
        status, headers, _ = await req(
            method, url(svc, "/cookies" if method == "POST" else "/needs-refresh"),
            headers={**H, "Origin": "https://evil.example"}, data=body(line()) if method == "POST" else None,
        )
        assert status == 403
        assert "Access-Control-Allow-Origin" not in headers
    status, headers, _ = await req("GET", url(svc, "/needs-refresh"), headers={**H, "Origin": EXT})
    assert status == 200 and headers["Access-Control-Allow-Origin"] == EXT


# -- content validation --------------------------------------------------------


def test_parse_filters_domains_and_malformed_lines():
    text = body(
        line(".youtube.com"), line("accounts.google.com", name="A"), line(".google.com", name="B"),
        line(".googlevideo.com", name="C"), "#HttpOnly_" + line(".youtube.com", name="D"),
        line("evil.com", name="E"), line("youtube.com.evil.com", name="F"), line("notyoutube.com", name="G"),
        "only\ttwo", line(name="H", path="nopath"), line(name="I", expiry="abc"), line(name="J", secure="MAYBE"),
    )
    clean, accepted, rejected = parse_netscape(text)
    assert accepted == 5 and rejected == 7
    joined = "\n".join(clean)
    assert "evil.com" not in joined and "notyoutube" not in joined
    assert any(c.startswith("#HttpOnly_.youtube.com") for c in clean)


async def test_accepts_youtube_cookies_and_drops_others(svc):
    status, headers, text = await req(
        "POST", url(svc, "/cookies"), headers=H, data=body(line(), line("evil.com", name="X"))
    )
    assert status == 200 and text == "ok"  # the body the extension expects
    assert headers["X-Cookies-Accepted"] == "1" and headers["X-Cookies-Rejected"] == "1"
    content = open(svc.cookies_path, encoding="utf-8").read()
    assert content.startswith("# Netscape HTTP Cookie File") and "evil.com" not in content and VALUE in content
    snap = svc.snapshot()
    assert snap["accepted_count"] == 1 and snap["rejected_count"] == 1
    assert snap["updates_total"] == 1 and snap["last_update_at"] and snap["last_error"] is None


async def test_nothing_acceptable_leaves_existing_file_untouched(svc):
    write_atomic(svc.cookies_path, "ORIGINAL\n")
    status, _, _ = await req("POST", url(svc, "/cookies"), headers=H, data=body(line("evil.com")))
    assert status == 422
    assert open(svc.cookies_path).read() == "ORIGINAL\n"
    assert svc.snapshot()["last_error"] and svc.snapshot()["updates_total"] == 0


async def test_size_cap(svc):
    big = body(*[line(name=f"N{i}", value="v" * 50) for i in range(200)])
    assert len(big) > svc.max_bytes
    status, _, _ = await req("POST", url(svc, "/cookies"), headers=H, data=big)
    assert status == 413
    assert not os.path.exists(svc.cookies_path)


@pytest.mark.parametrize("payload", [b"", b"   \n", b"\xff\xfe\x00garbage", b"{not cookies}", b"a\tb\tc"])
async def test_malformed_bodies_rejected(svc, payload):
    status, _, _ = await req("POST", url(svc, "/cookies"), headers=H, data=payload)
    assert status in (400, 422)
    assert not os.path.exists(svc.cookies_path)


async def test_rate_limit(tmp_path):
    s = CookieListenerService(port=0, secret=SECRET, cookies_path=str(tmp_path / "c.txt"), rate_per_min=3)
    await s.start()
    try:
        codes = [(await req("GET", url(s, "/needs-refresh"), headers=H))[0] for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
    finally:
        await s.stop()


# -- atomic write / backup / logging ---------------------------------------------


async def test_backup_kept_and_no_temp_files_left(svc):
    await req("POST", url(svc, "/cookies"), headers=H, data=body(line(name="First", value="one")))
    await req("POST", url(svc, "/cookies"), headers=H, data=body(line(name="Second", value="two")))
    d = os.path.dirname(svc.cookies_path)
    assert sorted(os.listdir(d)) == ["cookies.txt", "cookies.txt.bak"]
    assert "Second" in open(svc.cookies_path).read()
    assert "First" in open(svc.cookies_path + ".bak").read()


def test_write_atomic_failure_keeps_original_and_cleans_up(tmp_path, monkeypatch):
    path = tmp_path / "cookies.txt"
    path.write_text("ORIGINAL")

    def boom(src, dst, attempts=5):
        raise PermissionError("locked")

    monkeypatch.setattr(cls, "_replace", boom)
    with pytest.raises(PermissionError):
        write_atomic(str(path), "NEW")
    assert path.read_text() == "ORIGINAL"
    assert [p.name for p in tmp_path.iterdir()] == ["cookies.txt"]


async def test_reassert_rewrites_last_accepted_cookies(svc):
    await req("POST", url(svc, "/cookies"), headers=H, data=body(line(name="Keep")))
    write_atomic(svc.cookies_path, "CLOBBERED BY YTDLP\n")
    await svc.reassert()
    assert "Keep" in open(svc.cookies_path).read()


async def test_never_logs_cookie_values_or_secret(svc, caplog):
    caplog.set_level(logging.DEBUG)
    await req("POST", url(svc, "/cookies"), headers=H, data=body(line()))
    await req("POST", url(svc, "/cookies"), headers=H, data=body(line("evil.com", value=VALUE + "bad")))
    await req("GET", url(svc, "/needs-refresh"), headers={cls.SECRET_HEADER: "wrong-guess-value"})
    assert "accepted" in caplog.text  # counts are logged...
    for secret in (VALUE, SECRET, "wrong-guess-value"):
        assert secret not in caplog.text  # ...values are not
    assert VALUE not in (svc.snapshot()["last_error"] or "")


# -- auth modes (no secret configured) -------------------------------------------


@pytest.fixture
async def open_svc(tmp_path):
    s = CookieListenerService(port=0, secret="", cookies_path=str(tmp_path / "cookies.txt"), rate_per_min=1000)
    await s.start()
    yield s
    await s.stop()


async def test_no_secret_extension_origin_accepted(open_svc):
    s = open_svc
    status, headers, text = await req("GET", url(s, "/needs-refresh"), headers={"Origin": EXT})
    assert status == 200 and headers["Access-Control-Allow-Origin"] == EXT
    status, _, text = await req("POST", url(s, "/cookies"), headers={"Origin": EXT, "Content-Type": "text/plain"}, data=body(line()))
    assert status == 200 and text == "ok"
    assert os.path.exists(s.cookies_path)
    status, _, _ = await req("POST", url(s, "/cookies"), headers={"Origin": "moz-extension://uuid"}, data=body(line()))
    assert status == 200


async def test_no_secret_web_origin_rejected(open_svc):
    for origin in ("https://evil.example", "http://localhost:3000", "null"):
        for method, path in (("GET", "/needs-refresh"), ("POST", "/cookies")):
            status, _, _ = await req(method, url(open_svc, path), headers={"Origin": origin}, data=body(line()) if method == "POST" else None)
            assert status == 403
    assert not os.path.exists(open_svc.cookies_path)


async def test_no_secret_and_no_origin_rejected(open_svc):
    status, _, _ = await req("POST", url(open_svc, "/cookies"), data=body(line()))
    assert status == 401
    assert not os.path.exists(open_svc.cookies_path)


async def test_no_secret_extension_id_allowlist(tmp_path):
    s = CookieListenerService(port=0, secret="", cookies_path=str(tmp_path / "c.txt"), extension_ids=["GoodID"])
    await s.start()
    try:
        ok, _, _ = await req("GET", url(s, "/needs-refresh"), headers={"Origin": "chrome-extension://goodid"})
        bad, _, _ = await req("GET", url(s, "/needs-refresh"), headers={"Origin": "chrome-extension://other"})
        assert (ok, bad) == (200, 403)
    finally:
        await s.stop()


async def test_secret_still_required_when_set_even_with_extension_origin(svc):
    status, _, _ = await req("POST", url(svc, "/cookies"), headers={"Origin": EXT}, data=body(line()))
    assert status == 401
    status, _, _ = await req("POST", url(svc, "/cookies"), headers={"Origin": EXT, **H}, data=body(line()))
    assert status == 200


async def test_preflight_works_without_secret(open_svc):
    status, headers, _ = await req("OPTIONS", url(open_svc, "/cookies"), headers={"Origin": EXT})
    assert status == 204 and headers["Access-Control-Allow-Origin"] == EXT


# -- API shape the musicbot-cookie-sync extension expects --------------------------


async def test_api_matches_extension_contract(open_svc):
    h = {"Origin": EXT}
    status, headers, text = await req("GET", url(open_svc, "/needs-refresh"), headers=h)
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    assert json.loads(text) == {"needsRefresh": False}
    status, headers, text = await req("POST", url(open_svc, "/cookies"), headers=h, data="   ")
    assert (status, text) == (400, "empty cookies payload") and headers["Content-Type"].startswith("text/plain")
    status, headers, text = await req("POST", url(open_svc, "/cookies"), headers=h, data=body(line()))
    assert (status, text) == (200, "ok") and headers["Content-Type"].startswith("text/plain")
    status, _, text = await req("GET", url(open_svc, "/nope"), headers=h)
    assert status == 404 and text == ""
    status, headers, _ = await req("OPTIONS", url(open_svc, "/cookies"), headers=h)
    assert status == 204 and headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    assert "Content-Type" in headers["Access-Control-Allow-Headers"]


def test_defaults_match_extension(monkeypatch):
    import config

    assert config._env_port("UNSET_PORT_VAR_FOR_TEST", 8999) == 8999
    assert config.MUSIC_COOKIE_LISTENER_PORT == 8999 or os.environ.get("MUSIC_COOKIE_LISTENER_PORT")
    assert config.MUSIC_COOKIE_LISTENER_ENABLED is True or "MUSIC_COOKIE_LISTENER_ENABLED" in os.environ
    assert CookieListenerService().port == 8999


# -- port in use: warn + background retry, never raise -----------------------------


async def test_port_in_use_warns_and_retries_in_background(tmp_path, caplog, monkeypatch):
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await real_sleep(0.05)

    monkeypatch.setattr(cls.asyncio, "sleep", fast_sleep)
    caplog.set_level(logging.INFO)
    s = CookieListenerService(port=port, secret="", cookies_path=str(tmp_path / "c.txt"))
    try:
        await s.start_background()  # must not raise
        assert not s.running and s.bind_pending
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "could not bind" in r.getMessage()]
        assert len(warnings) == 1 and "Another process may hold the port" in warnings[0].getMessage()
        await real_sleep(0.3)
        assert not s.running  # still blocked, still retrying quietly
        assert len([r for r in caplog.records if "could not bind" in r.getMessage()]) == 1
        blocker.close()
        for _ in range(60):
            if s.running:
                break
            await real_sleep(0.05)
        assert s.running and not s.bind_pending
        status, _, _ = await req("GET", url(s, "/needs-refresh"), headers={"Origin": EXT})
        assert status == 200
    finally:
        blocker.close()
        await s.stop()
    assert s._retry_task is None


async def test_stop_cancels_pending_retry(tmp_path):
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    s = CookieListenerService(port=blocker.getsockname()[1], secret="", cookies_path=str(tmp_path / "c.txt"))
    try:
        await s.start_background()
        task = s._retry_task
        await s.stop()
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
    finally:
        blocker.close()


# -- Chrome sends no Origin on the poll ------------------------------------------


async def test_poll_without_origin_or_secret_is_accepted(open_svc):
    status, _, text = await req("GET", url(open_svc, "/needs-refresh"))
    assert status == 200 and json.loads(text) == {"needsRefresh": False}
    status, _, _ = await req("OPTIONS", url(open_svc, "/cookies"))
    assert status == 204


async def test_poll_with_web_origin_rejected_and_with_extension_origin_accepted(open_svc):
    status, _, _ = await req("GET", url(open_svc, "/needs-refresh"), headers={"Origin": "https://evil.example"})
    assert status == 403
    status, _, _ = await req("GET", url(open_svc, "/needs-refresh"), headers={"Origin": EXT})
    assert status == 200


async def test_post_still_needs_extension_origin_or_secret(open_svc, svc):
    assert (await req("POST", url(open_svc, "/cookies"), data=body(line())))[0] == 401
    assert (await req("POST", url(open_svc, "/cookies"), headers={"Origin": EXT}, data=body(line())))[0] == 200
    assert (await req("POST", url(svc, "/cookies"), data=body(line())))[0] == 401


async def test_last_seen_updates_on_poll_and_first_contact_is_logged(open_svc, caplog):
    caplog.set_level(logging.INFO)
    assert open_svc.snapshot()["last_poll_at"] is None
    await req("GET", url(open_svc, "/needs-refresh"))
    first = open_svc.snapshot()["last_poll_at"]
    assert first is not None
    await asyncio.sleep(0.02)
    await req("GET", url(open_svc, "/needs-refresh"), headers={"Origin": EXT})
    assert open_svc.snapshot()["last_poll_at"] > first
    assert caplog.text.count("extension connected") == 1
    assert caplog.text.count("first request from extension origin") == 1  # id logged once
    await req("POST", url(open_svc, "/cookies"), headers={"Origin": EXT}, data=body(line()))
    assert "Cookies refreshed" in caplog.text and VALUE not in caplog.text


async def test_rejection_logging_is_rate_limited(tmp_path, caplog):
    now = [1000.0]
    s = CookieListenerService(port=0, secret="", cookies_path=str(tmp_path / "c.txt"), rate_per_min=10**6, clock=lambda: now[0])
    await s.start()
    try:
        caplog.set_level(logging.DEBUG)
        for _ in range(50):
            await req("POST", url(s, "/cookies"), data=body(line()))
        warns = [r for r in caplog.records if r.levelno == logging.WARNING and "rejected a request" in r.getMessage()]
        assert len(warns) == 1
        now[0] += 601
        await req("POST", url(s, "/cookies"), data=body(line()))
        warns = [r for r in caplog.records if r.levelno == logging.WARNING and "rejected a request" in r.getMessage()]
        assert len(warns) == 2 and "49 more" in warns[1].getMessage()
        # a different kind of rejection still gets its own first WARNING
        await req("POST", url(s, "/cookies"), headers={"Origin": "https://evil.example"}, data=body(line()))
        assert sum(1 for r in caplog.records if r.levelno == logging.WARNING and "web or non-allowed" in r.getMessage()) == 1
    finally:
        await s.stop()
