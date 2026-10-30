"""
Dashboard control of the music player (cogs/music.py): the pieces that are not
tied to discord.py voice objects, so they can be unit-tested on their own.

* ``MusicActionError``: a refusal with an error code, a human message and the
  HTTP status the internal API answers with (``internal_api/routes/music.py``).
* ``MusicJobs``: in-memory background jobs for the slow parts (resolving a URL
  or a search with yt-dlp, ``!debugurl``). The dashboard starts a job (fast
  202) and polls it, so nothing waits on yt-dlp inside one HTTP request
  (Cloudflare cuts requests at 100 s; MUSIC_EXTRACT_TIMEOUT alone is 90 s).
  Jobs live on the bot; a bot restart loses them and the poll gets 404.
* Validation helpers shared by the cog and the route (playlist names, bounds).

The player state itself stays on the MusicCog instance (one guild, one voice
client); the cog's ``dashboard_*`` methods are the business logic and the
Discord prefix commands call the same core methods.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

# Bounds the dashboard enforces (the `!volume` command clamps to the same range).
VOLUME_MIN = 0
VOLUME_MAX = 200
QUERY_MAX_LEN = 500
PLAYLIST_NAME_MAX_LEN = 64
# How many queue entries the state payload carries (the total is always sent).
STATE_QUEUE_LIMIT = 200
TITLE_MAX_LEN = 200

# Simple control actions accepted by POST /music/control.
CONTROL_ACTIONS: tuple[str, ...] = (
    "pause",
    "resume",
    "skip",
    "stop",
    "previous",
    "shuffle",
    "clear",
    "loop",
    "autoplay",
    "volume",
    "remove",
    "move",
    "join",
    "leave",
)

_PLAYLIST_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


class MusicActionError(Exception):
    """A refused dashboard music action. ``status`` is the HTTP status the
    internal API answers with; ``code`` is stable for the frontend."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_detail(self) -> dict:
        return {"error": self.code, "message": self.message}


def safe_playlist_name(name: str) -> str:
    """Same rule as MusicCog._playlist_path: keep letters, digits, `_` and `-`.
    Raises MusicActionError when nothing is left or the name is too long."""
    raw = (name or "").strip()
    if len(raw) > PLAYLIST_NAME_MAX_LEN:
        raise MusicActionError("invalid_playlist_name", f"Playlist names are at most {PLAYLIST_NAME_MAX_LEN} characters.", 400)
    safe = _PLAYLIST_SAFE_RE.sub("", raw)
    if not safe:
        raise MusicActionError("invalid_playlist_name", "Use letters, numbers, `_` or `-` only.", 400)
    return safe


def clean_query(query: str) -> str:
    q = (query or "").strip()
    if not q:
        raise MusicActionError("empty_query", "Enter a URL or a search query.", 400)
    if len(q) > QUERY_MAX_LEN:
        raise MusicActionError("query_too_long", f"Keep it under {QUERY_MAX_LEN} characters.", 400)
    if any(ch in q for ch in "\r\n\x00"):
        raise MusicActionError("invalid_query", "The query must be a single line.", 400)
    return q


def check_volume(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MusicActionError("invalid_volume", f"Volume must be a whole number from {VOLUME_MIN} to {VOLUME_MAX}.", 400)
    if not VOLUME_MIN <= value <= VOLUME_MAX:
        raise MusicActionError("invalid_volume", f"Volume must be from {VOLUME_MIN} to {VOLUME_MAX}.", 400)
    return value


def short(text: Any, limit: int = TITLE_MAX_LEN) -> str:
    s = str(text if text is not None else "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


class MusicJobs:
    """Background jobs for the dashboard (one dict per job, polled by id).

    ``start(guild_id, kind, run)`` runs ``run()`` as a task; its return value
    (a JSON-able dict) becomes ``result``. A ``MusicActionError`` becomes
    ``status="failed"`` with its code/message; anything else is logged and
    reported generically (never the raw exception text, which can carry
    stream URLs or paths). At most ``MAX_RUNNING`` jobs run per guild.
    """

    JOB_TTL_SECONDS = 15 * 60
    MAX_JOBS = 100
    MAX_RUNNING = 4

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
            pool = finished or list(self._jobs)
            oldest = min(pool, key=lambda k: self._jobs[k]["_started"])
            self._jobs.pop(oldest, None)

    @staticmethod
    def public(job: dict) -> dict:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def running_count(self, guild_id: int) -> int:
        return sum(1 for j in self._jobs.values() if j["_guild_id"] == guild_id and j["status"] == "running")

    def start(
        self,
        guild_id: int,
        kind: str,
        run: Callable[[], Awaitable[dict]],
        *,
        actor_id: int = 0,
        private: Optional[dict] = None,
    ) -> dict:
        self._prune()
        if self.running_count(guild_id) >= self.MAX_RUNNING:
            raise MusicActionError("busy", "Too many music lookups are running. Wait for one to finish.", 429)
        job_id = secrets.token_urlsafe(9)
        job: dict = {
            "job_id": job_id,
            "kind": kind,
            "status": "running",
            "result": None,
            "error": None,
            "message": None,
            "_guild_id": guild_id,
            "_actor_id": actor_id,
            "_started": self._clock(),
            "_finished": None,
            # Kept by reference: run() may fill it (e.g. search results that a
            # later pick reads server-side, never from the browser).
            "_private": private if private is not None else {},
        }
        self._jobs[job_id] = job

        async def _runner() -> None:
            try:
                result = await run()
                job.update(status="done", result=result)
            except MusicActionError as exc:
                job.update(status="failed", error=exc.code, message=exc.message)
            except asyncio.CancelledError:
                job.update(status="failed", error="cancelled", message="The bot stopped this lookup.")
                raise
            except Exception:
                log.exception("music: dashboard %s job %s failed", kind, job_id)
                job.update(status="failed", error="internal_error", message="Something went wrong on the bot. Check the bot log.")
            finally:
                job["_finished"] = self._clock()
                self._tasks.pop(job_id, None)

        self._tasks[job_id] = asyncio.get_running_loop().create_task(_runner(), name=f"music-{kind}-{job_id}")
        return self.public(job)

    def get(self, guild_id: int, job_id: str) -> Optional[dict]:
        self._prune()
        job = self._jobs.get(job_id)
        if job is None or job["_guild_id"] != guild_id:
            return None
        return self.public(job)

    def raw(self, guild_id: int, job_id: str) -> Optional[dict]:
        """The full job (with private fields), for server-side follow-ups such
        as picking a search result. Never return this over the wire."""
        job = self._jobs.get(job_id)
        if job is None or job["_guild_id"] != guild_id:
            return None
        return job

    def cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
