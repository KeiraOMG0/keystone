"""Dashboard -> bot upstream client: reconnect-forever, backoff, resubscribe, bot restart."""

from __future__ import annotations

import asyncio
import json

import pytest

from server.live.hub import DashboardHub
from server.live.upstream import UpstreamClient, backoff_delay

pytestmark = pytest.mark.asyncio

CLOSE = object()


class FakeWS:
    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.incoming.get()
        if item is CLOSE:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item


class FakeConnect:
    """Callable standing in for websockets' connect(): one scripted outcome per attempt."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.kwargs: list[dict] = []
        self.sockets: list[FakeWS] = []

    def __call__(self, url, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        outcome = self.script.pop(0) if self.script else "ok"
        return _Ctx(self, outcome)


class _Ctx:
    def __init__(self, parent, outcome):
        self.parent, self.outcome = parent, outcome

    async def __aenter__(self):
        if self.outcome == "refuse":
            raise ConnectionRefusedError("connection refused")
        ws = FakeWS()
        self.parent.sockets.append(ws)
        return ws

    async def __aexit__(self, *exc):
        return False


async def _until(pred, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def test_backoff_grows_is_capped_and_jittered():
    assert backoff_delay(0, base=0.5, cap=15, rand=lambda: 1.0) == 0.5
    assert backoff_delay(3, base=0.5, cap=15, rand=lambda: 1.0) == 4.0
    assert backoff_delay(50, base=0.5, cap=15, rand=lambda: 1.0) == 15.0  # capped
    assert backoff_delay(50, base=0.5, cap=15, rand=lambda: 0.0) == 7.5  # jitter floor: half
    for attempt in range(0, 40):
        d = backoff_delay(attempt)
        assert 0 < d <= 15.0


async def test_retries_forever_then_connects_sends_bearer_and_subscriptions():
    delays: list[float] = []

    async def fast_sleep(d):
        delays.append(d)
        await asyncio.sleep(0)

    connect = FakeConnect(["refuse"] * 30 + ["ok"])
    states: list[bool] = []
    client = UpstreamClient(
        "ws://127.0.0.1:1/ws", "s3cret", on_message=lambda m: None, on_state=states.append,
        connect=connect, sleep=fast_sleep, rand=lambda: 1.0, backoff_cap=15.0,
    )
    client.set_topics({"stats", "honeypot"})
    client.start()
    await _until(lambda: client.connected)
    assert connect.calls == 31  # 30 refusals did not stop it
    assert max(delays) <= 15.0 and delays[:4] == [0.5, 1.0, 2.0, 4.0] and delays[-1] == 15.0
    assert connect.kwargs[-1]["additional_headers"] == {"Authorization": "Bearer s3cret"}
    assert connect.kwargs[-1]["proxy"] is None
    await _until(lambda: any(m.get("type") == "subscribe" for m in connect.sockets[0].sent))
    sub = next(m for m in connect.sockets[0].sent if m["type"] == "subscribe")
    assert sub["topics"] == ["honeypot", "stats"]
    assert states == [True]
    await client.stop()
    assert states == [True, False]


async def test_reconnect_replays_subscriptions_and_reports_state():
    async def fast_sleep(d):
        await asyncio.sleep(0)

    connect = FakeConnect(["ok", "refuse", "refuse", "ok"])
    states: list[bool] = []
    got: list[dict] = []
    client = UpstreamClient(
        "ws://x/ws", "s", on_message=got.append, on_state=states.append, connect=connect, sleep=fast_sleep,
    )
    client.set_topics({"diagnostics"})
    client.start()
    await _until(lambda: client.connected and connect.sockets)
    connect.sockets[0].incoming.put_nowait(json.dumps({"type": "event", "topic": "honeypot", "data": {"k": 1}}))
    connect.sockets[0].incoming.put_nowait("not json")  # ignored
    await _until(lambda: len(got) == 1)
    connect.sockets[0].incoming.put_nowait(CLOSE)  # the bot went away
    await _until(lambda: states == [True, False, True])
    assert connect.calls == 4 and len(connect.sockets) == 2
    await _until(lambda: any(m.get("type") == "subscribe" for m in connect.sockets[1].sent))
    assert next(m for m in connect.sockets[1].sent if m["type"] == "subscribe")["topics"] == ["diagnostics"]
    client.set_topics(set())  # last diagnostics subscriber left
    await _until(lambda: any(m.get("type") == "unsubscribe" for m in connect.sockets[1].sent))
    await client.stop()


async def test_handler_exceptions_do_not_kill_the_connection():
    async def fast_sleep(d):
        await asyncio.sleep(0)

    def bad(_msg):
        raise RuntimeError("handler bug")

    connect = FakeConnect(["ok"])
    client = UpstreamClient("ws://x/ws", "s", on_message=bad, connect=connect, sleep=fast_sleep)
    client.start()
    await _until(lambda: client.connected and connect.sockets)
    connect.sockets[0].incoming.put_nowait(json.dumps({"type": "event", "topic": "x", "data": {}}))
    await asyncio.sleep(0.05)
    assert client.connected
    await client.stop()


async def test_bot_restart_dashboard_reconnects_and_rebroadcasts_bot_connected():
    """Real websockets server on an ephemeral loopback port, killed and restarted."""
    from websockets.asyncio.server import serve

    seen_auth: list[str] = []

    async def handler(ws):
        seen_auth.append(ws.request.headers.get("Authorization", ""))
        await ws.send(json.dumps({"v": 1, "type": "hello", "topic": None, "ts": 0, "seq": 0, "data": {}}))
        async for _ in ws:
            pass

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    hub = DashboardHub()
    conn_events: list[dict] = []
    from server.live.hub import Connection

    browser = Connection("sid", 1, "u", {"bot"})
    hub.add_connection(browser)
    hub.subscribe(browser, ["bot"])
    client = UpstreamClient(
        f"ws://127.0.0.1:{port}/ws", "planted-secret",
        on_message=hub.on_upstream_message, on_state=hub.on_upstream_state,
        backoff_base=0.02, backoff_cap=0.1,
    )
    hub.upstream = client
    client.start()
    try:
        await _until(lambda: client.connected, 5)
        assert seen_auth[0] == "Bearer planted-secret"

        server.close()  # kill the fake bot
        await server.wait_closed()
        await _until(lambda: not client.connected, 5)
        assert hub.bot_state["online"] is False

        server2 = await serve(handler, "127.0.0.1", port)  # bot restarted
        try:
            await _until(lambda: client.connected, 8)
            assert hub.bot_state["online"] is True
        finally:
            server2.close()
            await server2.wait_closed()
    finally:
        await client.stop()

    while not browser.queue.empty():
        conn_events.append(browser.queue.get_nowait())
    online_flags = [m["data"]["online"] for m in conn_events if m["topic"] == "bot"]
    assert online_flags[0] is False  # initial snapshot on subscribe
    assert True in online_flags[1:] and False in online_flags[1:]  # connected -> disconnected -> connected
    assert online_flags[-1] in (True, False)
    assert online_flags.count(True) >= 2  # "bot connected" rebroadcast after the restart
