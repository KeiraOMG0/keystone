"""Pooled httpx clients (server/services/http_pool.py): the dashboard backend
must reuse one client per event loop for its loopback calls (a new client costs
~280 ms of TLS-context setup on Windows), never close it after a call, and
never cache responses (permission checks stay live)."""

from __future__ import annotations

import asyncio

import httpx

from server.services import http_pool, internal_client, supervisor_client


async def test_internal_client_is_pooled_and_stays_open():
    async with internal_client._client() as first:
        pass
    async with internal_client._client() as second:
        pass
    assert first is second and not first.is_closed
    assert str(first.base_url).startswith("http://127.0.0.1") or "localhost" in str(first.base_url)
    async with supervisor_client._client() as sup:
        assert sup is not first  # separate pool entry per target
    await http_pool.close_all()
    assert first.is_closed and sup.is_closed
    async with internal_client._client() as third:  # recreated after close
        assert third is not first and not third.is_closed
    await http_pool.close_all()


def test_one_client_per_event_loop():
    async def grab():
        async with internal_client._client() as c:
            return c

    a = asyncio.run(grab())
    b = asyncio.run(grab())
    assert a is not b  # a client is bound to its loop's connections


async def test_every_call_still_reaches_the_bot(monkeypatch):
    """No response caching: two permission checks are two requests."""
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        return httpx.Response(200, json={"allowed": len(hits) == 1})

    def factory():
        return httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(internal_client, "_new_client", factory)
    await http_pool.close_all()
    assert await internal_client.check_permission(1, 2, "staff_base") is True
    assert await internal_client.check_permission(1, 2, "staff_base") is False  # role removed: next call sees it
    assert len(hits) == 2
    await http_pool.close_all()
