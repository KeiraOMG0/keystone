"""Browser endpoint WS /api/ws: origin, cookie/session, mid-stream revalidation, limits."""

from __future__ import annotations

import datetime
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.pool import NullPool
from starlette.websockets import WebSocketDisconnect

from server import config
from server.app import app
from server.auth.dependencies import sign_session_id
from server.db.base import Base
from server.db.models import Session
from server.live import endpoint
from server.live.hub import DashboardHub
from server.services import internal_client
from server.services.internal_client import InternalAPIError

ORIGIN = config.FRONTEND_BASE_URL
USER = 424242424242424242
SID = "SID_SENTINEL_dddddddddddddddd"


def _row(sid=SID, uid=USER, member=True, last_seen=None, roles=()):
    now = datetime.datetime.now(datetime.timezone.utc)
    return Session(
        session_id=sid, discord_user_id=uid, discord_username="mod",
        discord_access_token="ACCESS_SENTINEL", discord_refresh_token="REFRESH_SENTINEL",
        token_expires_at=now, is_guild_member=member, cached_role_ids=list(roles),
        created_at=now, last_seen_at=last_seen or now,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "d.db"
    sync_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(sync_engine)
    with SyncSession(sync_engine) as db:
        db.add(_row())
        db.commit()
    sync_engine.dispose()

    aengine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
    factory = async_sessionmaker(aengine, expire_on_commit=False)
    monkeypatch.setattr(endpoint, "session_factory", lambda: factory())
    monkeypatch.setattr(config, "LIVE_ENABLED", False)  # never dial the real bot from tests
    monkeypatch.setattr(config, "LIVE_REVALIDATE_SECONDS", 0.15)

    hub = DashboardHub()
    monkeypatch.setattr(endpoint, "hub", hub)

    oracle = {"perms": {"moderate_members", "manage_tickets", "view_audit"}, "staff": True, "down": False}

    async def bulk(guild_id, user_id, permissions):
        if oracle["down"]:
            raise InternalAPIError("down")
        return {p: p in oracle["perms"] for p in permissions}

    async def is_staff(guild_id, user_id):
        if oracle["down"]:
            raise InternalAPIError("down")
        return oracle["staff"]

    monkeypatch.setattr(internal_client, "check_permissions_bulk", bulk)
    monkeypatch.setattr(internal_client, "check_is_staff", is_staff)

    def sql(query, *args):
        con = sqlite3.connect(path)
        try:
            con.execute(query, args)
            con.commit()
        finally:
            con.close()

    def add_session(**kw):
        e = create_engine(f"sqlite:///{path}")
        with SyncSession(e) as db:
            db.add(_row(**kw))
            db.commit()
        e.dispose()

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, hub=hub, oracle=oracle, sql=sql, add_session=add_session, path=path)


def headers(origin=ORIGIN, sid=SID, cookie=True):
    h = {}
    if origin is not None:
        h["origin"] = origin
    if cookie:
        h["cookie"] = f"{config.SESSION_COOKIE_NAME}={sign_session_id(sid)}"
    return h


def open_ws(env, **kw):
    return env.client.websocket_connect("/api/ws", headers=headers(**kw))


def recv_until(ws, pred, limit=30):
    for _ in range(limit):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    raise AssertionError("expected message never arrived")


def assert_closed_with(ws, code, limit=30):
    with pytest.raises(WebSocketDisconnect) as ei:
        for _ in range(limit):
            ws.receive_json()
    assert ei.value.code == code


# ---------------------------------------------------------------- handshake


def test_no_cookie_closed_4401(env):
    with open_ws(env, cookie=False) as ws:
        assert_closed_with(ws, 4401)


def test_forged_cookie_closed_4401(env):
    with env.client.websocket_connect(
        "/api/ws", headers={"origin": ORIGIN, "cookie": f"{config.SESSION_COOKIE_NAME}=forged.value"}
    ) as ws:
        assert_closed_with(ws, 4401)


def test_unknown_session_id_closed_4401(env):
    with open_ws(env, sid="does-not-exist") as ws:
        assert_closed_with(ws, 4401)


@pytest.mark.parametrize("bad_origin", [None, "", "https://evil.example", "http://localhost:9999", "null"])
def test_bad_or_missing_origin_closed_4403_even_with_valid_cookie(env, bad_origin):
    with open_ws(env, origin=bad_origin) as ws:
        assert_closed_with(ws, 4403)
    assert env.hub.conns == set()


