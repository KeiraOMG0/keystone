"""
WS /api/ws  --  browser live-update socket.

Handshake checks (all before any data is sent):
  1. STRICT `Origin`: must be present and in server/csrf.py's allowed set.
     SameSite=Lax does NOT protect websocket handshakes (the cookie rides along
     on cross-site ws:// upgrades from a hostile page), so this is the CSRF
     defence for this endpoint. Missing or foreign Origin -> close 4403.
  2. Session: the signed session cookie must resolve to a live, unexpired row
     (same loader as the HTTP dependency). None -> close 4401.
  3. Guild membership (`is_guild_member`) -> otherwise close 4403. After the
     accept, a bot answering "member not found" (hub.resolve_access returns no
     topics) also closes 4403, at the handshake or on any re-validation.
Rejections accept the upgrade and immediately close with an application code so
the browser sees a clean close code (a pre-accept refusal is an opaque 1006).

Then the socket is served by `DashboardHub`: per-topic authorization on every
subscribe AND on every outgoing message, periodic session/permission
re-validation (sockets whose session was deleted/expired are closed, demoted
users lose topics), size cap, inbound rate limit, idle timeout, ping/pong.
Cloudflare Tunnel proxies websockets; nothing here assumes a path prefix or a
proxy: the page is same-origin so the browser derives ws:// or wss:// itself.

Client messages: subscribe / unsubscribe {"topics": [...]}, ping, and
resync {"topics": [...]} (resend a full snapshot of subscribed topics; used by
the diagnostics delta stream, see DashboardHub.push_diagnostics).

Close codes: 4401 unauthenticated / session ended, 4403 forbidden (origin or
not a guild member), 4408 idle timeout, 4429 rate limited / too many sockets,
1009 message too big, 1001 server shutting down.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from server import config
from server.auth.dependencies import load_valid_session
from server.auth.session import get_session
from server.csrf import allowed_origins
from server.db import engine as db_engine
from server.live.hub import MAX_CONNECTIONS_PER_USER, Connection, TokenBucket, hub

log = logging.getLogger(__name__)

router = APIRouter()

MAX_MESSAGE_BYTES = 4096
MAX_TOPICS_PER_MESSAGE = 16
RATE_PER_SEC = 10.0
RATE_BURST = 20.0
IDLE_TIMEOUT_SECONDS = 90.0  # the client pings every ~25s

CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_IDLE = 4408
CLOSE_RATE_LIMITED = 4429
CLOSE_TOO_BIG = 1009


def session_factory():
    """Indirection so tests can point the endpoint at a temp dashboard DB."""
    return db_engine.async_session_factory()


def origin_allowed(origin: str | None) -> bool:
    if not origin:
        return False
    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        return False
    return f"{parsed.scheme}://{parsed.netloc}" in allowed_origins()


async def _reject(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.accept()
        await websocket.close(code=code)
    except Exception:
        pass


async def _safe_close(websocket: WebSocket, code: int) -> None:
    try:
        if websocket.application_state != WebSocketState.DISCONNECTED:
            await websocket.close(code=code)
    except Exception:
        pass


async def _writer(websocket: WebSocket, conn: Connection) -> None:
    try:
        while True:
            message = await conn.queue.get()
            await websocket.send_text(json.dumps(message, default=str, separators=(",", ":")))
    except (WebSocketDisconnect, RuntimeError):
        return


async def _reader(websocket: WebSocket, conn: Connection) -> None:
    bucket = TokenBucket(RATE_PER_SEC, RATE_BURST)
    while True:
        try:
            frame = await asyncio.wait_for(websocket.receive(), IDLE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            conn.request_close(CLOSE_IDLE)
            return
        except (WebSocketDisconnect, RuntimeError):
            return
        if frame["type"] == "websocket.disconnect":
            return
        text = frame.get("text")
        if text is None:  # binary frames are not part of the protocol
            conn.request_close(1003)
            return
        if len(text.encode("utf-8", "ignore")) > MAX_MESSAGE_BYTES:
            conn.request_close(CLOSE_TOO_BIG)
            return
        if not bucket.allow():
            conn.request_close(CLOSE_RATE_LIMITED)
            return
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            conn.put(hub.envelope("error", None, {"code": "bad_message"}))
            continue
        kind = msg["type"]
        if kind == "ping":
            conn.put(hub.envelope("pong", None))
        elif kind in ("subscribe", "unsubscribe"):
            topics = msg.get("topics")
            if not isinstance(topics, list) or len(topics) > MAX_TOPICS_PER_MESSAGE:
                conn.put(hub.envelope("error", None, {"code": "bad_topics"}))
                continue
            result: dict[str, Any] = {}
            if kind == "subscribe":
                result = hub.subscribe(conn, topics)
            else:
                hub.unsubscribe(conn, topics)
            conn.put(hub.envelope("ack", None, {"op": kind, "topics": sorted(conn.topics), **result}))
        elif kind == "resync":
            # "resend me a full snapshot" (diagnostics deltas that do not fit
            # the client's base). Only subscribed + authorized topics count.
            topics = msg.get("topics")
            if not isinstance(topics, list) or len(topics) > MAX_TOPICS_PER_MESSAGE:
                conn.put(hub.envelope("error", None, {"code": "bad_topics"}))
                continue
            done = hub.resync(conn, topics)
            conn.put(hub.envelope("ack", None, {"op": "resync", "topics": sorted(conn.topics), "resynced": done}))
        else:
            conn.put(hub.envelope("error", None, {"code": "unknown_type"}))


async def _revalidator(conn: Connection) -> None:
    """Every LIVE_REVALIDATE_SECONDS: is the session still valid, and what may
    this user see now? Deleted/expired session -> close 4401 immediately;
    demoted user -> lose the topics (never a silent stale grant)."""
    while True:
        await asyncio.sleep(max(0.05, float(config.LIVE_REVALIDATE_SECONDS)))
        try:
            async with session_factory() as db:
                session = await get_session(db, conn.session_id)
                if session is not None:
                    from server.auth.session import touch_session
                    import datetime

                    last = session.last_seen_at
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=datetime.timezone.utc)
                    age = (datetime.datetime.now(datetime.timezone.utc) - last).total_seconds()
                    if age > config.SESSION_MAX_AGE_SECONDS:
                        session = None
                    else:
                        session = await touch_session(db, session)  # an open tab is activity (throttled write)
        except Exception:
            log.warning("live session revalidation failed", exc_info=True)
            continue  # a DB hiccup must not log everyone out
        if session is None or not session.is_guild_member:
            conn.request_close(CLOSE_UNAUTHENTICATED if session is None else CLOSE_FORBIDDEN)
            return
        allowed = await hub.resolve_access(session, force=True)
        if not allowed:
            # The bot says this user is no longer a member (kicked/banned/left):
            # no topics at all, close instead of keeping an empty socket open.
            hub.apply_access(conn, allowed)
            conn.request_close(CLOSE_FORBIDDEN)
            return
        hub.apply_access(conn, allowed)


@router.websocket("/api/ws")
async def browser_ws(websocket: WebSocket) -> None:
    if not origin_allowed(websocket.headers.get("origin")):
        await _reject(websocket, CLOSE_FORBIDDEN)
        return

    async with session_factory() as db:
        session = await load_valid_session(db, websocket.cookies.get(config.SESSION_COOKIE_NAME))
    if session is None:
        await _reject(websocket, CLOSE_UNAUTHENTICATED)
        return
    if not session.is_guild_member:
        await _reject(websocket, CLOSE_FORBIDDEN)
        return
    if hub.connection_count(int(session.discord_user_id)) >= MAX_CONNECTIONS_PER_USER:
        await _reject(websocket, CLOSE_RATE_LIMITED)
        return

    await websocket.accept()
    allowed = await hub.resolve_access(session)
    if not allowed:  # the bot says this user is not (any longer) a guild member
        await _safe_close(websocket, CLOSE_FORBIDDEN)
        return
    conn = Connection(session.session_id, int(session.discord_user_id), session.discord_username or "", allowed)
    hub.add_connection(conn)
    conn.put(
        hub.envelope(
            "hello",
            None,
            {"topics": sorted(conn.allowed), "bot": dict(hub.bot_state), "revalidate_seconds": config.LIVE_REVALIDATE_SECONDS},
        )
    )
    tasks = [
        asyncio.create_task(_reader(websocket, conn)),
        asyncio.create_task(_writer(websocket, conn)),
        asyncio.create_task(_revalidator(conn)),
        asyncio.create_task(conn.close_event.wait()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass
        code = conn.close_code or 1000
        hub.remove_connection(conn)
        await _safe_close(websocket, code)
