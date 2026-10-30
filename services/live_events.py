"""
Bot-side live-event fan-out hub (the bot half of the dashboard's WebSocket
live updates; see docs/ARCHITECTURE.md "Live updates (WebSocket)").

The bot NEVER dials out. The dashboard backend (server/live/upstream.py) is the
only WebSocket client: it connects to the internal API's `/ws` route
(internal_api/routes/ws.py), which registers a `LiveClient` here. Service code
calls `publish(topic, data)` at real mutation points; the hub fans the message
out to every client subscribed to that topic.

Guarantees
----------
* `publish()` NEVER raises and never blocks (no awaits, `put_nowait` only). A
  broken websocket, a full queue or an unserialisable payload must never break a
  moderation action, a ticket close or the honeypot.
* Every client has a bounded queue (`QUEUE_MAX`). On overflow the OLDEST message
  is dropped, so a stalled consumer costs a bounded amount of memory.
* Snowflakes leave as strings (`internal_api.snowflakes.stringify_snowflakes`).
* `diagnostics` payloads are always passed through the diagnostics service's
  secret scrubber; if scrubbing fails the message is dropped, never sent raw.
* "Producer" topics (`bot`, `stats`, `diagnostics`) are polled snapshots. Their
  producer task exists ONLY while at least one client is subscribed, so the
  (comparatively heavy) diagnostics collector does not run when nobody watches.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

QUEUE_MAX = 256
PROTOCOL_VERSION = 1

# Topics the bot can emit. Authorization is NOT decided here (the bot trusts
# anyone holding the internal secret); the dashboard backend gates each topic
# per browser connection.
EVENT_TOPICS: tuple[str, ...] = ("honeypot", "tickets", "moderation", "audit", "roles", "automation", "birthdays", "music")
PRODUCER_TOPICS: tuple[str, ...] = ("bot", "stats", "diagnostics")
ALL_TOPICS: tuple[str, ...] = PRODUCER_TOPICS + EVENT_TOPICS

DEFAULT_INTERVALS: dict[str, float] = {"bot": 5.0, "stats": 5.0, "diagnostics": 2.0}

Producer = Callable[[], Awaitable[Any]]


def _now_ms() -> int:
    return int(time.time() * 1000)


class TokenBucket:
    """Tiny token bucket for inbound-message rate limiting."""

    def __init__(self, rate: float, burst: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = rate
        self.burst = burst
        self._clock = clock
        self._tokens = burst
        self._at = clock()

    def allow(self, cost: float = 1.0) -> bool:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
        self._at = now
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False


class LiveClient:
    """One connected internal-WS peer (in practice: the dashboard backend)."""

    __slots__ = ("queue", "topics", "dropped", "closed", "guild_id")

    def __init__(self, maxsize: int = QUEUE_MAX) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        self.topics: set[str] = set()
        self.dropped = 0
        self.closed = False
        self.guild_id: int | None = None

    def put(self, message: dict) -> None:
        """Enqueue without blocking; drop the OLDEST queued message on overflow."""
        try:
            self.queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        try:
            self.queue.get_nowait()
            self.dropped += 1
        except asyncio.QueueEmpty:  # pragma: no cover - raced with the consumer
            pass
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:  # pragma: no cover
            self.dropped += 1


class LiveHub:
    def __init__(self, *, queue_size: int = QUEUE_MAX, intervals: dict[str, float] | None = None) -> None:
        self._queue_size = queue_size
        self._clients: set[LiveClient] = set()
        self._seq: dict[str, int] = {}
        self._producers: dict[str, Producer] = {}
        self._intervals = dict(DEFAULT_INTERVALS)
        if intervals:
            self._intervals.update(intervals)
        self._tasks: dict[str, asyncio.Task] = {}
        self.guild_id: int | None = None
        self._configured_for: object | None = None

    # -- clients ----------------------------------------------------------
    def add_client(self) -> LiveClient:
        client = LiveClient(self._queue_size)
        self._clients.add(client)
        return client

    def remove_client(self, client: LiveClient) -> None:
        client.closed = True
        client.topics.clear()
        self._clients.discard(client)
        self._sync_producers()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def subscriber_count(self, topic: str) -> int:
        return sum(1 for c in self._clients if topic in c.topics)

    def has_subscribers(self, topic: str) -> bool:
        return any(topic in c.topics for c in self._clients)

    def subscribe(self, client: LiveClient, topics: list[str]) -> tuple[list[str], list[str]]:
        """Returns (accepted, rejected). Unknown topics are rejected."""
        accepted: list[str] = []
        rejected: list[str] = []
        for t in topics:
            if isinstance(t, str) and t in ALL_TOPICS:
                if t not in accepted:
                    accepted.append(t)
            else:
                rejected.append(str(t)[:40])
        client.topics.update(accepted)
        self._sync_producers()
        return accepted, rejected

    def unsubscribe(self, client: LiveClient, topics: list[str]) -> None:
        for t in topics:
            client.topics.discard(t)
        self._sync_producers()

    # -- producers --------------------------------------------------------
    def set_producer(self, topic: str, producer: Producer, interval: float | None = None) -> None:
        self._producers[topic] = producer
        if interval is not None:
            self._intervals[topic] = interval

    def clear_producers(self) -> None:
        self._producers.clear()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._configured_for = None

    def running_producers(self) -> set[str]:
        return {t for t, task in self._tasks.items() if not task.done()}

    def _sync_producers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): producers get synced on the next async call
        for topic in list(self._producers):
            wanted = self.has_subscribers(topic)
            task = self._tasks.get(topic)
            running = task is not None and not task.done()
            if wanted and not running:
                self._tasks[topic] = loop.create_task(self._run_producer(topic), name=f"live-producer-{topic}")
            elif not wanted and running:
                task.cancel()
                self._tasks.pop(topic, None)

    async def _run_producer(self, topic: str) -> None:
        try:
            while True:
                producer = self._producers.get(topic)
                if producer is None:
                    return
                try:
                    data = await producer()
                    if data is not None:
                        self.publish(topic, data, kind="snapshot")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("live producer %r failed", topic, exc_info=True)
                await asyncio.sleep(self._intervals.get(topic, 5.0))
        except asyncio.CancelledError:
            pass

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass

    # -- publishing -------------------------------------------------------
    def publish(self, topic: str, data: dict, kind: str = "event") -> None:
        """Fan a message out to every client subscribed to `topic`. Never raises."""
        try:
            if not self._clients:
                return
            targets = [c for c in self._clients if topic in c.topics]
            if not targets:
                return
            payload = self._prepare(topic, data)
            if payload is None:
                return
            seq = self._seq.get(topic, 0) + 1
            self._seq[topic] = seq
            message = {
                "v": PROTOCOL_VERSION,
                "type": kind,
                "topic": topic,
                "ts": _now_ms(),
                "seq": seq,
                "data": payload,
            }
            for c in targets:
                c.put(message)
        except Exception:
            log.warning("live publish to %r failed", topic, exc_info=True)

    def snapshot_for(self, client: LiveClient, topic: str, data: dict) -> None:
        """Queue a one-off snapshot for ONE client (used right after subscribe)."""
        try:
            payload = self._prepare(topic, data)
            if payload is None:
                return
            seq = self._seq.get(topic, 0)
            client.put(
                {"v": PROTOCOL_VERSION, "type": "snapshot", "topic": topic, "ts": _now_ms(), "seq": seq, "data": payload}
            )
        except Exception:
            log.warning("live snapshot for %r failed", topic, exc_info=True)

    @staticmethod
    def _prepare(topic: str, data: Any) -> Any | None:
        from internal_api.snowflakes import stringify_snowflakes

        out = stringify_snowflakes(data)
        if topic == "diagnostics":
            try:
                from services import diagnostics_service

                out = diagnostics_service.scrub_payload(out)
            except Exception:
                log.warning("diagnostics scrub failed; message dropped", exc_info=True)
                return None
        return out


hub = LiveHub()


def publish(topic: str, data: dict) -> None:
    """Module-level convenience used by services. Never raises, never blocks."""
    try:
        hub.publish(topic, data)
    except Exception:  # pragma: no cover - hub.publish already guards
        pass
