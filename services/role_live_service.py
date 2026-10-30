"""
Live `roles` topic publisher: coalesces guild role events into a few messages.

Fed by cogs/live_roles.py (on_guild_role_create / _update / _delete). Discord
sends one GUILD_ROLE_UPDATE per role whose position changed, so dragging a role
in the role list can fire dozens of updates within a few milliseconds. Pushing
each one to the dashboard would make every open Self-roles / Permissions page
re-fetch dozens of times, so events are buffered and flushed together:

* A trailing debounce of `window` seconds (default 0.5s): every new event pushes
  the flush back, so a burst flushes once it has gone quiet.
* A hard cap of `max_delay` seconds (default 2s) from the FIRST buffered event,
  so a never-ending trickle still flushes.
* Per role, the latest state wins with "created" and "deleted" sticky:
  created+updated -> created, anything+deleted -> deleted.

Wire contract (docs/ARCHITECTURE.md "Live updates"): each flush publishes ONE
message per action kind present (so at most three):

    {"action": "created"|"updated"|"deleted", "role_id": "<first role id>",
     "role_ids": ["<id>", ...], "count": <n>}

`role_id` is in SNOWFLAKE_KEYS (stringified by the hub); `role_ids` is already a
list of strings. Consumers (the dashboard) just invalidate their role queries on
ANY `roles` message, so the extra fields are informational.

Guarantees (same as services.live_events.publish): `note()` never raises and
never blocks; when nobody subscribes to `roles` it does nothing at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

log = logging.getLogger(__name__)

TOPIC = "roles"
ACTIONS: tuple[str, ...] = ("created", "updated", "deleted")
DEFAULT_WINDOW = 0.5
DEFAULT_MAX_DELAY = 2.0


def merge_action(previous: str | None, new: str) -> str:
    """Combine two buffered actions for the same role (pure)."""
    if new == "deleted" or previous == "deleted":
        return "deleted"
    if previous == "created":
        return "created"
    return new


def build_messages(pending: dict[int, str]) -> list[dict]:
    """Group a {role_id: action} buffer into at most one message per action (pure).

    Order is created, updated, deleted; role ids keep their arrival order.
    """
    out: list[dict] = []
    for action in ACTIONS:
        ids = [rid for rid, act in pending.items() if act == action]
        if not ids:
            continue
        out.append(
            {
                "action": action,
                "role_id": ids[0],
                "role_ids": [str(rid) for rid in ids],
                "count": len(ids),
            }
        )
    return out


class RoleEventCoalescer:
    def __init__(
        self,
        publish: Callable[[str, dict], None],
        *,
        has_subscribers: Callable[[str], bool] | None = None,
        window: float = DEFAULT_WINDOW,
        max_delay: float = DEFAULT_MAX_DELAY,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._publish = publish
        self._has_subscribers = has_subscribers
        self.window = window
        self.max_delay = max_delay
        self._clock = clock
        self._pending: dict[int, str] = {}
        self._first_at: float | None = None
        self._handle: asyncio.TimerHandle | None = None

    @property
    def pending(self) -> dict[int, str]:
        return dict(self._pending)

    def note(self, action: str, role_id: int) -> None:
        """Buffer one role event and (re)arm the flush timer. Never raises."""
        try:
            if action not in ACTIONS:
                return
            if self._has_subscribers is not None and not self._has_subscribers(TOPIC):
                return
            self._pending[int(role_id)] = merge_action(self._pending.get(int(role_id)), action)
            now = self._clock()
            if self._first_at is None:
                self._first_at = now
            delay = min(self.window, max(0.0, self._first_at + self.max_delay - now))
            self._arm(delay)
        except Exception:
            log.warning("roles live: failed to buffer %s for %s", action, role_id, exc_info=True)

    def _arm(self, delay: float) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()  # no loop (sync caller / tests): publish immediately
            return
        if self._handle is not None:
            self._handle.cancel()
        self._handle = loop.call_later(delay, self.flush)

    def flush(self) -> None:
        """Publish everything buffered. Never raises."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        pending, self._pending, self._first_at = self._pending, {}, None
        if not pending:
            return
        try:
            for message in build_messages(pending):
                self._publish(TOPIC, message)
        except Exception:
            log.warning("roles live: publish failed", exc_info=True)

    def cancel(self) -> None:
        """Drop the buffer and the timer (cog unload)."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._pending.clear()
        self._first_at = None
