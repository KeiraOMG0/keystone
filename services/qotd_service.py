"""
QOTD Service — generates, validates, and manages Question of the Day.

Responsibilities:
- Calling Ollama to generate a question
- Running the moderation / validation pipeline
- Duplicate detection via embedding similarity (simple word-overlap fallback)
- Fallback to a hardcoded question list when AI fails
- Exposing generate() and post() for the cog to use
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import subprocess
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Awaitable, Callable

import aiohttp
import discord

from database.repositories.qotd_repo import QotdRepository

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Banned keyword list — covers NSFW, political, religious, invasive topics.
# Kept as a flat set for O(1) word lookup.
# ---------------------------------------------------------------------------
_BANNED_KEYWORDS: frozenset[str] = frozenset(
    {
        # sexual / nsfw
        "sex", "sexual", "nude", "naked", "porn", "nsfw", "fetish", "kink",
        "masturbat", "erotic", "orgasm", "penis", "vagina", "breast",
        # drugs / alcohol (minors present)
        "drugs", "cocaine", "heroin", "meth", "weed", "marijuana", "cannabis",
        "alcohol", "drunk", "drinking", "beer", "wine", "shots",
        # violence / self-harm
        "suicide", "self-harm", "cutting", "kill", "murder", "abuse",
        "assault", "rape", "torture",
        # politics
        "republican", "democrat", "trump", "biden", "election", "vote",
        "political", "politics", "politician", "liberal", "conservative",
        "fascist", "communist", "marxist",
        # religion — too divisive for icebreaker context
        "god", "jesus", "allah", "satan", "hell", "heaven", "bible",
        "quran", "torah", "church", "mosque", "synagogue", "atheist",
        "religion", "religious", "christian", "muslim", "jewish", "hindu",
        "buddhist",
        # invasive personal
        "salary", "income", "weight", "bmi", "body", "ugly", "fat",
        "race", "racist", "racism", "ethnic", "slur",
        # harassment bait
        "worst person", "hate", "loser", "stupid", "idiot",
    }
)

# ---------------------------------------------------------------------------
# Fallback question list — used when Ollama fails after all retries.
# 50 community-safe icebreaker questions.
# ---------------------------------------------------------------------------
FALLBACK_QUESTIONS: list[str] = [
    "What is a hobby you picked up recently that you never expected to enjoy?",
    "If you could live in any fictional universe, which one would you choose?",
    "What is the most useful skill you have taught yourself?",
    "What song instantly makes you feel better no matter your mood?",
    "If you could master any instrument overnight, which would you pick?",
    "What is a movie or show you can rewatch endlessly without getting bored?",
    "What is the best meal you have ever eaten?",
    "If you could wake up tomorrow in a different city, where would you go?",
    "What is a game (video, board, or card) you would recommend to anyone?",
    "What small thing always makes your day a little brighter?",
    "What is something you are genuinely proud of accomplishing?",
    "If you could have any animal as a companion, what would it be?",
    "What is the most interesting fact you have learned this week?",
    "What subject in school did you unexpectedly end up loving?",
    "If you had a free weekend with no responsibilities, how would you spend it?",
    "What is a book, podcast, or YouTube channel you have been enjoying lately?",
    "What is your go-to comfort food on a tough day?",
    "If you could add one new feature to Discord, what would it be?",
    "What is a talent or skill most people in this server probably do not know you have?",
    "What fictional character do you feel like you relate to the most?",
    "What is the best gift you have ever received?",
    "What is something on your bucket list you are determined to do someday?",
    "What would be the title of your autobiography if you wrote one today?",
    "If you could only eat one cuisine for the rest of your life, what would it be?",
    "What app on your phone do you use way more than you expected?",
    "What is the oldest thing you own that still means something to you?",
    "What is a skill you wish was taught in school but was not?",
    "What was your favourite thing to do on summer days as a kid?",
    "If you could have coffee with anyone from history, who would you pick?",
    "What is a compliment you received that stuck with you?",
    "What is your ideal weather and why?",
    "What is something that looked boring but turned out to be really fun?",
    "What is a smell that immediately transports you to a happy memory?",
    "What would your dream job look like if money were no object?",
    "What is the most creative solution you have come up with to a problem?",
    "What is a TV show you think is criminally underrated?",
    "If you could swap lives with someone for just one day, who would it be?",
    "What hobby have you always wanted to try but have not gotten around to yet?",
    "What is one thing you wish more people knew about you?",
    "What is the most spontaneous thing you have ever done?",
    "What is your favourite season and what do you love most about it?",
    "What is a movie, book, or song that changed the way you see the world?",
    "If you could design your perfect day from start to finish, what would it look like?",
    "What is something you recently learned that genuinely surprised you?",
    "What is the most memorable trip or adventure you have been on?",
    "What is something you wish you had started learning earlier?",
    "What is a technology from science fiction you wish existed today?",
    "What local hidden gem would you recommend to someone visiting your area?",
    "What is a tradition, habit, or ritual that makes your week better?",
    "What would you do if you had an extra hour every single day?",
]

# ---------------------------------------------------------------------------
# Ollama system prompt — engineered for small 3B–8B models.
# Explicit constraints prevent common small-model failure modes.
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """You are a QOTD (Question of the Day) bot for a friendly Discord server.
Your job is to generate exactly ONE icebreaker question to help community members get to know each other.

