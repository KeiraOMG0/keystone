"""Error reporting + frontend caching (2026-10-30).

* Cache headers: hashed /assets are immutable for a year, index.html (SPA
  fallback) is no-cache, a missing asset is a no-store 404.
* POST /api/public/client-errors: public, rate limited, size-capped, scrubbed,
  user taken from the session (never the body), no IP stored.
* ErrorCaptureMiddleware records unhandled exceptions by route TEMPLATE and
  still answers 500.
* error_reports grouping / clear / pruning, and core/error_ring.py (the bot's
  in-memory ERROR log ring) plus its internal-route scrubber.
"""

from __future__ import annotations

import datetime
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.app as server_app
from server import config
from server.auth.dependencies import get_current_session
from server.auth.oauth import limiter
from server.db import engine as db_engine
from server.db.base import Base
from server.db.models import ErrorReport, Session
from server.services import error_reports

ORIGIN = config.FRONTEND_BASE_URL
INDEX = "<!doctype html><title>spa-index</title><div id=root></div>"


@pytest.fixture(autouse=True)
async def temp_db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'd.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_engine, "async_session_factory", factory)
    monkeypatch.setattr(error_reports, "_last_prune", 0.0)
    limiter.reset()
    yield factory
    limiter.reset()
    server_app.app.dependency_overrides.pop(get_current_session, None)
    await engine.dispose()


async def _rows(factory) -> list[ErrorReport]:
    async with factory() as db:
        return list((await db.execute(select(ErrorReport).order_by(ErrorReport.id))).scalars().all())


# --------------------------------------------------------------------------- caching


@pytest.fixture
def dist_client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX, encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    monkeypatch.setattr(server_app, "_FRONTEND_DIST", dist)
    return TestClient(server_app.create_app())


def test_hashed_assets_are_immutable(dist_client):
    r = dist_client.get("/assets/index-abc123.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_missing_asset_is_uncached_404(dist_client):
    r = dist_client.get("/assets/index-gone999.js")
    assert r.status_code == 404
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/", "/dashboard", "/dashboard/debug", "/index.html", "/appeal/sometoken123"])
def test_index_html_is_no_cache(dist_client, path):
    r = dist_client.get(path)
    assert r.status_code == 200 and "spa-index" in r.text
    assert r.headers["cache-control"] == "no-cache"


def test_root_files_cached_one_day(dist_client):
    r = dist_client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "public, max-age=86400"


# --------------------------------------------------------------------------- public report endpoint


@pytest.fixture
def client():
    server_app.app.dependency_overrides[get_current_session] = lambda: None
    return TestClient(server_app.app)


def _post(client, body, ip="203.0.113.50", **headers):
    return client.post(
        "/api/public/client-errors", json=body,
        headers={"Origin": ORIGIN, "CF-Connecting-IP": ip, "User-Agent": "TestPhone/1.0", **headers},
    )


async def test_report_is_stored_scrubbed(client, temp_db):
    r = _post(client, {
        "kind": "render",
        "name": "TypeError",
        "message": "x is undefined at /appeal/abcdefgh12345678?code=SECRETCODE",
        "stack": "TypeError: x\n    at f (https://dash.idlesys.xyz/assets/index-1.js:1:2)",
        "component_stack": "\n    at Page",
        "path": "/appeal/abcdefgh12345678?state=XYZ",
        "user_id": "1",  # ignored: identity only ever comes from the session
    })
    assert r.status_code == 204 and r.content == b""
    [row] = await _rows(temp_db)
    assert row.source == "browser" and row.kind == "render" and row.name == "TypeError"
    assert "abcdefgh12345678" not in row.message and "SECRETCODE" not in row.message
    assert row.path == "/appeal/:token"
    assert row.user_agent == "TestPhone/1.0"
    assert row.user_id is None and row.username is None
    assert "203.0.113.50" not in " ".join(str(v) for v in vars(row).values())


async def test_report_attaches_session_user(client, temp_db):
    s = Session()
    s.discord_user_id, s.discord_username = 500000000000000005, "owner"
    server_app.app.dependency_overrides[get_current_session] = lambda: s
    assert _post(client, {"kind": "error", "name": "Error", "message": "boom"}).status_code == 204
    [row] = await _rows(temp_db)
    assert row.user_id == 500000000000000005 and row.username == "owner"


@pytest.mark.parametrize("body", [{"kind": "nuke", "message": "x"}, {"message": "no kind"}, [], "str"])
async def test_report_rejects_bad_bodies(client, temp_db, body):
    r = _post(client, body)
    assert r.status_code == 400 and r.json() == {"error": "invalid_request"}
    assert await _rows(temp_db) == []


async def test_report_size_cap(client, temp_db):
    r = _post(client, {"kind": "error", "message": "x", "stack": "y" * 40_000})
    assert r.status_code in (400, 413)
    assert await _rows(temp_db) == []


def test_report_rate_limited_per_ip(client):
    codes = [_post(client, {"kind": "error", "message": f"m{i}"}).status_code for i in range(21)]
    assert codes == [204] * 20 + [429]
    assert _post(client, {"kind": "error", "message": "x"}).json() == {"error": "rate_limited"}
    assert _post(client, {"kind": "error", "message": "x"}, ip="203.0.113.51").status_code == 204


def test_report_csrf_origin_check(client):
    r = _post(client, {"kind": "error", "message": "x"}, Origin="https://evil.example")
    assert r.status_code == 403 and r.json() == {"error": "csrf_check_failed"}


# --------------------------------------------------------------------------- server exceptions


async def test_unhandled_exception_recorded_by_route_template(tmp_path, monkeypatch, temp_db):
    monkeypatch.setattr(server_app, "_FRONTEND_DIST", tmp_path / "no-dist")
    app = server_app.create_app()

    async def boom(token: str):
        raise RuntimeError(f"kaboom {token}")

    app.add_api_route("/boom/{token}", boom)
    r = TestClient(app, raise_server_exceptions=False).get("/boom/abc")
    assert r.status_code == 500
    [row] = await _rows(temp_db)
    assert row.source == "server" and row.kind == "exception" and row.name == "RuntimeError"
    assert row.path == "GET /boom/{token}"
    assert "Traceback" in (row.stack or "")


# --------------------------------------------------------------------------- grouping / clear / prune


async def test_grouping_and_clear(temp_db):
    for i in range(3):
        await error_reports.record(source="browser", kind="error", name="TypeError", message=f"bad {i}",
                                   stack="TypeError: bad\n    at f (x.js:1:2)", path=f"/dashboard/p{i}")
    await error_reports.record(source="server", kind="exception", name="KeyError", message="'k'")
    groups = await error_reports.list_groups()
    assert [g["source"] for g in groups] == ["server", "browser"]  # newest group first
    browser = groups[1]
    assert browser["count"] == 3 and len(browser["paths"]) == 3
    assert browser["first_seen"] <= browser["last_seen"]
    assert await error_reports.clear(fingerprint_id=browser["id"]) == 3
    assert await error_reports.clear(source="server") == 1
    assert await error_reports.list_groups() == []


async def test_prune_keeps_newest_rows(temp_db, monkeypatch):
    monkeypatch.setattr(error_reports, "ERROR_REPORT_MAX_ROWS", 3)
    for i in range(5):
        await error_reports.record(source="browser", kind="error", name="E", message=f"m{i}")
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=40)
    async with temp_db() as db:
        db.add(ErrorReport(source="browser", kind="error", fingerprint="f", name="Old", message="",
                           created_at=old))
        await db.commit()
    await error_reports.prune()
    rows = await _rows(temp_db)
    assert [r.message for r in rows] == ["m2", "m3", "m4"]  # 40-day-old row gone, then newest 3 kept
    assert all(r.name != "Old" for r in rows)


