"""
WS /ws  --  live-event stream for the dashboard BACKEND (never for browsers).

The dashboard backend (server/live/upstream.py) is the only intended client.
The bot never dials out and this route exposes nothing new to the internet:
it is served by the same loopback-bound uvicorn as the rest of the internal API.

Authentication (constant-time compare against INTERNAL_API_SECRET):
  * `Authorization: Bearer <secret>` on the upgrade request (preferred), or
  * no header, then the FIRST message must be {"type":"auth","secret":"..."}
    within AUTH_TIMEOUT seconds.
Anything else is closed (wrong header secret is refused before the handshake
completes; wrong/late first message is closed with 4401).

Defence in depth on the handshake: the peer must be a loopback address, and any
request carrying an `Origin` header is refused (a browser page must never be
able to open this socket, whatever it knows).

Protocol: see docs/ARCHITECTURE.md "Live updates (WebSocket)". Client messages:
  {"type":"subscribe","topics":[...]} / {"type":"unsubscribe","topics":[...]} /
  {"type":"ping"}. The server answers with envelopes
  {"v":1,"type":"event|snapshot|pong|error|hello|ack","topic","ts","seq","data"}.
Inbound messages are size-capped and rate-limited.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

import config
from internal_api.routes.stats import build_live_stats
from services import diagnostics_service
from services.live_events import ALL_TOPICS, PROTOCOL_VERSION, LiveClient, TokenBucket, hub

log = logging.getLogger(__name__)

router = APIRouter()

AUTH_TIMEOUT = 5.0
MAX_INBOUND_BYTES = 8 * 1024
MAX_TOPICS_PER_MESSAGE = 16
INBOUND_RATE_PER_SEC = 20.0
INBOUND_BURST = 40.0
# Hosts allowed to open the socket. The listener is loopback-bound anyway.
ALLOWED_HOSTS: set[str] = {"127.0.0.1", "::1", "localhost"}

# Close codes (4xxx = application defined).
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_RATE_LIMITED = 4429
CLOSE_TOO_BIG = 1009


def secret_ok(candidate: object) -> bool:
    """Constant-time comparison against the shared internal secret."""
    expected = getattr(config, "INTERNAL_API_SECRET", "") or ""
    if not expected or not isinstance(candidate, str):
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _envelope(kind: str, topic: str | None = None, data: Any = None, seq: int = 0) -> dict:
    return {
        "v": PROTOCOL_VERSION,
        "type": kind,
        "topic": topic,
        "ts": int(time.time() * 1000),
        "seq": seq,
        "data": data if data is not None else {},
    }


def _resolve_guild_id(bot: Any) -> int | None:
    if hub.guild_id:
        return hub.guild_id
    guilds = list(getattr(bot, "guilds", []) or [])
    return guilds[0].id if len(guilds) == 1 else None


def configure_hub(app: Any, guild_id: int | None) -> None:
    """Idempotently attach the bot-backed producers (bot/stats/diagnostics) to
    the hub. Done lazily on the first connection so main.py needs no change."""
    if guild_id:
        hub.guild_id = guild_id
    bot = getattr(app.state, "bot", None)
    if bot is None or hub._configured_for is app:
        return
    hub._configured_for = app

    async def bot_snapshot() -> dict:
        return {
            "online": True,
            "ready": bool(bot.is_ready()),
            "latency_ms": diagnostics_service._lat_ms(getattr(bot, "latency", None)),
        }

    async def stats_snapshot() -> dict | None:
        gid = _resolve_guild_id(bot)
        guild = bot.get_guild(gid) if gid else None
        return build_live_stats(guild, bot) if guild is not None else None

    async def diagnostics_snapshot() -> dict | None:
        gid = _resolve_guild_id(bot)
        if not gid:
            return None
        return await diagnostics_service.collect_diagnostics(bot, gid, app=app)

    hub.set_producer("bot", bot_snapshot)
    hub.set_producer("stats", stats_snapshot)
    hub.set_producer("diagnostics", diagnostics_snapshot)


async def _safe_close(websocket: WebSocket, code: int) -> None:
    try:
        if websocket.application_state != WebSocketState.DISCONNECTED:
            await websocket.close(code=code)
    except Exception:
        pass


async def _writer(websocket: WebSocket, client: LiveClient) -> None:
    try:
        while True:
            message = await client.queue.get()
            await websocket.send_text(json.dumps(message, default=str, separators=(",", ":")))
    except (WebSocketDisconnect, RuntimeError):
        return


async def _reader(websocket: WebSocket, client: LiveClient) -> None:
    bucket = TokenBucket(INBOUND_RATE_PER_SEC, INBOUND_BURST)
    while True:
        try:
            text = await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return
        if len(text.encode("utf-8", "ignore")) > MAX_INBOUND_BYTES:
            await _safe_close(websocket, CLOSE_TOO_BIG)
            return
        if not bucket.allow():
            await _safe_close(websocket, CLOSE_RATE_LIMITED)
            return
        try:
            msg = json.loads(text)
        except ValueError:
            msg = None
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            client.put(_envelope("error", data={"code": "bad_message"}))
            continue
        kind = msg["type"]
        if kind == "ping":
            client.put(_envelope("pong"))
        elif kind in ("subscribe", "unsubscribe"):
            topics = msg.get("topics")
            if not isinstance(topics, list) or len(topics) > MAX_TOPICS_PER_MESSAGE:
                client.put(_envelope("error", data={"code": "bad_topics"}))
                continue
            if kind == "subscribe":
                accepted, rejected = hub.subscribe(client, topics)
            else:
                hub.unsubscribe(client, [t for t in topics if isinstance(t, str)])
                accepted, rejected = [], []
            client.put(
                _envelope("ack", data={"op": kind, "topics": sorted(client.topics), "accepted": accepted, "rejected": rejected})
            )
        else:
            client.put(_envelope("error", data={"code": "unknown_type"}))


@router.websocket("/ws")
async def live_ws(websocket: WebSocket) -> None:
    host = websocket.client.host if websocket.client else None
    if host not in ALLOWED_HOSTS or websocket.headers.get("origin") is not None:
        await websocket.close(code=CLOSE_FORBIDDEN)  # refused before the handshake completes
        return

    header = websocket.headers.get("authorization")
    authed = False
    if header is not None:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not secret_ok(token.strip()):
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return
        authed = True

    await websocket.accept()

    if not authed:
        try:
            first = await asyncio.wait_for(websocket.receive_text(), AUTH_TIMEOUT)
            msg = json.loads(first) if len(first) <= MAX_INBOUND_BYTES else None
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, RuntimeError):
            msg = None
        if not (isinstance(msg, dict) and msg.get("type") == "auth" and secret_ok(msg.get("secret"))):
            await _safe_close(websocket, CLOSE_UNAUTHORIZED)
            return

    guild_id: int | None = None
    raw = websocket.query_params.get("guild_id")
    if raw and raw.isdigit():
        guild_id = int(raw)
    configure_hub(websocket.app, guild_id)

    client = hub.add_client()
    client.guild_id = guild_id
    try:
        client.put(
            _envelope(
                "hello",
                data={"topics": list(ALL_TOPICS), "guild_id": str(hub.guild_id) if hub.guild_id else None},
            )
        )
        reader = asyncio.create_task(_reader(websocket, client))
        writer = asyncio.create_task(_writer(websocket, client))
        done, pending = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except BaseException:
                pass
    finally:
        hub.remove_client(client)
        await _safe_close(websocket, 1000)