STRICT RULES — follow every single one:
- Output EXACTLY one question. Nothing else.
- End the question with a single question mark.
- Do NOT include numbering, bullet points, or labels.
- Do NOT add explanations, alternatives, or commentary.
- Do NOT output multiple questions or a question followed by a list.
- Do NOT roleplay, act as a character, or break the fourth wall.
- The question must be safe for all ages including 13-year-olds.
- Never ask about politics, religion, body image, income, drugs, alcohol, or anything NSFW.
- Make it a genuine icebreaker — fun, light, and easy for anyone to answer.
- Vary the topic each time: hobbies, food, travel, media, hypotheticals, memories, skills.

EXAMPLE OUTPUTS (one per response):
What is a skill you taught yourself that most people would find surprising?
If you could live inside any video game world, which one would you pick?
What is the most random talent you have?

Now generate a new, original QOTD question:"""


class ValidationError(Exception):
    pass


class HistoryLogError(Exception):
    """Raised when a QOTD posts successfully to Discord but the history record fails to save."""

    def __init__(self, message: discord.Message, original: Exception) -> None:
        super().__init__(str(original))
        self.message = message
        self.original = original


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return text.strip().strip('"\'').strip()


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"\b[a-z]{3,}\b", text.lower()))


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


# ---------------------------------------------------------------------------
# Daily scheduler helpers (cogs/qotd.py's _scheduler). Pure, so they are unit
# tested without Discord. Everything is UTC (post_hour/post_minute are UTC,
# qotd_history.posted_at and audit_log.created_at are SQLite UTC timestamps).
# ---------------------------------------------------------------------------

# audit_log action written when the daily approval request was successfully
# posted to the staff approval channel. It is the persisted dedupe key: the
# scheduler never sends a second approval request for the same UTC day, even
# across a bot restart (the in-memory _pending dict is lost on restart).
APPROVAL_SENT_ACTION = "qotd_approval_sent"


def parse_utc(ts: str | None) -> datetime | None:
    """SQLite 'YYYY-MM-DD HH:MM:SS' (or ISO) UTC text -> aware datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def day_bounds_utc(day: date) -> tuple[str, str]:
    """[start, end) of a UTC day as SQLite timestamp strings."""
    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")


def qotd_slot_due(
    now_utc: datetime,
    post_hour: int,
    post_minute: int,
    last_posted_at: str | None,
    approval_sent_today: bool,
) -> bool:
    """Whether today's daily QOTD approval request should be queued now.

    Due from the configured post time until the end of the same UTC day
    (catch-up: a bot that was down or restarting at the exact minute still
    queues it once when it comes back), unless a question was already posted
    today or today's approval request was already sent (the dedupe key)."""
    slot = now_utc.replace(hour=int(post_hour), minute=int(post_minute), second=0, microsecond=0)
    if now_utc < slot:
        return False
    last = parse_utc(last_posted_at)
    if last is not None and last.date() == now_utc.date():
        return False
    return not approval_sent_today


