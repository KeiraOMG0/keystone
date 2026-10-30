"""
Error reports for the owner-only Debug page's Errors tab (server/db/models.py
ErrorReport, Alembic c7e2a9d4b1f3).

Two writers, one table:
  * "browser": POST /api/public/client-errors (server/routes/client_errors.py),
    sent by the frontend's AppErrorBoundary / window error handlers
    (src/lib/errorReport.ts). Public and unverified (anyone can post one), so it
    is rate limited, size-capped, and the panel labels it as visitor-reported.
  * "server": unhandled exceptions in this backend, recorded by
    ErrorCaptureMiddleware (the request still gets its normal 500).

Everything is scrubbed before it is stored: secret values / tokens (the
diagnostics scrubber), appeal tokens in paths, OAuth `code`/`state` query values.
No IP address is stored. Kept ERROR_REPORT_RETENTION_DAYS, at most
ERROR_REPORT_MAX_ROWS rows (oldest dropped), pruned at most every 10 minutes
from record().

Bot-process errors are not here: the bot keeps them in memory
(core/error_ring.py) and the Debug route merges them in at read time.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
import time
import traceback
from typing import Any

from sqlalchemy import delete, func, select

from server.db import engine as db_engine
from server.db.models import ErrorReport

log = logging.getLogger(__name__)

ERROR_REPORT_RETENTION_DAYS = 30
ERROR_REPORT_MAX_ROWS = 5000
_PRUNE_EVERY_SECONDS = 600
_last_prune = 0.0

SOURCES = ("browser", "server")
BROWSER_KINDS = ("render", "error", "rejection", "chunk", "test")

# Field caps (characters) for what gets stored.
MAX_NAME = 200
MAX_MESSAGE = 2000
MAX_STACK = 8000
MAX_COMPONENT_STACK = 4000
MAX_PATH = 300
MAX_USER_AGENT = 400

_APPEAL_PATH_RE = re.compile(r"(/appeals?/)(?!for-user\b|submit\b|me\b)[A-Za-z0-9_\-]{8,128}")
_QUERY_SECRET_RE = re.compile(r"([?&](?:code|state|token|access_token)=)[^&#\s\"']+", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")
_LINE_COL_RE = re.compile(r":\d+(?::\d+)?\)?$")


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def scrub_text(text: str | None) -> str | None:
    """Secret values/tokens (diagnostics scrubber) + appeal tokens + OAuth query values."""
    if not text:
        return text
    from server.routes.diagnostics import scrub  # lazy: routes import this module

    s = scrub(text)
    s = _APPEAL_PATH_RE.sub(lambda m: m.group(1) + ":token", s)
    return _QUERY_SECRET_RE.sub(lambda m: m.group(1) + "<redacted>", s)


def safe_path(path: str | None) -> str | None:
    """A page path without its query string/fragment, with appeal tokens hidden."""
    if not path:
        return None
    path = path.split("?", 1)[0].split("#", 1)[0]
    return _clip(scrub_text(path), MAX_PATH)


def fingerprint(source: str, name: str, message: str, stack: str | None) -> str:
    """Same error = same fingerprint: source + name + message with numbers
    normalised + the first stack frame without line/column."""
    frame = ""
    for line in (stack or "").splitlines()[1:6]:
        line = line.strip()
        if line:
            frame = _LINE_COL_RE.sub("", line)
            break
    raw = "|".join((source, name, _DIGITS_RE.sub("#", message[:300]), frame))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


async def record(
    *,
    source: str,
    kind: str,
    name: str,
    message: str,
    stack: str | None = None,
    component_stack: str | None = None,
    path: str | None = None,
    user_agent: str | None = None,
    user_id: int | None = None,
    username: str | None = None,
) -> None:
    """Store one report (scrubbed + clipped). Never raises."""
    try:
        name = _clip(scrub_text(name) or "Error", MAX_NAME)
        message = _clip(scrub_text(message) or "", MAX_MESSAGE)
        stack = _clip(scrub_text(stack), MAX_STACK) or None
        component_stack = _clip(scrub_text(component_stack), MAX_COMPONENT_STACK) or None
        row = ErrorReport(
            source=source,
            kind=kind,
            fingerprint=fingerprint(source, name, message, stack),
            name=name,
            message=message,
            stack=stack,
            component_stack=component_stack,
            path=safe_path(path),
            user_agent=_clip(user_agent, MAX_USER_AGENT) or None,
            user_id=user_id,
            username=_clip(username, 64) or None,
        )
        async with db_engine.async_session_factory() as db:
            db.add(row)
            await db.commit()
        await _maybe_prune()
    except Exception:  # an error report must never cause another error
        log.warning("could not store an error report", exc_info=True)


async def _maybe_prune() -> None:
    global _last_prune
    now = time.monotonic()
    if _last_prune and now - _last_prune < _PRUNE_EVERY_SECONDS:
        return
    _last_prune = now
    await prune()


async def prune() -> int:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=ERROR_REPORT_RETENTION_DAYS)
    async with db_engine.async_session_factory() as db:
        removed = (await db.execute(delete(ErrorReport).where(ErrorReport.created_at < cutoff))).rowcount or 0
        total = (await db.execute(select(func.count(ErrorReport.id)))).scalar_one()
        if total > ERROR_REPORT_MAX_ROWS:
            keep_from = (
                await db.execute(
                    select(ErrorReport.id).order_by(ErrorReport.id.desc()).offset(ERROR_REPORT_MAX_ROWS - 1).limit(1)
                )
            ).scalar_one()
            removed += (await db.execute(delete(ErrorReport).where(ErrorReport.id < keep_from))).rowcount or 0
        await db.commit()
    return removed


def _iso(dt: datetime.datetime) -> str:
    if dt.tzinfo is None:  # SQLite hands naive datetimes back
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


async def list_groups(limit_rows: int = 2000) -> list[dict[str, Any]]:
    """The newest `limit_rows` reports grouped by fingerprint, newest group
    first. Each group carries its newest report as `sample` and up to 5 distinct
    pages and browsers it was seen on."""
    async with db_engine.async_session_factory() as db:
        rows = (
            await db.execute(select(ErrorReport).order_by(ErrorReport.id.desc()).limit(limit_rows))
        ).scalars().all()
    groups: dict[str, dict[str, Any]] = {}
    for r in rows:  # newest first
        g = groups.get(r.fingerprint)
        if g is None:
            g = groups[r.fingerprint] = {
                "id": r.fingerprint,
                "source": r.source,
                "kind": r.kind,
                "name": r.name,
                "message": r.message,
                "count": 0,
                "first_seen": _iso(r.created_at),
                "last_seen": _iso(r.created_at),
                "paths": [],
                "user_agents": [],
                "users": [],
                "sample": {
                    "stack": r.stack,
                    "component_stack": r.component_stack,
                    "path": r.path,
                    "user_agent": r.user_agent,
                    "user_id": str(r.user_id) if r.user_id else None,
                    "username": r.username,
                    "logger": None,
                },
            }
        g["count"] += 1
        g["first_seen"] = _iso(r.created_at)
        for key, value in (("paths", r.path), ("user_agents", r.user_agent)):
            if value and value not in g[key] and len(g[key]) < 5:
                g[key].append(value)
        if r.user_id and len(g["users"]) < 5 and all(u["id"] != str(r.user_id) for u in g["users"]):
            g["users"].append({"id": str(r.user_id), "username": r.username})
    return list(groups.values())


async def clear(*, source: str | None = None, fingerprint_id: str | None = None) -> int:
    """Delete stored reports: all, one source, or one group."""
    stmt = delete(ErrorReport)
    if source:
        stmt = stmt.where(ErrorReport.source == source)
    if fingerprint_id:
        stmt = stmt.where(ErrorReport.fingerprint == fingerprint_id)
    async with db_engine.async_session_factory() as db:
        removed = (await db.execute(stmt)).rowcount or 0
        await db.commit()
    return removed


class ErrorCaptureMiddleware:
    """Records unhandled exceptions from any HTTP route as "server" reports
    (route TEMPLATE only, never the raw path), then re-raises so the request
    still gets its normal 500 and uvicorn still logs it."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            route = getattr(scope.get("route"), "path", None) or "(unmatched)"
            await record(
                source="server",
                kind="exception",
                name=type(exc).__name__,
                message=str(exc),
                stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-40:]),
                path=f"{scope.get('method', '?')} {route}",
            )
            raise