def test_allowed_origins_are_the_csrf_set(env):
    from server.csrf import allowed_origins

    for origin in allowed_origins():
        with open_ws(env, origin=origin) as ws:
            assert ws.receive_json()["type"] == "hello"


def test_not_a_guild_member_closed_4403(env):
    env.add_session(sid="visitor", uid=7, member=False)
    with open_ws(env, sid="visitor") as ws:
        assert_closed_with(ws, 4403)


def test_expired_session_closed_at_handshake_and_deleted(env):
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=config.SESSION_MAX_AGE_SECONDS + 60)
    env.add_session(sid="old", uid=8, last_seen=old)
    with open_ws(env, sid="old") as ws:
        assert_closed_with(ws, 4401)
    con = sqlite3.connect(env.path)
    try:
        assert con.execute("SELECT COUNT(*) FROM sessions WHERE session_id = 'old'").fetchone()[0] == 0
    finally:
        con.close()


def test_hello_lists_only_authorized_topics(env):
    env.oracle.update(perms={"manage_tickets", "staff_base"}, staff=False)
    with open_ws(env) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["v"] == 1
        assert hello["data"]["topics"] == ["bot", "stats", "tickets"]
        assert hello["data"]["bot"]["online"] is False


# ---------------------------------------------------------------- protocol


def test_subscribe_unsubscribe_ping_and_authorization(env):
    env.oracle.update(perms={"moderate_members"}, staff=False)
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "topics": ["honeypot", "audit", "diagnostics", "nope"]})
        ack = recv_until(ws, lambda m: m["type"] == "ack")
        assert ack["data"]["accepted"] == ["honeypot"]
        assert set(ack["data"]["denied"]) == {"audit", "diagnostics"} and ack["data"]["unknown"] == ["nope"]

        env.client.portal.call(env.hub.on_upstream_message, {"type": "event", "topic": "honeypot", "data": {"kind": "catch_created"}})
        env.client.portal.call(env.hub.on_upstream_message, {"type": "event", "topic": "audit", "data": {"kind": "audit"}})
        ev = recv_until(ws, lambda m: m["type"] == "event")
        assert ev["topic"] == "honeypot" and ev["data"]["kind"] == "catch_created"

        ws.send_json({"type": "ping"})
        seen = []
        msg = recv_until(ws, lambda m: m["type"] == "pong" or seen.append(m))
        assert msg["type"] == "pong" and all(m.get("topic") != "audit" for m in seen)  # audit never delivered

        ws.send_json({"type": "unsubscribe", "topics": ["honeypot"]})
        recv_until(ws, lambda m: m["type"] == "ack")
        assert env.hub.refcount("honeypot") == 0


def test_bad_json_and_unknown_types_do_not_disconnect(env):
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_text("{oops")
        assert recv_until(ws, lambda m: m["type"] == "error")["data"]["code"] == "bad_message"
        ws.send_json({"type": "subscribe", "topics": ["a"] * 100})
        assert recv_until(ws, lambda m: m["type"] == "error")["data"]["code"] == "bad_topics"
        ws.send_json({"type": "nope"})
        assert recv_until(ws, lambda m: m["type"] == "error")["data"]["code"] == "unknown_type"


def test_message_size_cap_closes_1009(env, monkeypatch):
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_text("x" * (endpoint.MAX_MESSAGE_BYTES + 1))
        assert_closed_with(ws, 1009)


def test_binary_frames_closed(env):
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_bytes(b"\x00\x01")
        assert_closed_with(ws, 1003)


def test_rate_limit_closes_4429(env, monkeypatch):
    monkeypatch.setattr(endpoint, "RATE_BURST", 3.0)
    monkeypatch.setattr(endpoint, "RATE_PER_SEC", 0.1)
    with open_ws(env) as ws:
        ws.receive_json()
        for _ in range(12):
            ws.send_json({"type": "ping"})
        assert_closed_with(ws, 4429)


def test_idle_timeout_closes_4408(env, monkeypatch):
    monkeypatch.setattr(endpoint, "IDLE_TIMEOUT_SECONDS", 0.3)
    with open_ws(env) as ws:
        ws.receive_json()
        assert_closed_with(ws, 4408)


def test_per_user_connection_cap(env, monkeypatch):
    monkeypatch.setattr(endpoint, "MAX_CONNECTIONS_PER_USER", 2)
    with open_ws(env) as a, open_ws(env) as b:
        a.receive_json(), b.receive_json()
        with open_ws(env) as c:
            assert_closed_with(c, 4429)


# ---------------------------------------------------------------- mid-stream