# --------------------------------------------------------------------------- bot ring


def test_error_ring_groups_error_records():
    from core.error_ring import ErrorRing, ErrorRingHandler

    ring = ErrorRing()
    logger = logging.getLogger("test.error_ring")
    handler = ErrorRingHandler(ring)
    logger.addHandler(handler)
    try:
        logger.warning("just a warning")
        for i in range(3):
            try:
                raise ValueError(f"bad value {i}")
            except ValueError:
                logger.exception("Ignoring exception in command %s", "ping")
        logger.error("plain error")
    finally:
        logger.removeHandler(handler)
    groups = ring.snapshot()
    assert [g["name"] for g in groups] == ["ERROR", "ValueError"]  # newest first, warning ignored
    value_error = groups[1]
    assert value_error["source"] == "bot" and value_error["count"] == 3
    assert "Traceback" in value_error["sample"]["stack"]
    assert value_error["sample"]["logger"] == "test.error_ring"
    assert ring.clear(value_error["id"]) == 1 and ring.clear() == 1 and ring.snapshot() == []


def test_error_ring_is_bounded(monkeypatch):
    import core.error_ring as er

    monkeypatch.setattr(er, "MAX_GROUPS", 5)
    ring = er.ErrorRing()
    logger = logging.getLogger("test.error_ring.bound")
    handler = er.ErrorRingHandler(ring)
    logger.addHandler(handler)
    try:
        for i in range(8):
            logger.error("distinct %s", chr(ord("a") + i))
    finally:
        logger.removeHandler(handler)
    assert len(ring.snapshot()) == 5


def test_setup_logging_installs_ring_handler():
    from core.error_ring import ErrorRingHandler
    from core.logging_setup import setup_logging

    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        setup_logging("INFO")
        assert sum(isinstance(h, ErrorRingHandler) for h in root.handlers) == 1
    finally:
        for h in root.handlers:
            if h not in saved:
                h.close()
        root.handlers[:] = saved
        root.setLevel(level)


def test_internal_errors_route_scrubs():
    from internal_api.routes.debug import _scrub_error_group

    group = {
        "id": "x", "message": "GET /api/public/appeals/abcdefgh12345678?code=SECRET failed",
        "sample": {"stack": "Authorization: Bearer abcdefghijklmnop\nline2"},
    }
    out = _scrub_error_group(group)
    assert "abcdefgh12345678" not in out["message"] and "SECRET" not in out["message"]
    assert "abcdefghijklmnop" not in out["sample"]["stack"]