class DailyAttemptTracker:
    """In-memory retry budget for the daily approval request, per guild and
    UTC day. A transient failure (generation error, Discord 5xx) retries
    after `retry_delay`, up to `max_attempts`; a permanent one (Forbidden,
    missing approval channel) or a success stops further tries that day.
    Lost on restart by design: the persisted dedupe key is the
    APPROVAL_SENT_ACTION audit row, and one fresh retry after a restart is
    acceptable."""

    def __init__(self, max_attempts: int = 3, retry_delay: timedelta = timedelta(minutes=10)) -> None:
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self._state: dict[int, dict] = {}

    def _today(self, guild_id: int, now: datetime) -> dict:
        st = self._state.get(guild_id)
        if st is None or st["day"] != now.date():
            st = {"day": now.date(), "attempts": 0, "next": None, "stopped": False}
            self._state[guild_id] = st
        return st

    def may_try(self, guild_id: int, now: datetime) -> bool:
        st = self._today(guild_id, now)
        if st["stopped"] or st["attempts"] >= self.max_attempts:
            return False
        return st["next"] is None or now >= st["next"]

    def record_failure(self, guild_id: int, now: datetime, *, permanent: bool = False) -> None:
        st = self._today(guild_id, now)
        st["attempts"] += 1
        st["next"] = now + self.retry_delay
        if permanent:
            st["stopped"] = True

    def record_success(self, guild_id: int, now: datetime) -> None:
        st = self._today(guild_id, now)
        st["stopped"] = True


# ---------------------------------------------------------------------------
# Dashboard generation jobs. A generation can take minutes (cold model load,
# up to 3 attempts), far beyond Cloudflare's 100 s request limit, so the
# dashboard starts a job (fast 202) and polls it (fast GETs). Jobs live in
# memory on the bot; a bot restart loses them and the poll gets 404.
# ---------------------------------------------------------------------------

# Hard bound on one dashboard generation: past it, generation stops trying the
# model and returns a fallback question (see generate_question's deadline).
DASHBOARD_GENERATE_DEADLINE_SECONDS = 150.0


