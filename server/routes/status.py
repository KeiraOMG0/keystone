"""
GET /api/status

PUBLIC (no session/permission required) — this is the one route a user
needs to reach WHILE something else is broken, so it can't be gated behind
the auth it's trying to help diagnose. Actively probes both processes
rather than trusting that "this route ran" implies everything is healthy:

  - dashboard: always "up" if this handler is running at all (no probe
    needed — a dead dashboard process can't answer HTTP requests).
  - internal_api: a real GET to {INTERNAL_API_BASE_URL}/health (the same
    unauthenticated endpoint internal_api/app.py exposes — no
    Authorization header needed, unlike every other internal_api route).
    "up" only if it responds 200 within the timeout; latency_ms measures
    round trip so a slow-but-alive bot process is visible too.
  - bot (Discord gateway): internal_api's own /health doesn't reflect
    Discord connection state, only that the FastAPI server embedded in the
    bot process is answering. discord.Client.is_ready()/is_closed() live
    on the live bot object, which this dashboard process has no handle to
    — so "internal_api up" is reported as the proxy for "bot process up"
    rather than fabricating a separate signal we can't actually check.
"""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter

from server import config

router = APIRouter(prefix="/api", tags=["status"])

_PROBE_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


async def _probe_internal_api() -> dict:
    start = time.monotonic()
    try:
        from server.services import internal_client

        async with internal_client._client() as client:  # pooled: no ~280 ms client setup per probe
            resp = await client.get("/health", timeout=_PROBE_TIMEOUT)
        latency_ms = round((time.monotonic() - start) * 1000)
        if resp.status_code == 200:
            return {"status": "up", "latency_ms": latency_ms}
        return {"status": "down", "latency_ms": latency_ms, "detail": f"HTTP {resp.status_code}"}
    except httpx.RequestError:
        # Public endpoint: never echo the raw httpx error (internal host/port).
        return {"status": "down", "latency_ms": None, "detail": "unreachable"}


@router.get("/status")
async def get_status() -> dict:
    internal_api_status = await _probe_internal_api()
    return {
        "dashboard": {"status": "up"},
        "internal_api": internal_api_status,
        # internal_api's /health responds from inside the bot process, so a
        # reachable internal_api implies the bot process is alive — see
        # module docstring for why this isn't a separate live probe.
        "bot": {"status": internal_api_status["status"]},
    }
