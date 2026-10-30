"""Internal API WS /ws: authentication, protocol, limits, producers (no network, no Discord)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import discord
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import config
from internal_api.app import create_app
from internal_api.routes import ws as ws_route
from services.live_events import LiveHub

SECRET = config.INTERNAL_API_SECRET
AUTH = {"Authorization": f"Bearer {SECRET}"}
GUILD_ID = 100000000000000001


class FakeBot:
    def __init__(self):
        member = lambda st: SimpleNamespace(status=st)  # noqa: E731
        self.guild = SimpleNamespace(
            id=GUILD_ID, member_count=3,
            members=[member(discord.Status.online), member(discord.Status.idle), member(discord.Status.offline)],
        )
        self.guilds = [self.guild]
        self.intents = SimpleNamespace(presences=True)
        self.latency = 0.042

    def get_guild(self, gid):
        return self.guild if gid == GUILD_ID else None

    def is_ready(self):
        return True


@pytest.fixture
def env(monkeypatch):
    hub = LiveHub(intervals={"stats": 0.05, "bot": 0.05, "diagnostics": 0.05})
    monkeypatch.setattr(ws_route, "hub", hub)
    monkeypatch.setattr(ws_route, "ALLOWED_HOSTS", {"testclient"})
    app = create_app()
    app.state.bot = FakeBot()
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, hub=hub, app=app)


def _connect(env, headers=AUTH, path="/ws"):
    return env.client.websocket_connect(path, headers=headers)


def _recv_until(ws, pred, limit=20):
    for _ in range(limit):
        msg = ws.receive_json()
        if pred(msg):
            return msg
    raise AssertionError("expected message never arrived")


def test_wrong_secret_refused_before_handshake(env):
    with pytest.raises(WebSocketDisconnect) as ei:
        with _connect(env, {"Authorization": "Bearer definitely-wrong"}):
            pass
    assert ei.value.code == ws_route.CLOSE_UNAUTHORIZED


def test_malformed_header_and_empty_secret_refused(env):
    for bad in ("Basic abc", "Bearer ", SECRET):
        with pytest.raises(WebSocketDisconnect):
            with _connect(env, {"Authorization": bad}):
                pass


def test_missing_secret_closed_after_timeout(env, monkeypatch):
    monkeypatch.setattr(ws_route, "AUTH_TIMEOUT", 0.2)
    with _connect(env, {}) as ws:
        t0 = time.monotonic()
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == ws_route.CLOSE_UNAUTHORIZED
        assert time.monotonic() - t0 < 3


def test_first_message_auth_ok_and_wrong(env):
    with _connect(env, {}) as ws:
        ws.send_json({"type": "auth", "secret": SECRET})
        assert ws.receive_json()["type"] == "hello"
    with _connect(env, {}) as ws:
        ws.send_json({"type": "auth", "secret": "nope"})
        with pytest.raises(WebSocketDisconnect) as ei:
            ws.receive_json()
        assert ei.value.code == ws_route.CLOSE_UNAUTHORIZED
    with _connect(env, {}) as ws:
        ws.send_json({"type": "subscribe", "topics": ["stats"]})  # not an auth message
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_secret_comparison_is_constant_time(monkeypatch):
    calls = []
    real = ws_route.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(ws_route.hmac, "compare_digest", spy)
    assert ws_route.secret_ok(SECRET) is True
    assert ws_route.secret_ok("x" * len(SECRET)) is False
    assert ws_route.secret_ok(None) is False and ws_route.secret_ok(123) is False
    assert len(calls) == 2 and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


def test_browser_origin_and_non_loopback_refused(env, monkeypatch):
    with pytest.raises(WebSocketDisconnect):
        with _connect(env, {**AUTH, "Origin": "http://localhost:8080"}):
            pass
    monkeypatch.setattr(ws_route, "ALLOWED_HOSTS", {"127.0.0.1"})  # "testclient" is now a non-loopback peer
    with pytest.raises(WebSocketDisconnect):
        with _connect(env):
            pass


def test_subscribe_publish_ping_unsubscribe(env):
    with _connect(env) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["v"] == 1 and "honeypot" in hello["data"]["topics"]

        ws.send_json({"type": "subscribe", "topics": ["honeypot", "bogus"]})
        ack = _recv_until(ws, lambda m: m["type"] == "ack")
        assert ack["data"]["accepted"] == ["honeypot"] and ack["data"]["rejected"] == ["bogus"]

        env.client.portal.call(env.hub.publish, "honeypot", {"kind": "catch_created", "user_id": 987654321098765432})
        ev = _recv_until(ws, lambda m: m["type"] == "event")
        assert ev["topic"] == "honeypot" and ev["data"]["user_id"] == "987654321098765432"

        ws.send_json({"type": "ping"})
        assert _recv_until(ws, lambda m: m["type"] == "pong")["type"] == "pong"

        ws.send_json({"type": "unsubscribe", "topics": ["honeypot"]})
        _recv_until(ws, lambda m: m["type"] == "ack")
        env.client.portal.call(env.hub.publish, "honeypot", {"kind": "after_unsubscribe"})
        ws.send_json({"type": "ping"})
        seen = [ws.receive_json()]
        assert seen[0]["type"] == "pong"  # the post-unsubscribe event never arrived


def test_bad_messages_get_errors_not_disconnects(env):
    with _connect(env) as ws:
        ws.receive_json()
        ws.send_text("not json")
        assert _recv_until(ws, lambda m: m["type"] == "error")["data"]["code"] == "bad_message"
        ws.send_json({"type": "subscribe", "topics": "stats"})
        assert _recv_until(ws, lambda m: m["type"] == "error")["data"]["code"] == "bad_topics"
        ws.send_json({"type": "explode"})
        assert _recv_until(ws, lambda m: m["type"] == "error")["data"]["code"] == "unknown_type"


def test_oversized_message_closes(env):
    with _connect(env) as ws:
        ws.receive_json()
        ws.send_text("x" * (ws_route.MAX_INBOUND_BYTES + 10))
        with pytest.raises(WebSocketDisconnect) as ei:
            for _ in range(5):
                ws.receive_json()
        assert ei.value.code == ws_route.CLOSE_TOO_BIG


def test_rate_limit_closes(env, monkeypatch):
    monkeypatch.setattr(ws_route, "INBOUND_BURST", 3.0)
    monkeypatch.setattr(ws_route, "INBOUND_RATE_PER_SEC", 0.1)
    with _connect(env) as ws:
        ws.receive_json()
        for _ in range(10):
            ws.send_json({"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as ei:
            for _ in range(15):
                ws.receive_json()
        assert ei.value.code == ws_route.CLOSE_RATE_LIMITED


def test_producers_run_only_while_subscribed_and_use_live_bot(env):
    hub = env.hub
    with _connect(env) as ws:
        ws.receive_json()
        assert hub.running_producers() == set()
        ws.send_json({"type": "subscribe", "topics": ["stats", "bot"]})
        stats = _recv_until(ws, lambda m: m.get("topic") == "stats" and m["type"] == "snapshot")
        assert stats["data"]["total"] == 3 and stats["data"]["online"] == 1 and stats["data"]["guild_id"] == str(GUILD_ID)
        bot = _recv_until(ws, lambda m: m.get("topic") == "bot")
        assert bot["data"]["online"] is True and bot["data"]["latency_ms"] == 42.0
        assert hub.running_producers() == {"stats", "bot"}
    time.sleep(0.2)
    assert hub.running_producers() == set()  # disconnect stopped them
    assert hub.client_count == 0
