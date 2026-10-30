"""QOTD daily scheduler catch-up/dedupe and dashboard generation jobs
(services/qotd_service.py, cogs/qotd.py). Pure: mocks only, no Discord/Ollama."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord

from services.qotd_service import (
    APPROVAL_SENT_ACTION,
    DailyAttemptTracker,
    GenerationJobs,
    day_bounds_utc,
    qotd_slot_due,
)

GUILD_ID = 100000000000000001
APPROVAL_CH = 200000000000000003


def utc(h, mi=0, d=14):
    return datetime(2026, 10, d, h, mi, tzinfo=timezone.utc)


# --- qotd_slot_due --------------------------------------------------------------
def test_not_due_before_post_time():
    assert qotd_slot_due(utc(13, 59), 14, 0, None, False) is False


def test_due_at_and_after_post_time_same_day_catch_up():
    assert qotd_slot_due(utc(14, 0), 14, 0, None, False) is True
    assert qotd_slot_due(utc(18, 30), 14, 0, None, False) is True  # bot was down at 14:00
    assert qotd_slot_due(utc(23, 59), 14, 0, None, False) is True


def test_not_due_when_already_posted_today_or_approval_sent():
    assert qotd_slot_due(utc(15), 14, 0, "2026-10-14 08:00:00", False) is False
    assert qotd_slot_due(utc(15), 14, 0, "2026-10-13 14:05:00", False) is True
    assert qotd_slot_due(utc(15), 14, 0, None, True) is False


def test_next_day_is_a_new_slot():
    assert qotd_slot_due(utc(0, 5, d=15), 14, 0, "2026-10-14 14:05:00", False) is False
    assert qotd_slot_due(utc(14, 1, d=15), 14, 0, "2026-10-14 14:05:00", False) is True


def test_day_bounds():
    assert day_bounds_utc(date(2026, 12, 31)) == ("2026-12-31 00:00:00", "2027-01-01 00:00:00")


# --- DailyAttemptTracker ----------------------------------------------------------
def test_tracker_retries_then_gives_up_and_resets_next_day():
    t = DailyAttemptTracker(max_attempts=2, retry_delay=timedelta(minutes=10))
    now = utc(14)
    assert t.may_try(GUILD_ID, now)
    t.record_failure(GUILD_ID, now)
    assert not t.may_try(GUILD_ID, now + timedelta(minutes=5))
    assert t.may_try(GUILD_ID, now + timedelta(minutes=10))
    t.record_failure(GUILD_ID, now + timedelta(minutes=10))
    assert not t.may_try(GUILD_ID, now + timedelta(hours=2))
    assert t.may_try(GUILD_ID, utc(14, d=15))


def test_tracker_permanent_failure_and_success_stop_for_the_day():
    t = DailyAttemptTracker()
    t.record_failure(GUILD_ID, utc(14), permanent=True)
    assert not t.may_try(GUILD_ID, utc(20))
    t2 = DailyAttemptTracker()
    t2.record_success(GUILD_ID, utc(14))
    assert not t2.may_try(GUILD_ID, utc(14, 1))


# --- cog scheduler: catch-up, dedupe, Forbidden ---------------------------------
def _make_cog(*, approval_sent=False, last_posted=None, send_side_effect=None):
    from cogs.qotd import QotdCog

    cog = object.__new__(QotdCog)
    cog.bot = MagicMock()
    cog.bot.user.id = 42
    cog._pending = {}
    cog._attempts = DailyAttemptTracker()
    cog._repo = MagicMock()
    cog._repo.get_config = AsyncMock(return_value={
        "enabled": 1, "channel_id": 1, "post_hour": 14, "post_minute": 0,
        "approval_channel_id": APPROVAL_CH, "staff_ping_role_id": None, "model": "m",
    })
    cog._repo.get_last_posted_at = AsyncMock(return_value=last_posted)
    cog._repo.approval_sent_between = AsyncMock(return_value=approval_sent)
    cog._service = MagicMock()
    cog._service.generate_question = AsyncMock(return_value=("What is fun?", "ai", []))
    cog._audit = MagicMock()
    cog._audit.log = AsyncMock()

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = APPROVAL_CH
    channel.send = AsyncMock(return_value=MagicMock(id=999), side_effect=send_side_effect)
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_channel = MagicMock(return_value=channel)
    return cog, guild, channel


async def test_catch_up_sends_once_and_records_dedupe_marker():
    cog, guild, channel = _make_cog()
    await cog._maybe_queue_approval(guild, utc(17, 30))  # post time 14:00 passed
    channel.send.assert_awaited_once()
    assert cog._pending[GUILD_ID] is not None
    action = cog._audit.log.await_args[0][2]
    assert action == APPROVAL_SENT_ACTION
    # Same process, later tick: pending blocks it.
    await cog._maybe_queue_approval(guild, utc(17, 31))
    assert channel.send.await_count == 1


async def test_restart_with_marker_never_double_posts():
    cog, guild, channel = _make_cog(approval_sent=True)  # fresh process, marker in audit_log
    await cog._maybe_queue_approval(guild, utc(14, 0))
    await cog._maybe_queue_approval(guild, utc(16, 0))
    channel.send.assert_not_awaited()
    cog._service.generate_question.assert_not_awaited()


async def test_already_posted_today_skips():
    cog, guild, channel = _make_cog(last_posted="2026-10-14 09:00:00")
    await cog._maybe_queue_approval(guild, utc(14, 0))
    channel.send.assert_not_awaited()


async def test_forbidden_clears_pending_and_gives_up_for_today():
    forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "no")
    cog, guild, channel = _make_cog(send_side_effect=forbidden)
    await cog._maybe_queue_approval(guild, utc(14, 0))
    assert GUILD_ID not in cog._pending  # previously stuck here until restart
    await cog._maybe_queue_approval(guild, utc(14, 1))
    assert channel.send.await_count == 1
    # Next day it tries again.
    channel.send.side_effect = None
    await cog._maybe_queue_approval(guild, utc(14, 0, d=15))
    assert channel.send.await_count == 2


async def test_transient_send_error_retries_after_delay():
    err = discord.HTTPException(MagicMock(status=500, reason="err"), "boom")
    cog, guild, channel = _make_cog(send_side_effect=err)
    await cog._maybe_queue_approval(guild, utc(14, 0))
    await cog._maybe_queue_approval(guild, utc(14, 5))
    assert channel.send.await_count == 1
    channel.send.side_effect = None
    await cog._maybe_queue_approval(guild, utc(14, 11))
    assert channel.send.await_count == 2


async def test_missing_approval_channel_skips_without_default():
    cog, guild, channel = _make_cog()
    cog._repo.get_config.return_value["approval_channel_id"] = None
    await cog._maybe_queue_approval(guild, utc(14, 0))
    channel.send.assert_not_awaited()
    cog._service.generate_question.assert_not_awaited()


async def test_scheduler_loop_survives_errors():
    from cogs.qotd import QotdCog

    cog, guild, _ = _make_cog()
    cog._repo.get_config = AsyncMock(side_effect=RuntimeError("db"))
    cog.bot.guilds = [guild]
    await QotdCog._scheduler.coro(cog)  # must not raise
    assert GUILD_ID not in cog._pending


# --- GenerationJobs -----------------------------------------------------------------
async def test_generation_job_lifecycle_and_reuse():
    jobs = GenerationJobs()
    gate = asyncio.Event()

    async def run():
        await gate.wait()
        return ("Q?", "ai", [])

    job = jobs.start(GUILD_ID, "m", run)
    assert job["status"] == "running" and job["reused"] is False
    again = jobs.start(GUILD_ID, "m", run)
    assert again["job_id"] == job["job_id"] and again["reused"] is True
    gate.set()
    await asyncio.sleep(0.01)
    done = jobs.get(GUILD_ID, job["job_id"])
    assert done["status"] == "done" and done["question"] == "Q?"
    assert jobs.get(GUILD_ID + 1, job["job_id"]) is None  # other guild


async def test_generation_job_failure_is_reported():
    jobs = GenerationJobs()

    async def run():
        raise RuntimeError("ollama down")

    job = jobs.start(GUILD_ID, None, run)
    await asyncio.sleep(0.01)
    got = jobs.get(GUILD_ID, job["job_id"])
    assert got["status"] == "failed" and "ollama down" in got["error"]


async def test_generate_question_deadline_falls_back():
    from services.qotd_service import QotdService

    repo = MagicMock()
    repo.get_recent_questions = AsyncMock(return_value=[])
    svc = QotdService(repo)
    svc._ensure_ollama = AsyncMock()

    async def slow(model):
        await asyncio.sleep(5)
        return "never"

    svc._call_ollama = slow
    q, source, errors = await svc.generate_question(GUILD_ID, "m", deadline_seconds=0.05)
    assert source == "fallback" and q and errors
