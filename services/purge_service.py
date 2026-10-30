"""
services/purge_service.py - `!purge` bounds, parsing and the purge itself,
shared by the prefix command (cogs/moderation.py) and the dashboard
(internal_api/routes/member_tools.py, a 202-style background job).

Bounds (the dashboard backend, server/routes/moderation.py, keeps its own copy
for its pydantic validation; keep them in sync):
  - a count of 1..PURGE_MAX (1000) messages, or
  - a time window "30s" / "15m" / "2h" / "1d" of at most PURGE_MAX_WINDOW
    (14 days: Discord only bulk-deletes messages younger than that), which
    deletes up to PURGE_MAX messages newer than the window;
  - an optional user filter by ID (works for users who already left);
  - more than PURGE_CONFIRM_ABOVE (100) messages, or any window, needs an
    explicit confirmation (a button in Discord, `confirm: true` from the
    dashboard).

PurgeJobs: purges can take a while (Discord rate limits, and messages older
than 14 days in a count purge are deleted one API call each), longer than the
dashboard request may wait behind Cloudflare's 100 s limit. The dashboard
starts a job and polls it, like QOTD generation. Jobs live in memory in the
bot process (a bot restart loses them; polling then 404s). One running purge
per channel.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

import discord

from services.log_router import build_embed

log = logging.getLogger(__name__)

MOD_PURGE_LOG_TEMPLATE_NAME = "mod-purge-log"

PURGE_MIN = 1
PURGE_MAX = 1000
# Purges that may delete more than this (or any time-window purge, whose size
# is unknown up front) ask the author to confirm first.
PURGE_CONFIRM_ABOVE = 100
# How many messages `!purge N @user` scans at most while looking for N
# messages by that user (purge's `limit` counts SCANNED messages).
PURGE_FILTERED_SCAN_LIMIT = 2000
# The window may reach back at most this far. Discord only bulk-deletes
# messages younger than 14 days; older ones go one API call each.
PURGE_MAX_WINDOW = timedelta(days=14)
_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
PURGE_USAGE = (
    "Usage: `!purge <count> [@user or user ID]` (1 to 1000) or "
    "`!purge <duration> [@user or user ID]` (e.g. `30s`, `15m`, `2h`, `1d`; up to 14 days)."
)


def purge_count_error(count: int) -> str | None:
    if count < PURGE_MIN or count > PURGE_MAX:
        return f"The purge count must be between {PURGE_MIN} and {PURGE_MAX}."
    return None


def parse_purge_amount(raw) -> tuple[int | None, timedelta | None, str | None]:
    """`!purge`'s first argument -> (count, window, error). Exactly one of
    count/window is set when error is None. "50" is a count; "15m" (s/m/h/d)
    a time window, which deletes up to PURGE_MAX messages newer than that."""
    text = str(raw).strip()
    if re.fullmatch(r"-?\d+", text):
        count = int(text)
        error = purge_count_error(count)
        return (None, None, error) if error else (count, None, None)
    match = _DURATION_RE.match(text)
    if not match:
        return None, None, PURGE_USAGE
    window = timedelta(**{_DURATION_UNITS[match.group(2).lower()]: int(match.group(1))})
    if window <= timedelta(0) or window > PURGE_MAX_WINDOW:
        return None, None, "The purge window must be between 1 second and 14 days."
    return None, window, None


def needs_confirm(count: int | None, window: timedelta | None) -> bool:
    """Same rule as the Confirm button of `!purge`."""
    return window is not None or (count is not None and count > PURGE_CONFIRM_ABOVE)


def describe_window(window: timedelta) -> str:
    """timedelta -> "15 minutes" / "2 hours" / "1 day" (largest whole unit)."""
    seconds = int(window.total_seconds())
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def describe_filter(count: int | None, window: timedelta | None, user_id: int | None) -> str:
    """{purgefilter}: "last 50 messages", "messages from the last 15 minutes", "... from <@id>"."""
    text = f"messages from the last {describe_window(window)}" if window else f"last {count} messages"
    return text + (f" from <@{user_id}>" if user_id else "")


def make_purge_check(count: int, user_id: int | None = None):
    """Check for TextChannel.purge that accepts at most `count` messages
    (optionally only by `user_id`)."""
    matched = 0

    def check(message) -> bool:
        nonlocal matched
        if matched >= count:
            return False
        if user_id is not None and getattr(getattr(message, "author", None), "id", None) != user_id:
            return False
        matched += 1
        return True

    return check


async def purge_channel(
    channel,
    *,
    count: int | None,
    window: timedelta | None,
    user_id: int | None,
    reason: str,
    before=None,
) -> list:
    """Run the purge exactly like `!purge`. With a user filter, scan further
    back so N of THAT user's messages are found, not N scanned. A time window
    walks newest-first, so hitting the PURGE_MAX cap leaves the oldest
    messages of the window, not the newest. discord.Forbidden /
    HTTPException propagate to the caller."""
    if window is not None:
        return await channel.purge(
            limit=PURGE_FILTERED_SCAN_LIMIT if user_id else PURGE_MAX,
            before=before,
            after=discord.utils.utcnow() - window,
            oldest_first=False,
            check=make_purge_check(PURGE_MAX, user_id),
            reason=reason,
        )
    assert count is not None
    return await channel.purge(
        limit=PURGE_FILTERED_SCAN_LIMIT if user_id else count,
        before=before,
        check=make_purge_check(count, user_id),
        reason=reason,
    )


def purge_audit_detail(count: int, user_id: int | None, window: timedelta | None, source: str | None = None) -> dict:
    detail: dict[str, Any] = {
        "count": count, "filtered_user_id": user_id,
        "window_seconds": int(window.total_seconds()) if window else None,
    }
    if source:
        detail["source"] = source
    return detail


def purge_log_embed(channel, actor_id: int, count: int, user_id: int | None, window: timedelta | None) -> discord.Embed:
    """Fallback mod-log embed for a purge (same fields as `!purge`)."""
    return build_embed(
        "messages_purged", actor_id=actor_id,
        extra_fields={
            "Channel": channel.mention,
            "Messages Deleted": str(count),
            **({"Filtered User": f"<@{user_id}>"} if user_id else {}),
            **({"Window": f"Last {describe_window(window)}"} if window else {}),
        },
    )


# ---------------------------------------------------------------------------
# Dashboard purge jobs
# ---------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PurgeJobs:
    """In-memory job table for dashboard purges (one running job per channel)."""

    JOB_TTL_SECONDS = 15 * 60
    MAX_JOBS = 50

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._jobs: dict[str, dict] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._clock = clock

    def _prune(self) -> None:
        now = self._clock()
        for job_id, job in list(self._jobs.items()):
            done_at = job.get("_finished")
            if done_at is not None and now - done_at > self.JOB_TTL_SECONDS:
                self._jobs.pop(job_id, None)
        while len(self._jobs) > self.MAX_JOBS:
            finished = [k for k, j in self._jobs.items() if j["status"] != "running"]
            if not finished:
                break
            oldest = min(finished, key=lambda k: self._jobs[k]["_started"])
            self._jobs.pop(oldest, None)

    @staticmethod
    def public(job: dict) -> dict:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def running_for_channel(self, guild_id: int, channel_id: int) -> dict | None:
        for job in self._jobs.values():
            if job["_guild_id"] == guild_id and job["channel_id"] == channel_id and job["status"] == "running":
                return job
        return None

    def start(
        self,
        guild_id: int,
        channel_id: int,
        run: Callable[[], Awaitable[int]],
        *,
        summary: dict | None = None,
    ) -> dict | None:
        """Start `run()` (-> number of deleted messages) as a background task.
        Returns the public job, or None when a purge already runs in that
        channel (the caller answers 409)."""
        self._prune()
        if self.running_for_channel(guild_id, channel_id) is not None:
            return None
        job_id = secrets.token_urlsafe(9)
        job = {
            "job_id": job_id, "status": "running", "channel_id": channel_id,
            "deleted": None, "error": None, "started_at": _iso_now(), "finished_at": None,
            **(summary or {}),
            "_guild_id": guild_id, "_started": self._clock(), "_finished": None,
        }
        self._jobs[job_id] = job

        async def _runner() -> None:
            try:
                deleted = await run()
                job.update(status="done", deleted=int(deleted))
            except discord.Forbidden:
                job.update(status="failed", error="Discord refused: the bot is missing Manage Messages or Read Message History in that channel.")
            except discord.HTTPException as exc:
                job.update(status="failed", error=f"Discord rejected the purge: {getattr(exc, 'text', '') or exc}"[:300])
            except Exception as exc:  # never leave a job "running" forever
                log.exception("purge: dashboard job %s failed", job_id)
                job.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
            finally:
                job["finished_at"] = _iso_now()
                job["_finished"] = self._clock()
                self._tasks.pop(job_id, None)

        self._tasks[job_id] = asyncio.get_running_loop().create_task(_runner(), name=f"purge-{job_id}")
        return self.public(job)

    def get(self, guild_id: int, job_id: str) -> dict | None:
        self._prune()
        job = self._jobs.get(job_id)
        if job is None or job["_guild_id"] != guild_id:
            return None
        return self.public(job)


# One table per bot process (the internal API runs in the bot's event loop).
JOBS = PurgeJobs()
