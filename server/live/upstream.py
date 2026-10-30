"""
The dashboard backend's ONE persistent WebSocket connection to the bot's
internal API (`ws://127.0.0.1:8766/ws`).

  * This process is the client; the bot never dials out.
  * Authenticated with `Authorization: Bearer <INTERNAL_API_SECRET>` on the
    upgrade request (same shared secret as the HTTP API).
  * Reconnects FOREVER with exponential backoff + jitter (capped, default 15 s),
    so a bot restart heals by itself; nothing here ever gives up.
  * Heartbeat: library-level ping/pong (dead-peer detection) plus an
    application `{"type":"ping"}` whose pong gives a round-trip time.
  * Topic subscriptions are declarative: `set_topics()` records the desired set
    and the connection loop replays/diffs it (so a reconnect re-subscribes).
  * Exposes `connected` plus `on_state(bool)` so the hub can broadcast "bot
    connected/disconnected".
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

MessageHandler = Callable[[dict], None]
StateHandler = Callable[[bool], None]

MAX_MESSAGE_BYTES = 8 * 1024 * 1024  # a full diagnostics dump can be a few hundred KB
STABLE_AFTER_SECONDS = 5.0  # a connection that lived this long resets the backoff


def backoff_delay(attempt: int, *, base: float = 0.5, cap: float = 15.0, rand: Callable[[], float] = random.random) -> float:
    """Exponential backoff with jitter: min(cap, base*2^attempt), scaled into
    [50%, 100%] of that so a fleet of reconnecting clients does not sync up."""
    ceiling = min(cap, base * (2 ** min(attempt, 30)))
    return ceiling * (0.5 + 0.5 * rand())


def _default_connect(url: str, **kwargs):
    from websockets.asyncio.client import connect

    return connect(url, **kwargs)


class UpstreamClient:
    def __init__(
        self,
        url: str,
        secret: str,
        *,
        on_message: MessageHandler,
        on_state: StateHandler | None = None,
        connect: Callable[..., Any] | None = None,
        backoff_base: float = 0.5,
        backoff_cap: float = 15.0,
        ping_interval: float = 15.0,
        rand: Callable[[], float] = random.random,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._url = url
        self._secret = secret
        self._on_message = on_message
        self._on_state = on_state
        self._connect = connect or _default_connect
        self._base = backoff_base
        self._cap = backoff_cap
        self._ping_interval = ping_interval
        self._rand = rand
        self._sleep = sleep
        self._desired: set[str] = set()
        self._dirty: asyncio.Event | None = None  # created in run() (bound to the serving loop)
        self._task: asyncio.Task | None = None
        self.connected = False
        self.rtt_ms: float | None = None
        self.attempts = 0  # consecutive failed attempts (observability / tests)
        self.connects = 0  # total successful connections

    # -- public ------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.run(), name="live-upstream")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self._set_connected(False)

    def set_topics(self, topics: set[str]) -> None:
        if set(topics) != self._desired:
            self._desired = set(topics)
            if self._dirty is not None:
                self._dirty.set()

    @property
    def desired_topics(self) -> set[str]:
        return set(self._desired)

    # -- internals ---------------------------------------------------------
    def _set_connected(self, value: bool) -> None:
        if self.connected == value:
            return
        self.connected = value
        if value:
            self.connects += 1
        else:
            self.rtt_ms = None
        if self._on_state is not None:
            try:
                self._on_state(value)
            except Exception:
                log.exception("upstream on_state handler failed")

    async def run(self) -> None:
        """Connect/serve/reconnect loop. Retries forever; only cancellation ends it."""
        self._dirty = asyncio.Event()
        while True:
            opened_at: float | None = None
            try:
                headers = {"Authorization": f"Bearer {self._secret}"}
                async with self._connect(
                    self._url,
                    additional_headers=headers,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_interval,
                    open_timeout=5,
                    max_size=MAX_MESSAGE_BYTES,
                    proxy=None,  # never route a loopback connection through an env proxy
                ) as ws:
                    opened_at = time.monotonic()
                    self._dirty.set()  # (re)send the desired subscriptions
                    self._set_connected(True)
                    await self._serve(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Message only: never log headers (they carry the secret).
                if self.attempts in (0, 1) or self.attempts % 20 == 0:
                    log.info("live upstream unavailable (%s: %s); retrying", type(exc).__name__, exc)
            finally:
                self._set_connected(False)
            if opened_at is not None and time.monotonic() - opened_at >= STABLE_AFTER_SECONDS:
                self.attempts = 0
            delay = backoff_delay(self.attempts, base=self._base, cap=self._cap, rand=self._rand)
            self.attempts += 1
            await self._sleep(delay)

    async def _serve(self, ws) -> None:
        sent: set[str] = set()
        ping_sent_at: list[float | None] = [None]

        async def reader() -> None:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "pong":
                    if ping_sent_at[0] is not None:
                        self.rtt_ms = round((time.monotonic() - ping_sent_at[0]) * 1000, 1)
                        ping_sent_at[0] = None
                    continue
                try:
                    self._on_message(msg)
                except Exception:
                    log.exception("upstream message handler failed")

        async def writer() -> None:
            while True:
                try:
                    await asyncio.wait_for(self._dirty.wait(), self._ping_interval)
                except asyncio.TimeoutError:
                    ping_sent_at[0] = time.monotonic()
                    await ws.send(json.dumps({"type": "ping"}))
                    continue
                self._dirty.clear()
                want = set(self._desired)
                add, drop = sorted(want - sent), sorted(sent - want)
                if add:
                    await ws.send(json.dumps({"type": "subscribe", "topics": add}))
                if drop:
                    await ws.send(json.dumps({"type": "unsubscribe", "topics": drop}))
                sent.clear()
                sent.update(want)
                if add:  # measure RTT right after (re)connecting
                    ping_sent_at[0] = time.monotonic()
                    await ws.send(json.dumps({"type": "ping"}))

        tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                exc = t.exception()
                if exc is not None:
                    raise exc
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except BaseException:
                    pass
