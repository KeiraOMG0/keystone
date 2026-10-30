"""
In-memory record of the bot process's errors, for the owner-only Debug page's
Errors tab (internal GET /guilds/{id}/debug/errors -> dashboard /api/debug/errors).

`setup_logging` attaches ErrorRingHandler to the root logger, so every ERROR or
CRITICAL log record (failed cogs, command/app-command errors, task-loop and
event-listener exceptions discord.py logs, anything a cog logs with
log.error/log.exception) is kept here, grouped by fingerprint with a count.

In memory only: it starts empty at every bot start (crashes that kill the
process are in logs/supervisor/bot.log). Bounded: at most MAX_GROUPS groups
(the least recently seen are dropped). Text is stored raw and scrubbed when it
is served (internal_api/routes/debug.py uses the diagnostics scrubbers).
"""

from __future__ import annotations

import collections
import datetime
import hashlib
import logging
import re
import threading
import time
import traceback
from typing import Any

MAX_GROUPS = 200
MAX_MESSAGE = 2000
MAX_STACK = 8000

_DIGITS_RE = re.compile(r"\d+")


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class ErrorRing:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._groups: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
        self.started_at = time.time()

    def add(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        name = record.levelname
        stack = None
        if record.exc_info and record.exc_info[0] is not None:
            etype, evalue, etb = record.exc_info
            name = etype.__name__
            detail = str(evalue)
            if detail:
                message = f"{message}: {detail}" if message else detail
            stack = "".join(traceback.format_exception(etype, evalue, etb)[-40:])[-MAX_STACK:]
        message = message[:MAX_MESSAGE]
        raw = "|".join(("bot", record.name, name, _DIGITS_RE.sub("#", message[:300])))
        key = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]
        now = record.created or time.time()
        with self._lock:
            g = self._groups.pop(key, None)
            if g is None:
                g = {"id": key, "name": name, "logger": record.name, "count": 0, "first_seen": now}
            g.update(message=message, stack=stack, last_seen=now, level=record.levelname,
                     where=f"{record.module}.{record.funcName}:{record.lineno}")
            g["count"] += 1
            self._groups[key] = g  # most recently seen last
            while len(self._groups) > MAX_GROUPS:
                self._groups.popitem(last=False)

    def snapshot(self) -> list[dict[str, Any]]:
        """Groups in the dashboard's error-group shape, newest first (unscrubbed)."""
        with self._lock:
            groups = [dict(g) for g in reversed(self._groups.values())]
        return [
            {
                "id": g["id"],
                "source": "bot",
                "kind": g["level"].lower(),
                "name": g["name"],
                "message": g["message"],
                "count": g["count"],
                "first_seen": _iso(g["first_seen"]),
                "last_seen": _iso(g["last_seen"]),
                "paths": [],
                "user_agents": [],
                "users": [],
                "sample": {
                    "stack": g["stack"],
                    "component_stack": None,
                    "path": g["where"],
                    "user_agent": None,
                    "user_id": None,
                    "username": None,
                    "logger": g["logger"],
                },
            }
            for g in groups
        ]

    def clear(self, fingerprint: str | None = None) -> int:
        with self._lock:
            if fingerprint:
                return 1 if self._groups.pop(fingerprint, None) is not None else 0
            n = len(self._groups)
            self._groups.clear()
            return n


RING = ErrorRing()


class ErrorRingHandler(logging.Handler):
    def __init__(self, ring: ErrorRing = RING) -> None:
        super().__init__(level=logging.ERROR)
        self.ring = ring

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ring.add(record)
        except Exception:  # never let error capture break logging
            pass
