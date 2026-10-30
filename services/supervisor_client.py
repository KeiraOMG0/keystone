"""
Bot-side client for supervisor.py's control API (loopback, bearer auth).

The supervisor and the bot do not import each other; this mirrors
server/services/supervisor_client.py (dashboard side). Contract:

  GET  /status   -> {"supervisor": {...}, "services": [...]}
  POST /restart  {"target", "requested_by", "source", "reason", "delay_s"}
                 -> 202 {"accepted": [...], "skipped": [...], "delay_s", "eta_s"}
                    429 {"error": "cooldown", "retry_after_s"} / 409 {"error", "message"}
  GET  /logs?service=&lines=  -> {"service", "lines": [...]}

Errors:
  SupervisorUnavailable - connection refused / timeout: the bot is not running
                          under supervisor.py (or it runs old code without the API).
  SupervisorCooldown    - 429; `.retry_after_s`.
  SupervisorError       - any other non-2xx; `.status`, `.error`, `.message`.

Helper for the bot's own code:
    await supervisor_client.request_restart("bot", reason="...", requested_by="bot", source="bot")
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, TypedDict

import aiohttp

import config

Target = Literal["bot", "dashboard", "tunnel", "all"]
Source = Literal["dashboard", "discord", "bot", "cli"]

_TIMEOUT = aiohttp.ClientTimeout(total=5, connect=2)


class RestartRequestInfo(TypedDict, total=False):
    by: str
    source: str
    reason: str | None
    at: str


class ServiceStatus(TypedDict, total=False):
    name: str
    running: bool
    pid: int | None
    uptime_s: float | None
    restarts: int
    last_exit: dict | None
    last_request: RestartRequestInfo | None
    health_fails: int
    cooldown_remaining_s: float
    waiting_on_dependency: bool
    skip_reason: str | None
    next_start_in_s: float | None
    restart_pending: bool


class SupervisorStatus(TypedDict):
    supervisor: dict
    services: list[ServiceStatus]


class RestartResult(TypedDict, total=False):
    accepted: list[str]
    skipped: list[dict]
    delay_s: float
    eta_s: float


class SupervisorUnavailable(Exception):
    """The control API did not answer (not running under supervisor.py)."""


class SupervisorError(Exception):
    def __init__(self, status: int, payload: dict | None = None):
        payload = payload or {}
        self.status = status
        self.error = str(payload.get("error") or "supervisor_error")
        self.message = str(payload.get("message") or self.error)
        self.payload = payload
        super().__init__(f"supervisor returned {status}: {self.error}")


class SupervisorCooldown(SupervisorError):
    @property
    def retry_after_s(self) -> int:
        try:
            return int(self.payload.get("retry_after_s") or 0)
        except (TypeError, ValueError):
            return 0


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.SUPERVISOR_CONTROL_SECRET}"}


async def _call(method: str, path: str, *, json_body: dict | None = None, params: dict | None = None) -> Any:
    if not config.SUPERVISOR_CONTROL_SECRET:
        raise SupervisorUnavailable("no supervisor control secret configured")
    url = f"{config.SUPERVISOR_CONTROL_URL}{path}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.request(method, url, json=json_body, params=params, headers=_headers()) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    payload = None
                if resp.status < 300:
                    return payload if isinstance(payload, dict) else {}
                payload = payload if isinstance(payload, dict) else {}
                if resp.status == 429:
                    raise SupervisorCooldown(resp.status, payload)
                raise SupervisorError(resp.status, payload)
    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as exc:
        raise SupervisorUnavailable(str(exc) or type(exc).__name__) from exc


async def get_status() -> SupervisorStatus:
    return await _call("GET", "/status")  # type: ignore[return-value]


async def get_logs(service: str, lines: int = 50) -> dict:
    return await _call("GET", "/logs", params={"service": service, "lines": str(lines)})


async def request_restart(
    target: Target,
    *,
    reason: str | None = None,
    requested_by: str = "bot",
    source: Source = "bot",
    delay_s: float | None = None,
) -> RestartResult:
    body: dict[str, Any] = {"target": target, "requested_by": requested_by, "source": source, "reason": reason}
    if delay_s is not None:
        body["delay_s"] = delay_s
    return await _call("POST", "/restart", json_body=body)  # type: ignore[return-value]
