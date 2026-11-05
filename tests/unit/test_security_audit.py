"""Security audit regression tests (2026-10-30; the full audit report is kept privately).

These assert the security POLICY, so they fail when a boundary breaks:

* every dashboard /api route rejects a request with no credentials (401, or the
  hidden 404 for owner-only routes) unless it is on the explicit public list;
  every internal API route rejects a request without the bearer secret;
* forged / cross-purpose credentials (a tampered session cookie, an appeal
  identity cookie, the wrong bearer) authorize nothing;
* retention jobs delete what the Privacy Policy says they delete (expired
  sessions, old error reports, legacy archived tickets);
* the music URL check refuses loopback / private / link-local / non-http
  targets (SSRF) without touching yt-dlp;
* security headers, access-log scrubbing, and the dashboard login revoking
  its Discord token.
"""

from __future__ import annotations

import datetime
import logging
import re
import socket

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.app as server_app
from server import config
from server.auth.oauth import limiter
from server.db import engine as db_engine
from server.db.base import Base
from server.db.models import ErrorReport, Session

ORIGIN = config.FRONTEND_BASE_URL

# Routes that are deliberately reachable without a dashboard session. Anything
# else under /api that answers an anonymous request is a regression.
PUBLIC = {
    "/api/auth/login", "/api/auth/callback", "/api/auth/logout", "/api/auth/logout-all",
    "/api/public/appeal-auth/login", "/api/public/appeal-auth/callback", "/api/public/appeal-auth/logout",
    "/api/public/appeal-auth/me", "/api/public/appeal-auth/submit",
    "/api/public/appeals", "/api/public/appeals/{token}", "/api/public/client-errors",
    "/api/status", "/api/ws", "/api/{rest:path}",
}


def _flat(routes, prefix=""):
    for r in routes:
        if hasattr(r, "original_router"):
            ctx = getattr(r, "include_context", None)
            yield from _flat(r.original_router.routes, prefix + (getattr(ctx, "prefix", "") or ""))
        else:
            yield prefix, r


def _api_routes(app):
    out = []
    for prefix, r in _flat(app.routes):
        if isinstance(r, APIRoute):
            path = r.path if r.path.startswith("/api") else prefix + r.path
            if path.startswith("/api"):
                for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
                    out.append((method, path))
    return out


@pytest.fixture
async def temp_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_engine, "async_session_factory", factory)
    import server.auth.dependencies as deps

    monkeypatch.setattr(deps, "async_session_factory", factory)
    limiter.reset()
    yield factory
    limiter.reset()
    await engine.dispose()


DASHBOARD_ROUTES = _api_routes(server_app.app)


def test_route_inventory_is_complete():
    assert len(DASHBOARD_ROUTES) > 150  # the walk really found the routers


@pytest.mark.parametrize("method,path", [r for r in DASHBOARD_ROUTES if r[1] not in PUBLIC])
def test_anonymous_request_is_rejected_everywhere(temp_db, method, path):
    url = re.sub(r"\{[^}]+\}", "1", path)
    client = TestClient(server_app.app)
    r = client.request(method, url, json={}, headers={"Origin": ORIGIN})
    # 401 = needs a session; only the owner-only routes answer the hidden 404
    # (a wrong path would also 404, so 404 is accepted ONLY for those).
    owner_only = path.startswith("/api/debug/") or path in ("/api/supervisor/rotate-logs", "/api/supervisor/update-deps")
    if owner_only:
        assert r.status_code == 404 and r.json() == {"error": "not_found"}, (method, path, r.status_code)
    else:
        assert r.status_code == 401, (method, path, r.status_code, r.text[:200])
        assert r.json()["detail"]["error"] == "unauthenticated"


@pytest.mark.parametrize("cookie", ["garbage", "c2lk.forged-signature", ""])
def test_forged_session_cookie_is_anonymous(temp_db, cookie):
    client = TestClient(server_app.app)
    client.cookies.set(config.SESSION_COOKIE_NAME, cookie)
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/dashboard/stats").status_code == 401