def test_deleted_session_closes_socket_mid_stream(env):
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "topics": ["stats"]})
        recv_until(ws, lambda m: m["type"] == "ack")
        env.sql("DELETE FROM sessions WHERE session_id = ?", SID)
        t0 = time.monotonic()
        assert_closed_with(ws, 4401)
        assert time.monotonic() - t0 < 5
    assert env.hub.conns == set() and env.hub.refcount("stats") == 0


def test_expired_session_closes_socket_mid_stream(env):
    with open_ws(env) as ws:
        ws.receive_json()
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=config.SESSION_MAX_AGE_SECONDS + 60)).isoformat(sep=" ")
        env.sql("UPDATE sessions SET last_seen_at = ? WHERE session_id = ?", old, SID)
        assert_closed_with(ws, 4401)


def test_demoted_user_loses_topic_mid_stream(env):
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "topics": ["tickets", "honeypot"]})
        recv_until(ws, lambda m: m["type"] == "ack")
        env.client.portal.call(env.hub.on_upstream_message, {"type": "event", "topic": "tickets", "data": {"kind": "opened"}})
        assert recv_until(ws, lambda m: m["type"] == "event")["topic"] == "tickets"

        env.oracle.update(perms={"moderate_members"}, staff=True)  # lost manage_tickets
        err = recv_until(ws, lambda m: m["type"] == "error" and m["topic"] == "tickets")
        assert err["data"] == {"code": "forbidden", "reason": "permission_revoked"}
        env.client.portal.call(env.hub.on_upstream_message, {"type": "event", "topic": "tickets", "data": {"kind": "closed"}})
        env.client.portal.call(env.hub.on_upstream_message, {"type": "event", "topic": "honeypot", "data": {"kind": "catch_created"}})
        nxt = recv_until(ws, lambda m: m["type"] == "event")
        assert nxt["topic"] == "honeypot"  # the demoted topic is gone, the still-allowed one continues
        ws.send_json({"type": "subscribe", "topics": ["tickets"]})
        assert recv_until(ws, lambda m: m["type"] == "ack")["data"]["denied"] == ["tickets"]


def test_logout_closes_sockets_immediately(env):
    with open_ws(env) as ws:
        ws.receive_json()
        env.client.portal.call(env.hub.close_session, SID)
        assert_closed_with(ws, 4401)


def test_transient_bot_outage_does_not_close_or_revoke(env):
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "topics": ["honeypot"]})
        recv_until(ws, lambda m: m["type"] == "ack")
        env.oracle["down"] = True  # bot unreachable during revalidation
        time.sleep(0.5)  # several revalidation rounds
        env.client.portal.call(env.hub.on_upstream_message, {"type": "event", "topic": "honeypot", "data": {"kind": "x"}})
        assert recv_until(ws, lambda m: m["type"] == "event")["topic"] == "honeypot"


# ---------------------------------------------------------------- diagnostics stream


def test_diagnostics_stream_staff_only_and_secret_free(env, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_SECRET", "planted-wire-secret-VALUE")
    env.hub.diagnostics_interval = 0.1
    env.hub.upstream = SimpleNamespace(connected=True, rtt_ms=2.0, set_topics=lambda t: None)
    env.client.portal.call(
        env.hub.on_upstream_message,
        {"type": "snapshot", "topic": "diagnostics", "data": {"process": {"pid": 5}, "leak": "planted-wire-secret-VALUE"}},
    )
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "topics": ["diagnostics"]})
        recv_until(ws, lambda m: m["type"] == "ack")
        snap = recv_until(ws, lambda m: m["type"] == "snapshot" and m["topic"] == "diagnostics")
        assert snap["data"]["bot"]["reachable"] is True and snap["data"]["bot"]["process"]["pid"] == 5
        assert snap["data"]["viewer"]["username"] == "mod"
        assert "planted-wire-secret-VALUE" not in str(snap)
        assert "ACCESS_SENTINEL" not in str(snap) and SID not in str(snap)
        assert env.hub.diagnostics_running
    time.sleep(0.4)
    assert not env.hub.diagnostics_running  # last subscriber left -> loop stopped

    env.oracle.update(staff=False)
    env.hub.forget_permissions()
    with open_ws(env) as ws:
        ws.receive_json()
        ws.send_json({"type": "subscribe", "topics": ["diagnostics"]})
        ack = recv_until(ws, lambda m: m["type"] == "ack")
        assert ack["data"]["denied"] == ["diagnostics"] and not env.hub.diagnostics_running
