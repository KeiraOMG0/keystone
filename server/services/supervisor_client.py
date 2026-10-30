"""
Dashboard-side client for supervisor.py's control API (loopback, bearer auth).

DELIBERATE EXCEPTION to "server/ only talks to the internal API": restart and
status must work while the bot (and so the internal API) is down or hung, so
this module calls the supervisor directly. It mirrors the bot-side
services/supervisor_client.py; the two processes do not import each other.

  GET  /status                   -> {"supervisor": {...}, "services": [...]}
  POST /restart {"target", "requested_by", "source", "reason", "delay_s"}
                                 -> 202 {"accepted", "skipped", "delay_s", "eta_s"}
  GET  /logs?service=&lines=     -> {"service", "lines": [...]}
  POST /rotate-logs {"requested_by", "source", "reason"}
                                 -> 202 {"queued": true, "message"} (result: /status log_rotation.last_run)
"""

from __future__ import annotations

from typing import Any

import httpx

from server import config

_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class SupervisorUnavailable(Exception):
    """Connection refused / timeout: not running under supervisor.py (or an old copy without the API)."""


class SupervisorError(Exception):
    def __init__(self, status_code: int, body: dict | None = None):
        super().__init__(f"supervisor returned {status_code}")
        self.status_code = status_code
        self.body = body or {}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.SUPERVISOR_BASE_URL,
        headers={"Authorization": f"Bearer {config.SUPERVISOR_CONTROL_SECRET}"},
        timeout=_TIMEOUT,
    )


async def _request(method: str, path: str, **kwargs: Any) -> dict:
    if not config.SUPERVISOR_CONTROL_SECRET:
        raise SupervisorUnavailable("no supervisor control secret configured")
    try:
        async with _client() as client:
            resp = await client.request(method, path, **kwargs)
    except httpx.RequestError as exc:  # connect refused, timeouts, protocol errors
        raise SupervisorUnavailable(type(exc).__name__) from exc
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code >= 300:
        raise SupervisorError(resp.status_code, body if isinstance(body, dict) else None)
    return body if isinstance(body, dict) else {}


async def get_status() -> dict:
    return await _request("GET", "/status")


async def get_logs(service: str, lines: int) -> dict:
    return await _request("GET", "/logs", params={"service": service, "lines": lines})


async def request_restart(
    target: str, *, requested_by: str, source: str = "dashboard", reason: str | None = None, delay_s: float | None = None
) -> dict:
    body: dict[str, Any] = {"target": target, "requested_by": requested_by, "source": source, "reason": reason}
    if delay_s is not None:
        body["delay_s"] = delay_s
    return await _request("POST", "/restart", json=body)


async def rotate_logs(*, requested_by: str, source: str = "dashboard", reason: str | None = None) -> dict:
    """Queue a log rotation now (owner-only in the dashboard). Same auth, timeout
    and error mapping as request_restart; 409 rotation_in_progress, 429 cooldown."""
    return await _request(
        "POST", "/rotate-logs", json={"requested_by": requested_by, "source": source, "reason": reason}
    )