def test_appeal_identity_cookie_is_not_a_dashboard_session(temp_db):
    """A credential for one purpose (appeal sign-in) must not open another (dashboard)."""
    from server.routes.appeal_auth import _identity_signer

    token = _identity_signer.dumps({"uid": "1", "name": "x"})
    client = TestClient(server_app.app)
    client.cookies.set(config.SESSION_COOKIE_NAME, token)
    assert client.get("/api/me").status_code == 401


def test_session_cookie_is_not_an_appeal_identity():
    from server.auth.dependencies import sign_session_id
    from server.routes.appeal_auth import identity_of

    req = type("R", (), {"cookies": {"appeal_identity": sign_session_id("1")}})()
    assert identity_of(req) is None


def test_internal_api_requires_the_bearer_secret_everywhere():
    import internal_api.app as internal

    app = internal.create_app()
    client = TestClient(app)
    routes = [r for r in _api_routes_all(app) if r[1] != "/health"]
    assert len(routes) > 100
    for method, path in routes:
        url = re.sub(r"\{[^}]+\}", "1", path)
        r = client.request(method, url, json={})
        assert r.status_code == 401, (method, path, r.status_code)
        for bad in ("Bearer wrong-secret", "Bearer ", "Basic abc"):
            r = client.request(method, url, json={}, headers={"Authorization": bad})
            assert r.status_code in (401, 403), (method, path, bad, r.status_code)


def _api_routes_all(app):
    out = []
    for prefix, r in _flat(app.routes):
        if isinstance(r, APIRoute):
            for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
                out.append((method, r.path if prefix and r.path.startswith(prefix) else prefix + r.path))
    return out


def test_internal_ws_secret_check_fails_closed(monkeypatch):
    from internal_api.routes import ws

    monkeypatch.setattr(ws.config, "INTERNAL_API_SECRET", "")
    assert ws.secret_ok("") is False  # an unset secret never matches an empty one
    monkeypatch.setattr(ws.config, "INTERNAL_API_SECRET", "s3cret-value")
    assert ws.secret_ok("s3cret-value") is True and ws.secret_ok("s3cret-valuX") is False
    assert ws.secret_ok(None) is False