class GenerationJobs:
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
            oldest = min(self._jobs, key=lambda k: self._jobs[k]["_started"])
            self._jobs.pop(oldest, None)

    @staticmethod
    def public(job: dict) -> dict:
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def running_for(self, guild_id: int) -> dict | None:
        for job in self._jobs.values():
            if job["_guild_id"] == guild_id and job["status"] == "running":
                return job
        return None

    def start(
        self,
        guild_id: int,
        model: str | None,
        run: Callable[[], Awaitable[tuple[str, str, list[str]]]],
    ) -> dict:
        """Start a job running `run()` (-> (question, source, errors)). One
        running job per guild: a second start while one runs returns that
        job (with reused=True) instead of stacking model calls."""
        self._prune()
        existing = self.running_for(guild_id)
        if existing is not None:
            return {**self.public(existing), "reused": True}
        job_id = secrets.token_urlsafe(9)
        job = {
            "job_id": job_id, "status": "running", "model": model,
            "question": None, "source": None, "errors": [], "error": None,
            "_guild_id": guild_id, "_started": self._clock(), "_finished": None,
        }
        self._jobs[job_id] = job

        async def _runner() -> None:
            try:
                question, source, errors = await run()
                job.update(status="done", question=question, source=source, errors=list(errors or []))
            except Exception as exc:  # never leave a job "running" forever
                log.exception("qotd: dashboard generation job %s failed", job_id)
                job.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
            finally:
                job["_finished"] = self._clock()
                self._tasks.pop(job_id, None)

        self._tasks[job_id] = asyncio.get_running_loop().create_task(_runner(), name=f"qotd-generate-{job_id}")
        return {**self.public(job), "reused": False}

    def get(self, guild_id: int, job_id: str) -> dict | None:
        self._prune()
        job = self._jobs.get(job_id)
        if job is None or job["_guild_id"] != guild_id:
            return None
        return self.public(job)

    def cancel_all(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()


class QotdService:
    def __init__(
        self,
        repo: QotdRepository,
        *,
        ollama_url: str = "http://localhost:11434",
        ollama_exe: str | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._repo = repo
        self._ollama_url = ollama_url.rstrip("/")
        self._ollama_exe = ollama_exe
        self._max_retries = max_retries
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def _ensure_ollama(self) -> None:
        """Start Ollama if it isn't reachable, then wait up to 15s for it to be ready."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as s:
                async with s.get(f"{self._ollama_url}/api/tags") as r:
                    if r.status == 200:
                        return
        except Exception:
            pass

        log.info("qotd: Ollama not reachable — starting via 'ollama serve'")
        candidates = ["ollama"]
        if self._ollama_exe:
            candidates.insert(0, self._ollama_exe)
        ollama_exe = None
        for candidate in candidates:
            try:
                subprocess.run([candidate, "--version"], capture_output=True, timeout=3)
                ollama_exe = candidate
                break
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
        if ollama_exe is None:
            log.warning("qotd: ollama executable not found — skipping auto-start")
            return
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.Popen(
                    [ollama_exe, "start"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                ),
            )
        except FileNotFoundError:
            log.warning("qotd: ollama executable not found — skipping auto-start")
            return

        for _ in range(15):
            await asyncio.sleep(1)
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as s:
                    async with s.get(f"{self._ollama_url}/api/tags") as r:
                        if r.status == 200:
                            log.info("qotd: Ollama ready")
                            return
            except Exception:
                pass
        log.warning("qotd: Ollama did not respond within 15s after start")

    async def generate_question(
        self,
        guild_id: int,
        model: str | None = None,
        *,
        deadline_seconds: float | None = None,
    ) -> tuple[str, str, list[str]]:
        """
        Returns (question, source, errors) where source is 'ai' or 'fallback'.
        errors is empty on success; populated with per-attempt failure reasons on fallback.
        Ensures Ollama is running, then tries up to max_retries times before falling back.

        `deadline_seconds` bounds the whole call: once it is used up, the
        current model call is abandoned and the fallback pool is used (the
        dashboard passes DASHBOARD_GENERATE_DEADLINE_SECONDS).
        """
        if model is None:
            model = os.getenv("OLLAMA_MODEL") or "llama3.2:3b"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_seconds if deadline_seconds else None

        def _remaining() -> float | None:
            return None if deadline is None else deadline - loop.time()

        errors: list[str] = []
        try:
            await asyncio.wait_for(self._ensure_ollama(), timeout=_remaining())
        except asyncio.TimeoutError:
            errors.append("time limit reached while starting Ollama")
        recent = await self._repo.get_recent_questions(guild_id, limit=60)

        for attempt in range(1, self._max_retries + 1):
            remaining = _remaining()
            if remaining is not None and remaining <= 0:
                errors.append(f"attempt {attempt}: skipped, time limit reached")
                break
            try:
                raw = await asyncio.wait_for(self._call_ollama(model), timeout=remaining)
                question = self._validate(raw, recent)
                log.info("qotd: AI question generated on attempt %d: %s", attempt, question)
                return question, "ai", []
            except ValidationError as e:
                log.warning("qotd: attempt %d validation failed — %s", attempt, e)
                errors.append(f"attempt {attempt}: {e}")
            except Exception as e:
                msg = str(e) or type(e).__name__
                log.warning("qotd: attempt %d Ollama error — %s", attempt, msg)
                errors.append(f"attempt {attempt}: {msg}")

        error_lines = "\n".join(errors)
        log.warning(
            "qotd: all %d AI attempts failed — using fallback\n```\n%s\n```",
            self._max_retries,
            error_lines,
        )
        question = self._pick_fallback(recent)
        return question, "fallback", errors

    async def post_qotd(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        question: str,
        source: str,
        *,
        use_emojis: bool = False,
        ping_role_id: int | None = None,
    ) -> discord.Message:
        """Format and send the QOTD embed, then record it in history."""
        embed = self._build_embed(question, use_emojis=use_emojis)
        content = f"<@&{ping_role_id}>" if ping_role_id else None
        msg = await channel.send(content=content, embed=embed)
        try:
            await self._repo.add_history(
                guild.id,
                question,
                source,
                message_id=msg.id,
                channel_id=channel.id,
            )
        except Exception as e:
            log.error(
                "qotd: posted to %s/%s (source=%s) but failed to save history — %s",
                guild.name, channel.name, source, e,
            )
            raise HistoryLogError(msg, e) from e
        log.info("qotd: posted to %s/%s (source=%s)", guild.name, channel.name, source)
        return msg

    # ------------------------------------------------------------------
    # Ollama
    # ------------------------------------------------------------------

    async def _call_ollama(self, model: str) -> str:
        url = f"{self._ollama_url}/api/generate"
        payload = {
            "model": model,
            "system": _SYSTEM_PROMPT,
            "prompt": "Generate one QOTD question now.",
            "stream": False,
            "keep_alive": "30m",
            "options": {
                "temperature": 0.85,
                "top_p": 0.9,
                "top_k": 40,
                "num_predict": 80,
                "stop": ["\n", "?"],  # stop after first question mark is appended below
            },
        }
        # We stop at \n so the model can't ramble, but we need the ? so remove it from stop
        payload["options"]["stop"] = ["\n\n", "\n1.", "\nA:", "\n-"]

        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"Ollama HTTP {resp.status}: {body[:200]}")
                data = await resp.json()
                return data.get("response", "")

    # ------------------------------------------------------------------
    # Validation pipeline
    # ------------------------------------------------------------------

    def _validate(self, raw: str, recent: list[str]) -> str:
        if not raw or not raw.strip():
            raise ValidationError("empty response from model")

        text = _normalise(raw)

        # Strip any common preamble the model might output
        for prefix in ("question:", "qotd:", "here is", "here's", "sure!", "sure,", "of course"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()

        # Take only the first line/sentence
        text = text.split("\n")[0].strip()

        # Ensure it ends with exactly one question mark
        text = text.rstrip("!.?") + "?"

        if len(text) < 15:
            raise ValidationError(f"question too short ({len(text)} chars): {text!r}")

        if len(text) > 280:
            raise ValidationError(f"question too long ({len(text)} chars)")

        # Must contain exactly one question mark, at the end
        if text.count("?") != 1 or not text.endswith("?"):
            raise ValidationError(f"bad question mark placement: {text!r}")

        # Must not contain multiple sentences / questions
        sentence_ends = len(re.findall(r"[.!?]", text))
        if sentence_ends > 1:
            raise ValidationError(f"multiple sentences detected: {text!r}")

        # Banned keywords check (word-boundary match)
        words = _word_set(text)
        hits = words & _BANNED_KEYWORDS
        if hits:
            raise ValidationError(f"banned keyword(s): {hits}")

        # Duplicate / similarity check against recent history
        for prev in recent:
            sim = _similarity(text, prev)
            if sim > 0.75:
                raise ValidationError(f"too similar to recent question (sim={sim:.2f}): {prev!r}")

        # Overlap check on significant words
        sig_words = words - {"the", "and", "for", "you", "your", "that", "this", "with", "have", "would", "what", "which"}
        for prev in recent[:20]:
            prev_sig = _word_set(prev) - {"the", "and", "for", "you", "your", "that", "this", "with", "have", "would", "what", "which"}
            overlap = len(sig_words & prev_sig) / max(len(sig_words), 1)
            if overlap > 0.65:
                raise ValidationError(f"high word overlap ({overlap:.2f}) with recent: {prev!r}")

        return text

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    def _pick_fallback(self, recent: list[str]) -> str:
        """Pick the least recently used fallback question."""
        recent_lower = [q.lower() for q in recent]
        for q in FALLBACK_QUESTIONS:
            if q.lower() not in recent_lower:
                return q
        # All exhausted — just return the first one
        return FALLBACK_QUESTIONS[0]

    # ------------------------------------------------------------------
    # Embed builder
    # ------------------------------------------------------------------

    def _build_embed(self, question: str, *, use_emojis: bool = False) -> discord.Embed:
        title = "Question of the Day" if not use_emojis else "Question of the Day"
        embed = discord.Embed(
            title=title,
            description=question,
            color=discord.Color.from_rgb(114, 137, 218),
        )
        footer = "Reply in the thread below!"
        embed.set_footer(text=footer)
        return embed
