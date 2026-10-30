"""
Dashboard-side live hub: bridges the bot's upstream stream to browser sockets.

Responsibilities
  * Owns the connection registry for browser sockets (`Connection`).
  * Decides per-topic authorization (TOPIC_PERMISSIONS) using the SAME permission
    model as the HTTP routes: `check-bulk` through the internal API for grantable
    permissions, `check_is_staff` for `diagnostics` (the `require_staff`
    semantics, including the cached-role fallback when the bot is down so the
    diagnostics page still works during an outage). Fallbacks apply ONLY when
    the bot cannot answer, and cached grants only for
    PERMISSION_FALLBACK_MAX_AGE; a bot answering "member not found" removes
    every topic and the socket is closed 4403 (see resolve_access).
  * Filters EVERY outgoing message per connection (`topic in conn.allowed and
    topic in conn.topics`); the client's subscribe request is never trusted.
  * Ref-counts topic subscriptions and tells the upstream client which topics to
    subscribe to, so the bot only produces `stats` / `diagnostics` while at least
    one browser is watching.
  * Runs the diagnostics push loop ONLY while >= 1 diagnostics subscriber exists.
    Diagnostics go out as DELTAS per connection (`diagnostics_delta`): one full
    `snapshot` when a connection subscribes (or asks to `resync`, or after the
    cache-clear button), then `delta` envelopes that carry only the paths
    (up to DIAG_DELTA_DEPTH keys deep) whose value changed since what THAT
    connection last received. Every payload is scrubbed before diffing.
  * Tracks bot connected/disconnected state and broadcasts it on `bot`.

server/ deliberately does not import the bot's packages: the wire format is the
JSON envelope documented in docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from types import SimpleNamespace
from typing import Any

from server import config
from server.live.upstream import UpstreamClient
from server.services import internal_client
from server.permissions.guards import bot_unreachable, cached_staff, member_gone
from server.services.internal_client import InternalAPIError

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
QUEUE_MAX = 256
MAX_CONNECTIONS_PER_USER = 8
DIAGNOSTICS_INTERVAL = 2.0
PERMISSION_CACHE_TTL = 30.0
# While the bot cannot answer, a user's last known grants are honoured for at
# most this long after they were last confirmed (then: member-level topics,
# plus diagnostics for cached-role staff, like GET /api/diagnostics).
PERMISSION_FALLBACK_MAX_AGE = 600.0
BOT_DUMP_MAX_AGE = 8.0  # a pushed bot dump older than this counts as "bot unreachable"
# Diagnostics deltas compare (and replace) values at most this many keys deep:
# ["bot", "process", "cpu_percent"] is one unit; anything deeper (lists, the
# system.cpu dict, ...) is resent whole when any part of it changed.
DIAG_DELTA_DEPTH = 3

# topic -> None (any authenticated guild member) | "staff" | a permission string
# | a tuple of permission strings (ANY of them grants the topic).
TOPIC_PERMISSIONS: dict[str, str | tuple[str, ...] | None] = {
    "bot": None,
    "stats": "staff_base",  # same gate as GET /api/dashboard/stats
    "diagnostics": "staff",
    "honeypot": "moderate_members",
    "tickets": "manage_tickets",
    "moderation": "moderate_members",
    "audit": "view_audit",
    "roles": "manage_reaction_roles",
    "birthdays": "manage_birthdays",
    "music": "control_music",  # Music page (cogs/music.py _notify: {"event"} only)  # Birthdays page (services/birthday_service.py notify)
    # Automation page: payloads only say which tab changed ({"section", "action"}),
    # so any of the tab permissions may receive it (welcome/boost changes reach
    # staff through the bulk check, which is True for staff).
    "automation": ("manage_events", "manage_schedules", "manage_autorole", "manage_templates"),
}
TOPICS: tuple[str, ...] = tuple(TOPIC_PERMISSIONS)


def required_permissions(req: str | tuple[str, ...] | None) -> tuple[str, ...]:
    """The grantable permissions behind a TOPIC_PERMISSIONS value (any-of)."""
    if req is None or req == "staff":
        return ()
    return (req,) if isinstance(req, str) else tuple(req)


_GRANTABLE = sorted({p for req in TOPIC_PERMISSIONS.values() for p in required_permissions(req)})


def now_ms() -> int:
    return int(time.time() * 1000)


def _same(a: Any, b: Any) -> bool:
    # type check first: 1 == True and 1 == 1.0 in Python, but not on the page
    return type(a) is type(b) and a == b


def diagnostics_delta(old: dict, new: dict, max_depth: int = DIAG_DELTA_DEPTH) -> tuple[list[list], list[list]]:
    """What changed from `old` to `new` (both JSON-shaped dicts).

    Returns (set, unset): `set` is [[path, value], ...] for every path (a list
    of keys, at most `max_depth` long) whose value is new or different; `unset`
    is [path, ...] for keys that disappeared. Applying both to `old` (replace the
    value at each set path, delete each unset path) yields `new`. Dicts are
    recursed into until `max_depth`; everything else is compared whole."""
    sets: list[list] = []
    unsets: list[list] = []

    def walk(o: dict, n: dict, path: list) -> None:
        for k, v in n.items():
            p = path + [k]
            if k not in o:
                sets.append([p, v])
                continue
            ov = o[k]
            if len(p) < max_depth and isinstance(ov, dict) and isinstance(v, dict):
                walk(ov, v, p)
            elif not _same(ov, v):
                sets.append([p, v])
        for k in o:
            if k not in n:
                unsets.append(path + [k])

    walk(old, new, [])
    return sets, unsets


class TokenBucket:
    def __init__(self, rate: float, burst: float) -> None:
        self.rate, self.burst = rate, burst
        self._tokens = burst
        self._at = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
        self._at = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False


class Connection:
    """One browser websocket."""

    def __init__(self, session_id: str, user_id: int, username: str, allowed: set[str]) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.username = username
        self.allowed: set[str] = set(allowed)  # topics this user may receive right now
        self.topics: set[str] = set()  # topics the client has (successfully) subscribed to
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.closed = False
        self.close_code: int | None = None
        self.dropped = 0
        self.close_event = asyncio.Event()
        # Diagnostics delta state: what this connection last received (None ->
        # the next push is a full snapshot) and its own revision counter.
        self.diag_last: dict | None = None
        self.diag_rev = 0

    def put(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
            return
        except asyncio.QueueFull:
            pass
        try:
            self.queue.get_nowait()
            self.dropped += 1
        except asyncio.QueueEmpty:  # pragma: no cover
            pass
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:  # pragma: no cover
            self.dropped += 1

    def request_close(self, code: int) -> None:
        if not self.closed:
            self.closed = True
            self.close_code = code
            self.close_event.set()


class DashboardHub:
    def __init__(self) -> None:
        self.conns: set[Connection] = set()
        self._seq: dict[str, int] = defaultdict(int)
        self.upstream: UpstreamClient | None = None
        self.bot_state: dict[str, Any] = {"online": False, "ready": None, "latency_ms": None, "since": now_ms()}
        self.latest: dict[str, dict] = {}
        self._bot_dump: tuple[float, dict] | None = None
        self._diag_task: asyncio.Task | None = None
        self._diag_wake: asyncio.Event | None = None  # created inside the loop that uses it
        self._perm_cache: dict[int, tuple[float, set[str]]] = {}
        self.diagnostics_interval = DIAGNOSTICS_INTERVAL
        self.app: Any = None  # set by the lifespan; used for the dashboard route listing

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self, app: Any = None, upstream: UpstreamClient | None = None) -> None:
        self.app = app
        if upstream is None and config.LIVE_ENABLED and self.upstream is None:
            upstream = UpstreamClient(
                f"{config.LIVE_INTERNAL_WS_URL}?guild_id={config.DASHBOARD_GUILD_ID}",
                config.INTERNAL_API_SECRET,
                on_message=self.on_upstream_message,
                on_state=self.on_upstream_state,
                backoff_cap=config.LIVE_UPSTREAM_BACKOFF_CAP_SECONDS,
            )
        if upstream is not None:
            self.upstream = upstream
            self._sync_upstream()
            upstream.start()

    async def stop(self) -> None:
        for conn in list(self.conns):
            conn.request_close(1001)  # going away
        if self._diag_task is not None:
            self._diag_task.cancel()
            try:
                await self._diag_task
            except BaseException:
                pass
            self._diag_task = None
        if self.upstream is not None:
            await self.upstream.stop()
            self.upstream = None

    # ------------------------------------------------------------------
    # envelopes
    # ------------------------------------------------------------------
    def envelope(self, kind: str, topic: str | None, data: Any = None, *, bump: bool = False) -> dict:
        if topic and bump:
            self._seq[topic] += 1
        return {
            "v": PROTOCOL_VERSION,
            "type": kind,
            "topic": topic,
            "ts": now_ms(),
            "seq": self._seq[topic] if topic else 0,
            "data": data if data is not None else {},
        }

    def broadcast(self, topic: str, kind: str, data: Any) -> None:
        """Deliver to every connection that is BOTH subscribed and authorized."""
        msg = self.envelope(kind, topic, data, bump=True)
        for conn in list(self.conns):
            if not conn.closed and topic in conn.topics and topic in conn.allowed:
                conn.put(msg)

    # ------------------------------------------------------------------
    # connections
    # ------------------------------------------------------------------
    def connection_count(self, user_id: int) -> int:
        return sum(1 for c in self.conns if c.user_id == user_id and not c.closed)

    def add_connection(self, conn: Connection) -> None:
        self.conns.add(conn)

    def remove_connection(self, conn: Connection) -> None:
        conn.request_close(conn.close_code or 1000)
        conn.topics.clear()
        self.conns.discard(conn)
        self._sync_upstream()

    async def close_session(self, session_id: str, code: int = 4401) -> int:
        """Immediately close every socket bound to this session (logout)."""
        n = 0
        for conn in list(self.conns):
            if conn.session_id == session_id:
                conn.request_close(code)
                n += 1
        return n

    def refcount(self, topic: str) -> int:
        return sum(1 for c in self.conns if topic in c.topics and not c.closed)

    # ------------------------------------------------------------------
    # authorization
    # ------------------------------------------------------------------
    async def resolve_access(self, session: Any, *, force: bool = False) -> set[str]:
        """Topics this session may receive right now. Cached briefly; `force`
        bypasses the cache (used by the periodic re-validation).

        An EMPTY set means "no access at all" (not a guild member, or the bot
        says the member is gone): the endpoint closes the socket with 4403.

        Bot errors: the cached-grant fallback applies ONLY when the bot cannot
        answer (guards.bot_unreachable: no answer, 5xx, guild not cached yet)
        and only within PERMISSION_FALLBACK_MAX_AGE of the last confirmation.
        A definitive answer never falls back: 404 "Member ... not found" ->
        empty set (cache dropped); any other 4xx -> member-level topics only."""
        uid = int(session.discord_user_id)
        hit = self._perm_cache.get(uid)
        if hit and not force and time.monotonic() - hit[0] < PERMISSION_CACHE_TTL:
            return set(hit[1])
        if not session.is_guild_member:
            return set()

        allowed = {t for t, req in TOPIC_PERMISSIONS.items() if req is None}
        try:
            perms = await internal_client.check_permissions_bulk(config.DASHBOARD_GUILD_ID, uid, _GRANTABLE)
            allowed |= {
                t for t, req in TOPIC_PERMISSIONS.items()
                if any(perms.get(p) for p in required_permissions(req))
            }
            is_staff = await internal_client.check_is_staff(config.DASHBOARD_GUILD_ID, uid)
            if is_staff:
                allowed.add("diagnostics")
            # Staff bypass everything (services/permission_service.py): the bulk
            # check already reflects that, this is belt and braces.
        except InternalAPIError as exc:
            member_level = {t for t, req in TOPIC_PERMISSIONS.items() if req is None}
            if member_gone(exc):
                # The bot answered: this user left / was kicked / was banned.
                # Drop everything, including the cached grants.
                self._perm_cache.pop(uid, None)
                return set()
            if not bot_unreachable(exc):
                # The bot answered something else (401/422/unknown 404): fail
                # closed to member-level topics, never the cached grants.
                self._perm_cache.pop(uid, None)
                return member_level
            # Bot unreachable: keep the last known grants for a user confirmed
            # recently; otherwise only the member-level topics, plus diagnostics
            # for cached-role staff (same fallback as GET /api/diagnostics) so the
            # ops page keeps working exactly when things are broken.
            if hit and time.monotonic() - hit[0] <= PERMISSION_FALLBACK_MAX_AGE:
                return set(hit[1])
            if cached_staff(session):
                member_level.add("diagnostics")
            return member_level  # deliberately NOT cached: retry on the next check
        self._perm_cache[uid] = (time.monotonic(), set(allowed))
        return set(allowed)

    def forget_permissions(self, user_id: int | None = None) -> None:
        if user_id is None:
            self._perm_cache.clear()
        else:
            self._perm_cache.pop(user_id, None)

    def apply_access(self, conn: Connection, allowed: set[str]) -> list[str]:
        """Set a connection's authorized topics; returns topics just revoked
        (they are also removed from its subscriptions)."""
        conn.allowed = set(allowed)
        revoked = sorted(t for t in conn.topics if t not in conn.allowed)
        for t in revoked:
            conn.topics.discard(t)
            if t == "diagnostics":
                conn.diag_last = None
            conn.put(self.envelope("error", t, {"code": "forbidden", "reason": "permission_revoked"}))
        if revoked:
            self._sync_upstream()
        return revoked

    # ------------------------------------------------------------------
    # subscriptions
    # ------------------------------------------------------------------
    def subscribe(self, conn: Connection, topics: list[Any], *, max_topics: int = 16) -> dict[str, list[str]]:
        accepted: list[str] = []
        denied: list[str] = []
        unknown: list[str] = []
        for t in topics:
            if not isinstance(t, str) or t not in TOPIC_PERMISSIONS:
                unknown.append(str(t)[:40])
            elif t not in conn.allowed:
                denied.append(t)
                conn.put(self.envelope("error", t, {"code": "forbidden"}))
            elif t not in conn.topics and len(conn.topics) >= max_topics:
                conn.put(self.envelope("error", t, {"code": "too_many_subscriptions"}))
            else:
                if t not in accepted:
                    accepted.append(t)
        newly = [t for t in accepted if t not in conn.topics]
        conn.topics.update(accepted)
        self._sync_upstream()
        for t in newly:
            self._send_initial(conn, t)
        for t in unknown:
            conn.put(self.envelope("error", None, {"code": "unknown_topic", "topic": t}))
        return {"accepted": accepted, "denied": denied, "unknown": unknown}

    def unsubscribe(self, conn: Connection, topics: list[Any]) -> None:
        for t in topics:
            if isinstance(t, str):
                conn.topics.discard(t)
                if t == "diagnostics":
                    conn.diag_last = None
        self._sync_upstream()

    def resync(self, conn: Connection, topics: list[Any]) -> list[str]:
        """Client asked for a fresh snapshot of topics it is subscribed to
        (e.g. a diagnostics delta did not fit what it has). Returns the topics
        honoured; anything not subscribed+authorized is ignored."""
        done: list[str] = []
        for t in topics:
            if isinstance(t, str) and t in conn.topics and t in conn.allowed and t not in done:
                done.append(t)
                self._send_initial(conn, t)
        return done

    def reset_diagnostics_state(self) -> None:
        """Forget what every connection last received (the cache-clear button):
        the next push is a full snapshot for everyone, sent right away."""
        for conn in list(self.conns):
            conn.diag_last = None
        self._wake_diagnostics()

    def _send_initial(self, conn: Connection, topic: str) -> None:
        if topic == "bot":
            conn.put(self.envelope("snapshot", "bot", dict(self.bot_state)))
        elif topic == "stats" and "stats" in self.latest:
            conn.put(self.envelope("snapshot", "stats", self.latest["stats"]))
        elif topic == "diagnostics":
            conn.diag_last = None  # next push to this connection is a full snapshot
            self._wake_diagnostics()

    def _sync_upstream(self) -> None:
        wanted = {t for t in TOPICS if self.refcount(t) > 0}
        if self.upstream is not None:
            self.upstream.set_topics(wanted)
        self._ensure_diagnostics_loop()

    # ------------------------------------------------------------------
    # upstream callbacks
    # ------------------------------------------------------------------
    def on_upstream_state(self, connected: bool) -> None:
        self.bot_state = {
            **self.bot_state,
            "online": connected,
            "since": now_ms(),
            **({} if connected else {"latency_ms": None, "ready": None}),
        }
        if not connected:
            self._bot_dump = None
            self.latest.pop("stats", None)
        self.broadcast("bot", "event", dict(self.bot_state))
        self._wake_diagnostics()

    def on_upstream_message(self, msg: dict) -> None:
        kind, topic = msg.get("type"), msg.get("topic")
        if kind not in ("event", "snapshot") or topic not in TOPIC_PERMISSIONS:
            return
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        if topic == "diagnostics":
            # Merged with the dashboard's own section by the diagnostics loop.
            self._bot_dump = (time.monotonic(), data)
            return
        if topic == "bot":
            self.bot_state = {
                **self.bot_state,
                "online": True,
                "ready": data.get("ready"),
                "latency_ms": data.get("latency_ms"),
            }
            data = dict(self.bot_state)
        elif topic == "stats":
            self.latest["stats"] = data
        self.broadcast(topic, kind, data)

    # ------------------------------------------------------------------
    # diagnostics loop (runs only while somebody is subscribed)
    # ------------------------------------------------------------------
    def _wake_diagnostics(self) -> None:
        if self._diag_wake is not None:
            self._diag_wake.set()

    def _ensure_diagnostics_loop(self) -> None:
        wanted = self.refcount("diagnostics") > 0
        running = self._diag_task is not None and not self._diag_task.done()
        if wanted and not running:
            try:
                self._diag_task = asyncio.get_running_loop().create_task(self._diagnostics_loop(), name="live-diagnostics")
            except RuntimeError:
                pass
        elif not wanted and running:
            self._diag_task.cancel()
            self._diag_task = None

    @property
    def diagnostics_running(self) -> bool:
        return self._diag_task is not None and not self._diag_task.done()

    async def build_diagnostics(self, viewer: dict | None = None) -> dict:
        """One merged push, same shape as GET /api/diagnostics."""
        from server.routes import diagnostics as route

        bot: dict[str, Any]
        fresh = self._bot_dump is not None and time.monotonic() - self._bot_dump[0] <= BOT_DUMP_MAX_AGE
        connected = self.upstream is not None and self.upstream.connected
        rtt = self.upstream.rtt_ms if self.upstream is not None else None
        if fresh and connected:
            bot = {"reachable": True, **({"round_trip_ms": rtt} if rtt is not None else {}), **self._bot_dump[1]}
        else:
            bot = {"reachable": False, "error": "internal_api_unavailable"}
        shim = SimpleNamespace(app=self.app) if self.app is not None else None
        dash = await route.dashboard_section(shim)
        dash["internal_api"] = {
            "reachable": bot["reachable"],
            "round_trip_ms": rtt,
            "base_url": config.INTERNAL_API_BASE_URL,
            "transport": "websocket",
            "upstream_connected": bool(connected),
        }
        import datetime

        return route._scrub(
            {
                "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "viewer": viewer or {},
                "bot": bot,
                "dashboard": dash,
            }
        )

    def push_diagnostics(self, conn: Connection, payload: dict) -> dict | None:
        """Send `payload` (already scrubbed) to one connection: a full
        `snapshot` if it has no base, else a `delta` with only what changed.
        Envelope extras: `rev` (this connection's revision after applying the
        message) and, on deltas, `base_rev` (the revision it applies on top
        of). Returns the message, or None when nothing changed."""
        if conn.diag_last is None:
            conn.diag_rev += 1
            msg = self.envelope("snapshot", "diagnostics", payload, bump=True)
            msg["rev"] = conn.diag_rev
        else:
            sets, unsets = diagnostics_delta(conn.diag_last, payload)
            if not sets and not unsets:
                return None
            base = conn.diag_rev
            conn.diag_rev += 1
            msg = self.envelope("delta", "diagnostics", {"set": sets, "unset": unsets}, bump=True)
            msg["rev"], msg["base_rev"] = conn.diag_rev, base
        conn.diag_last = payload
        conn.put(msg)
        return msg

    async def _diagnostics_loop(self) -> None:
        wake = self._diag_wake = asyncio.Event()
        try:
            while self.refcount("diagnostics") > 0:
                try:
                    payload = await self.build_diagnostics()
                    for conn in list(self.conns):
                        if conn.closed or "diagnostics" not in conn.topics or "diagnostics" not in conn.allowed:
                            continue
                        per_conn = dict(payload)
                        per_conn["viewer"] = {"user_id": str(conn.user_id), "username": conn.username}
                        self.push_diagnostics(conn, per_conn)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("live diagnostics push failed", exc_info=True)
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), self.diagnostics_interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass


hub = DashboardHub()
