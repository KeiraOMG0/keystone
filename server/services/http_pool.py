"""
Shared httpx clients for the dashboard backend's local calls (internal API,
supervisor control API).

Why: creating an `httpx.AsyncClient` builds a TLS context every time (even for
plain-http loopback URLs), which costs ~280 ms on this Windows host. The old
code made a new client per call, so a typical dashboard request (permission
check + data) spent ~560 ms just constructing clients while the bot answered in
1-5 ms. One pooled client per event loop and target removes that and keeps the
loopback connections alive between requests (measured 2026-10-30: ~280 ms ->
~3 ms per internal call).

Nothing is cached here except the connection: every request still goes to the
bot, so permission changes (a removed role) take effect on the next request.

Clients are kept per running event loop (tests create several loops) and closed
by `close_all()` from server/app.py's lifespan shutdown.
"""

from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import httpx

_POOLS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, httpx.AsyncClient]]" = (
    weakref.WeakKeyDictionary()
)


def shared(key: str, factory: Callable[[], httpx.AsyncClient]) -> httpx.AsyncClient:
    """The pooled client for `key` on the running loop (created on first use)."""
    loop = asyncio.get_running_loop()
    pool = _POOLS.setdefault(loop, {})
    client = pool.get(key)
    if client is None or client.is_closed:
        client = pool[key] = factory()
    return client


@asynccontextmanager
async def borrow(key: str, factory: Callable[[], httpx.AsyncClient]) -> AsyncIterator[httpx.AsyncClient]:
    """`async with borrow(...) as client:`, a drop-in for `async with httpx.AsyncClient(...)`
    that does NOT close the client afterwards."""
    yield shared(key, factory)


async def close_all() -> None:
    """Close every pooled client of the running loop (app shutdown)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for client in list((_POOLS.pop(loop, None) or {}).values()):
        try:
            await client.aclose()
        except Exception:
            pass