def test_csrf_blocks_foreign_origin_on_state_changing_routes(temp_db):
    client = TestClient(server_app.app)
    for method, path in [("POST", "/api/auth/logout-all"), ("POST", "/api/moderation/ban"), ("DELETE", "/api/templates/1")]:
        r = client.request(method, path, json={}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and r.json() == {"error": "csrf_check_failed"}, path


@pytest.mark.parametrize("cookie_state,query_state", [(None, "abc"), ("abc", None), ("abc", "abd"), ("", "")])
def test_oauth_callback_rejects_missing_or_wrong_state(temp_db, cookie_state, query_state):
    client = TestClient(server_app.app)
    if cookie_state is not None:
        client.cookies.set(config.OAUTH_STATE_COOKIE_NAME, cookie_state)
    url = "/api/auth/callback?code=x" + (f"&state={query_state}" if query_state is not None else "")
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_state"
    assert config.SESSION_COOKIE_NAME not in r.headers.get("set-cookie", "")


# --------------------------------------------------------------------------- login revokes the token


class _FakeResp:
    def __init__(self, status, data=None):
        self.status_code, self._data = status, data or {}

    def json(self):
        return self._data


async def test_dashboard_login_revokes_discord_token(temp_db, monkeypatch):
    from server.auth import oauth

    calls = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            calls.append(("POST", url, kw.get("data", {}).get("token")))
            if url == oauth.DISCORD_TOKEN_URL:
                return _FakeResp(200, {"access_token": "AT-123"})
            return _FakeResp(200)

        async def get(self, url, **kw):
            calls.append(("GET", url, None))
            if url.endswith("/users/@me"):
                return _FakeResp(200, {"id": "42", "username": "u"})
            return _FakeResp(500)  # member lookup fails -> 502, token must STILL be revoked

    monkeypatch.setattr(oauth.httpx, "AsyncClient", FakeClient)
    client = TestClient(server_app.app)
    client.cookies.set(config.OAUTH_STATE_COOKIE_NAME, "st")
    r = client.get("/api/auth/callback?code=c&state=st", follow_redirects=False)
    assert r.status_code == 502
    assert ("POST", oauth.DISCORD_REVOKE_URL, "AT-123") in calls
    assert calls[-1][1] == oauth.DISCORD_REVOKE_URL


# --------------------------------------------------------------------------- retention


async def test_housekeeping_deletes_expired_sessions_and_old_reports(temp_db):
    from server.services import housekeeping

    now = datetime.datetime.now(datetime.timezone.utc)
    async with temp_db() as db:
        for sid, idle_days in (("fresh", 1), ("edge", 29), ("stale", 31), ("ancient", 97)):
            db.add(Session(session_id=sid, discord_user_id=1, discord_username="u", is_guild_member=True,
                           cached_role_ids=[], created_at=now, last_seen_at=now - datetime.timedelta(days=idle_days)))
        db.add(ErrorReport(source="browser", kind="error", fingerprint="f", name="Old", message="",
                           created_at=now - datetime.timedelta(days=40)))
        db.add(ErrorReport(source="browser", kind="error", fingerprint="g", name="New", message=""))
        await db.commit()
    result = await housekeeping.run_once()
    assert result == {"sessions": 2, "error_reports": 1}
    async with temp_db() as db:
        left = sorted((await db.execute(select(Session.session_id))).scalars().all())
        names = (await db.execute(select(ErrorReport.name))).scalars().all()
    assert left == ["edge", "fresh"] and names == ["New"]


async def test_ticket_purge_covers_legacy_rows(tmp_path):
    from database.connection import Database
    from database.repositories.ticket_repo import TicketRepository

    db = Database(str(tmp_path / "t.db"))
    await db.connect()
    try:
        await db.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY, guild_id INTEGER, channel_id INTEGER, "
                         "status TEXT, closed_at TEXT, archived_at TEXT)")
        await db.execute("CREATE TABLE ticket_messages (id INTEGER PRIMARY KEY, ticket_id INTEGER, content TEXT)")
        rows = [
            (1, "archived", "2026-07-24 14:30:34", None),  # legacy: archived_at NULL -> must go
            (2, "archived", "2026-08-10 13:01:09", "2026-10-18 00:52:30"),  # backfilled late -> closed counts
            (3, "archived", "2026-10-29 10:00:00", "2026-10-29 10:00:30"),  # recent -> stays
            (4, "open", None, None),  # open -> never
            (5, "archived", None, None),  # no timestamps at all -> left alone
        ]
        for tid, st, closed, archived in rows:
            await db.execute("INSERT INTO tickets VALUES (?, 1, ?, ?, ?, ?)", (tid, tid * 10, st, closed, archived))
            await db.execute("INSERT INTO ticket_messages (ticket_id, content) VALUES (?, 'x')", (tid,))
        repo = TicketRepository(db)
        cutoff = "2026-09-30 00:00:00"
        listed = sorted(r["id"] for r in await repo.list_archived_before(cutoff))
        deleted = sorted(await repo.purge_archived_before(cutoff))
        assert listed == deleted == [1, 2]
        left = [r[0] for r in await db.fetchall("SELECT id FROM tickets ORDER BY id")]
        msgs = [r[0] for r in await db.fetchall("SELECT ticket_id FROM ticket_messages ORDER BY ticket_id")]
        assert left == [3, 4, 5] and msgs == [3, 4, 5]
    finally:
        await db.close()


# --------------------------------------------------------------------------- SSRF


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8766/guilds/1/settings", "http://localhost:8767/status", "http://[::1]:8080/",
    "http://10.0.0.5/", "http://192.168.1.1/admin", "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/", "http://[::ffff:127.0.0.1]/", "file:///C:/Windows/win.ini", "ftp://example.com/x",
    "http:///nohost", "http://100.64.0.1/",
])
async def test_music_url_check_blocks_internal_targets(url):
    from cogs.music import url_block_reason

    assert await url_block_reason(url) is not None


