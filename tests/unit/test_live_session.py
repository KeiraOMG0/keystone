"""Stay-logged-in facts: throttled sliding expiry, restart-safe cookie signing, lifetimes."""

from __future__ import annotations

import datetime

import pytest
from itsdangerous import URLSafeSerializer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from server import config
from server.auth import dependencies as deps
from server.auth.session import create_session, get_session, touch_session
from server.db.base import Base

pytestmark = pytest.mark.asyncio


def n(dt):
    return dt.replace(tzinfo=None)


def utc(**delta):
    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(**delta)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _make(db, last_seen):
    s = await create_session(
        db, discord_user_id=1, discord_username="u", discord_avatar=None, discord_access_token="a",
        discord_refresh_token="r", token_expires_at=utc(), is_guild_member=True, cached_role_ids=[],
    )
    s.last_seen_at = last_seen
    await db.commit()
    return s


async def test_touch_is_throttled(factory):
    async with factory() as db:
        s = await _make(db, utc(seconds=10))
        before = s.last_seen_at
        s2 = await touch_session(db, s)
        assert n(s2.last_seen_at) == n(before)  # inside the throttle window: no write
        reread = await get_session(db, s.session_id)
        assert reread.last_seen_at.replace(tzinfo=None) == before.replace(tzinfo=None)


async def test_touch_writes_after_interval_and_when_forced(factory, monkeypatch):
    async with factory() as db:
        s = await _make(db, utc(seconds=config.SESSION_TOUCH_INTERVAL_SECONDS + 30))
        old = s.last_seen_at
        s = await touch_session(db, s)
        assert n(s.last_seen_at) > n(old)
        stamp = s.last_seen_at
        s = await touch_session(db, s)
        assert n(s.last_seen_at) == n(stamp)  # just written: throttled again
        s = await touch_session(db, s, force=True)
        assert n(s.last_seen_at) > n(stamp)


async def test_loader_keeps_session_within_30_days_and_expires_after(factory):
    assert config.SESSION_MAX_AGE_SECONDS == 30 * 24 * 3600  # documented default (no env override in .env)
    async with factory() as db:
        fresh = await _make(db, utc(days=29))
        cookie = deps.sign_session_id(fresh.session_id)
        assert await deps.load_valid_session(db, cookie) is not None
        stale = await _make(db, utc(days=31))
        assert await deps.load_valid_session(db, deps.sign_session_id(stale.session_id)) is None
        assert await get_session(db, stale.session_id) is None  # expired rows are deleted
        assert await deps.load_valid_session(db, None) is None
        assert await deps.load_valid_session(db, "forged") is None


async def test_cookie_signing_is_restart_safe():
    """The signing key comes from .env (SESSION_COOKIE_SECRET), never generated per process."""
    cookie = deps.sign_session_id("session-abc")
    after_restart = URLSafeSerializer(config.SESSION_COOKIE_SECRET, salt="dashboard-session-cookie")
    assert after_restart.loads(cookie) == "session-abc"
    assert deps.unsign_session_id(cookie) == "session-abc"
    assert deps.unsign_session_id(cookie + "x") is None


async def test_cookie_outlives_sliding_window_and_httponly_lax_kept():
    # The cookie must not be the thing that logs a daily user out after 30 days.
    assert config.SESSION_COOKIE_MAX_AGE_SECONDS > config.SESSION_MAX_AGE_SECONDS
    import inspect

    from server.auth import oauth

    src = inspect.getsource(oauth.callback)
    assert "SESSION_COOKIE_MAX_AGE_SECONDS" in src and "httponly=True" in src and 'samesite="lax"' in src
    assert "secure=cookie_secure(request)" in src  # env-driven (auto/true/false), see test_dashboard_auth_hardening.py


async def test_api_me_wire_contract_the_frontend_classifier_relies_on(monkeypatch):
    """Only 401 {"error":"unauthenticated"} logs out; a bot outage is a 502 JSON that stays transient."""
    from fastapi.testclient import TestClient

    from server.app import app
    from server.auth.dependencies import require_session
    from server.services import internal_client
    from server.services.internal_client import InternalAPIError

    c = TestClient(app)
    r = c.get("/api/me")
    assert r.status_code == 401 and r.json()["detail"] == {"error": "unauthenticated"}

    async def down(*a, **k):
        raise InternalAPIError("down")

    monkeypatch.setattr(internal_client, "check_permissions_bulk", down)
    app.dependency_overrides[require_session] = lambda: type(
        "S", (), {"is_guild_member": True, "discord_user_id": 1}
    )()
    try:
        r = c.get("/api/me")
    finally:
        app.dependency_overrides.pop(require_session, None)
    assert r.status_code == 502 and r.json()["detail"] == {"error": "internal_api_unavailable"}