async def test_music_url_check_resolves_hostnames(monkeypatch):
    import asyncio

    from cogs import music

    answers = {"evil.example": "192.168.0.10", "ok.example": "93.184.216.34"}

    async def fake_getaddrinfo(host, port, **kw):
        if host not in answers:
            raise socket.gaierror("nope")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port))]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    assert await music.url_block_reason("https://evil.example/song.mp3") is not None
    assert await music.url_block_reason("https://ok.example/song.mp3") is None
    assert await music.url_block_reason("https://unknown.example/x") is not None
    # YouTube never needs the lookup.
    assert await music.url_block_reason("https://www.youtube.com/watch?v=abc") is None
    assert await music.url_block_reason("https://youtu.be/abc") is None


async def test_enqueue_never_hands_a_blocked_url_to_ytdlp():
    from types import SimpleNamespace

    from cogs.music import MusicCog

    called = []

    async def extract(*a, **k):
        called.append(a)
        return {"title": "x"}

    fake = SimpleNamespace(_ydl_base={}, _extract_info=extract, song_queue=[])
    assert await MusicCog._enqueue_url(fake, "http://127.0.0.1:8766/health") is None
    assert called == [] and fake.song_queue == []


# --------------------------------------------------------------------------- headers + logs


def test_security_headers_on_api_and_pages(temp_db):
    client = TestClient(server_app.app, base_url="https://testserver")
    for path in ("/api/me", "/health"):
        r = client.get(path)
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
        assert r.headers["strict-transport-security"].startswith("max-age=")
    plain = TestClient(server_app.app).get("/health")
    assert "strict-transport-security" not in plain.headers  # never on plain http


@pytest.mark.parametrize("raw,leak", [
    ("/api/auth/callback?code=SECRETCODE&state=STATEVAL", ("SECRETCODE", "STATEVAL")),
    ("/api/public/appeal-auth/callback?state=S1&code=C1", ("S1", "C1")),
    ("/api/public/appeals/AbCdEfGh12345678", ("AbCdEfGh12345678",)),
    ("/appeal/AbCdEfGh12345678?x=1", ("AbCdEfGh12345678",)),
])
def test_access_log_scrubs_credentials(raw, leak):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", raw, "1.1", 200), None)
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(record)
    line = record.getMessage()
    assert all(secret not in line for secret in leak), line
    assert "<redacted>" in line


# --------------------------------------------------------------------------- !debugurl YouTube only


@pytest.mark.parametrize("target,refused", [
    ("https://www.youtube.com/watch?v=abc", False),
    ("https://youtu.be/abc", False),
    ("lofi hip hop", False),
    ("lofi: chill beats", False),  # a colon in a search is fine
    ("https://evil.example/song", True),
    ("HTTP://evil.example/song", True),  # scheme case must not bypass
    ("https://youtube.com.evil.example/x", True),
    ("http://127.0.0.1:8766/health", True),
    ("file:///C:/Windows/win.ini", True),
    ("ftp://youtube.com/x", True),
])
def test_debugurl_only_accepts_youtube(target, refused, monkeypatch):
    from types import SimpleNamespace

    from cogs import music

    monkeypatch.setattr(music._config, "MUSIC_DEBUGURL_BLOCKED", [], raising=False)
    fake = SimpleNamespace(_is_url=music.MusicCog._is_url)
    assert (music.MusicCog._debugurl_refusal(fake, target) is not None) is refused


def test_debugurl_extractors_exclude_generic():
    import yt_dlp

    from cogs.music import DEBUGURL_EXTRACTORS

    with yt_dlp.YoutubeDL({"quiet": True, "allowed_extractors": DEBUGURL_EXTRACTORS}) as ydl:
        names = {ie.IE_NAME.lower() for ie in ydl._ies.values()}
        assert "generic" not in names and "youtube:search" in names
        assert all(n.startswith("youtube") for n in names)
        with pytest.raises(yt_dlp.utils.DownloadError, match="No suitable extractor"):
            ydl.extract_info("http://127.0.0.1:9/x", download=False)  # refused before any request


def test_tests_never_start_housekeeping_against_the_real_db():
    """The lifespan's retention job uses the REAL engine; tests must keep it off
    (tests/conftest.py). A test run once pruned the live dashboard.db."""
    assert config.HOUSEKEEPING_ENABLED is False
