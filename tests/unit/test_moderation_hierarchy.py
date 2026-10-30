"""Rank checks and input bounds in services/moderation_service.py, plus the
warning repo/service changes (delwarn ownership, actor recorded on removal).
Fake discord objects, in-memory SQLite. No network."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio

import config
from database.connection import Database
from database.repositories.warning_repo import WarningRepository
from services.moderation_service import (
    MAX_TIMEOUT_MINUTES,
    ModerationBlocked,
    ModerationInputError,
    ModerationService,
    actor_is_owner,
    member_block_reason,
)
from services.warning_service import WarningService


SQL_DIR = pathlib.Path(__file__).resolve().parents[2] / "database" / "migrations" / "sql"
GUILD_ID = 100000000000000001
BOT_ID = 900000000000000001
GUILD_OWNER = 800000000000000008
CFG_OWNER = 700000000000000007


def role(pos: int):
    return SimpleNamespace(position=pos)


def member(uid: int, pos: int):
    m = SimpleNamespace(id=uid, top_role=role(pos), name=f"u{uid}", display_name=f"U{uid}", roles=[])
    m.kick = AsyncMock()
    m.timeout = AsyncMock()
    return m


def guild(bot_pos: int = 10):
    return SimpleNamespace(
        id=GUILD_ID, owner_id=GUILD_OWNER, me=member(BOT_ID, bot_pos),
        ban=AsyncMock(), unban=AsyncMock(), get_channel=lambda cid: None,
    )


def service():
    repo = SimpleNamespace(add_action=AsyncMock(return_value=1))
    audit = SimpleNamespace(log=AsyncMock())
    return ModerationService(repo, audit), repo, audit


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(config, "OWNER_ID", CFG_OWNER)


async def test_kick_blocked_when_actor_not_above_target():
    svc, repo, audit = service()
    g = guild()
    actor, target = member(1, 5), member(2, 5)
    with pytest.raises(ModerationBlocked) as exc:
        await svc.kick(target, g, actor, "r", check_hierarchy=True)
    assert "at or above yours" in exc.value.reason
    target.kick.assert_not_awaited()
    repo.add_action.assert_not_awaited()
    audit.log.assert_not_awaited()


async def test_kick_allowed_when_actor_above_target():
    svc, _, _ = service()
    target = member(2, 4)
    await svc.kick(target, guild(), member(1, 5), "r", check_hierarchy=True)
    target.kick.assert_awaited_once()


async def test_owner_skips_only_actor_rank():
    svc, _, _ = service()
    g = guild(bot_pos=10)
    # config owner with a low role can act on a higher member...
    target = member(2, 8)
    await svc.timeout(target, g, member(CFG_OWNER, 1), 10, None, check_hierarchy=True)
    target.timeout.assert_awaited_once()
    # ...but never above the bot, and never on the guild owner.
    with pytest.raises(ModerationBlocked, match="above mine"):
        await svc.timeout(member(3, 10), g, member(CFG_OWNER, 1), 10, None, check_hierarchy=True)
    with pytest.raises(ModerationBlocked, match="owner"):
        await svc.untimeout(member(GUILD_OWNER, 1), g, member(CFG_OWNER, 1), None, check_hierarchy=True)
    # The guild owner counts as owner too.
    assert actor_is_owner(g, GUILD_OWNER) and actor_is_owner(g, CFG_OWNER) and not actor_is_owner(g, 5)


async def test_identity_rules():
    g = guild()
    me = member(1, 9)
    assert "yourself" in member_block_reason(g, me, me)
    assert "myself" in member_block_reason(g, me, g.me)


async def test_ban_non_member_only_identity_rules():
    svc, _, _ = service()
    g = guild()
    # A low-ranked actor may ban a user who is not in the guild (no top role).
    await svc.ban(g, 12345, member(1, 1), "spam", member=None, check_hierarchy=True)
    g.ban.assert_awaited_once()
    with pytest.raises(ModerationBlocked):
        await svc.ban(g, GUILD_OWNER, member(1, 1), None, member=None, check_hierarchy=True)


async def test_ban_member_gets_rank_check():
    svc, _, _ = service()
    g = guild()
    with pytest.raises(ModerationBlocked):
        await svc.ban(g, 2, member(1, 3), None, member=member(2, 3), check_hierarchy=True)
    g.ban.assert_not_awaited()


async def test_bot_initiated_paths_skip_check():
    """Honeypot / auto-kick call without check_hierarchy (default False)."""
    svc, _, _ = service()
    target = member(2, 50)
    await svc.kick(target, guild(), SimpleNamespace(id=BOT_ID), "Honeypot")
    target.kick.assert_awaited_once()


@pytest.mark.parametrize("minutes", [0, -5, MAX_TIMEOUT_MINUTES + 1, True, 1.5])
async def test_timeout_bounds(minutes):
    svc, _, _ = service()
    target = member(2, 1)
    with pytest.raises(ModerationInputError):
        await svc.timeout(target, guild(), member(1, 5), minutes, None)
    target.timeout.assert_not_awaited()


async def test_timeout_max_allowed():
    svc, _, _ = service()
    target = member(2, 1)
    await svc.timeout(target, guild(), member(1, 5), MAX_TIMEOUT_MINUTES, None)
    target.timeout.assert_awaited_once()


async def test_reason_and_delete_days_bounds():
    svc, _, _ = service()
    g = guild()
    with pytest.raises(ModerationInputError, match="too long"):
        await svc.kick(member(2, 1), g, member(1, 5), "x" * 513)
    await svc.kick(member(3, 1), g, member(1, 5), "x" * 512)
    with pytest.raises(ModerationInputError, match="0 and 7"):
        await svc.ban(g, 4, member(1, 5), None, delete_message_days=8)
    with pytest.raises(ModerationInputError):
        await svc.ban(g, 4, member(1, 5), None, delete_message_days=-1)
    g.ban.assert_not_awaited()


# ---------------------------------------------------------------------------
# Warnings: delwarn ownership + actor recorded
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def wdb():
    db = Database(":memory:")
    await db.connect()
    await db.executescript((SQL_DIR / "018_warnings.sql").read_text(encoding="utf-8"))
    yield db
    await db.close()


async def test_remove_warning_respects_user(wdb):
    repo = WarningRepository(wdb)
    wid = await repo.add_warning(GUILD_ID, 111, 9, "rude")
    # Wrong user: nothing deleted.
    assert await repo.remove_warning(GUILD_ID, wid, 222) is None
    assert len(await repo.get_warnings(GUILD_ID, 111)) == 1
    row = await repo.remove_warning(GUILD_ID, wid, 111)
    assert row and row["user_id"] == 111 and row["reason"] == "rude"
    assert await repo.get_warnings(GUILD_ID, 111) == []
    assert await repo.remove_warning(GUILD_ID, wid) is None


async def test_warning_service_records_actor(wdb):
    audit = SimpleNamespace(log=AsyncMock())
    svc = WarningService(WarningRepository(wdb), audit)
    wid = await svc.add_warning(GUILD_ID, 111, 9, "rude")
    audit.log.assert_awaited_with(
        GUILD_ID, 9, "member_warned", target_type="member", target_id=111,
        detail={"warning_id": wid, "reason": "rude"},
    )
    assert await svc.remove_warning(GUILD_ID, wid, user_id=222, actor_id=55) is None
    removed = await svc.remove_warning(GUILD_ID, wid, user_id=111, actor_id=55)
    assert removed["id"] == wid
    audit.log.assert_awaited_with(
        GUILD_ID, 55, "warning_removed", target_type="member", target_id=111,
        detail={"warning_id": wid, "reason": "rude"},
    )


async def test_warning_service_without_audit_still_works(wdb):
    svc = WarningService(WarningRepository(wdb))
    wid = await svc.add_warning(GUILD_ID, 111, 9, None)
    assert await svc.remove_warning(GUILD_ID, wid)


# ---------------------------------------------------------------------------
# Purge helpers (cogs/moderation.py)
# ---------------------------------------------------------------------------

def _msg(author_id):
    return SimpleNamespace(author=SimpleNamespace(id=author_id))


def test_purge_check_counts_exactly_n():
    from cogs.moderation import make_purge_check

    check = make_purge_check(3)
    assert [check(_msg(1)) for _ in range(5)] == [True, True, True, False, False]


def test_purge_check_filters_user():
    from cogs.moderation import make_purge_check

    check = make_purge_check(2, user_id=7)
    results = [check(_msg(a)) for a in (1, 7, 2, 7, 7, 3)]
    assert results == [False, True, False, True, False, False]


def test_purge_count_bounds():
    from cogs.moderation import purge_count_error

    assert purge_count_error(0) and purge_count_error(1001) and purge_count_error(-1)
    assert purge_count_error(1) is None and purge_count_error(1000) is None


def test_parse_purge_amount():
    from datetime import timedelta

    from cogs.moderation import describe_window, parse_purge_amount

    assert parse_purge_amount("50") == (50, None, None)
    assert parse_purge_amount(5) == (5, None, None)
    assert parse_purge_amount("15m") == (None, timedelta(minutes=15), None)
    assert parse_purge_amount("2H") == (None, timedelta(hours=2), None)
    assert parse_purge_amount("14d")[1] == timedelta(days=14)
    for bad in ("0", "1001", "-3", "15d", "0m", "abc", "5w", "1.5h"):
        count, window, error = parse_purge_amount(bad)
        assert count is None and window is None and error, bad
    assert describe_window(timedelta(minutes=15)) == "15 minutes"
    assert describe_window(timedelta(hours=1)) == "1 hour"
    assert describe_window(timedelta(seconds=90)) == "90 seconds"
    assert describe_window(timedelta(days=2)) == "2 days"


def _purge_ctx(purge):
    cmd_message = SimpleNamespace(delete=AsyncMock())
    prompt = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
    channel = SimpleNamespace(id=5, mention="#c", purge=purge)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID, get_member=lambda uid: None), author=SimpleNamespace(id=1),
        channel=channel, message=cmd_message, reply=AsyncMock(return_value=prompt),
        send=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
    )
    return ctx, cmd_message, prompt


def _purge_cog(monkeypatch, confirmed: bool):
    from cogs import moderation as mod

    cog = mod.ModerationCog.__new__(mod.ModerationCog)
    cog.bot = SimpleNamespace(permission_service=SimpleNamespace(has_permission=AsyncMock(return_value=True)))
    cog._audit = SimpleNamespace(log=AsyncMock())
    cog._send_mod_log = AsyncMock()
    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock())
    views = []

    class FakeConfirm:
        def __init__(self, timeout, allowed_user_id):
            self.allowed_user_id = allowed_user_id
            self.confirmed = confirmed
            views.append(self)

        async def wait(self):
            return None

    monkeypatch.setattr(mod, "ConfirmView", FakeConfirm)
    return mod, cog, views


async def test_purge_small_count_needs_no_confirm(monkeypatch):
    mod, cog, views = _purge_cog(monkeypatch, confirmed=True)
    purge = AsyncMock(return_value=[1, 2])
    ctx, *_ = _purge_ctx(purge)
    await mod.ModerationCog.purge_cmd.callback(cog, ctx, "100")
    assert views == [] and purge.await_args.kwargs["limit"] == 100


async def test_purge_large_count_confirmed(monkeypatch):
    mod, cog, views = _purge_cog(monkeypatch, confirmed=True)
    purge = AsyncMock(return_value=list(range(250)))
    ctx, cmd_message, prompt = _purge_ctx(purge)
    await mod.ModerationCog.purge_cmd.callback(cog, ctx, "250")
    assert len(views) == 1 and views[0].allowed_user_id == 1
    kw = purge.await_args.kwargs
    assert kw["limit"] == 250 and kw["before"] is cmd_message and "after" not in kw
    prompt.delete.assert_awaited_once()
    cmd_message.delete.assert_awaited_once()
    ctx.send.assert_awaited_with("Deleted 250 message(s).")


async def test_purge_cancelled_deletes_nothing(monkeypatch):
    mod, cog, _ = _purge_cog(monkeypatch, confirmed=False)
    purge = AsyncMock()
    ctx, cmd_message, prompt = _purge_ctx(purge)
    await mod.ModerationCog.purge_cmd.callback(cog, ctx, "500")
    purge.assert_not_awaited()
    prompt.edit.assert_awaited_once_with(content="Purge cancelled.", view=None)
    cog._audit.log.assert_not_awaited()


async def test_purge_time_window_newest_first(monkeypatch):
    import discord as _discord

    mod, cog, views = _purge_cog(monkeypatch, confirmed=True)
    purge = AsyncMock(return_value=[1, 2, 3])
    ctx, cmd_message, _ = _purge_ctx(purge)
    before = _discord.utils.utcnow()
    await mod.ModerationCog.purge_cmd.callback(cog, ctx, "15m")
    assert len(views) == 1  # window purges always confirm
    kw = purge.await_args.kwargs
    assert kw["limit"] == mod.PURGE_MAX and kw["oldest_first"] is False and kw["before"] is cmd_message
    delta = before - kw["after"]
    assert 899 <= delta.total_seconds() <= 901
    detail = cog._audit.log.await_args.kwargs["detail"]
    assert detail == {"count": 3, "filtered_user_id": None, "window_seconds": 900}


async def test_purge_user_filter_works_for_departed_user(monkeypatch):
    """The user filter is by ID, so a spammer who already left or was banned
    (get_member -> None) can still be purged."""
    mod, cog, _ = _purge_cog(monkeypatch, confirmed=True)

    async def purge(**kw):
        msgs = [_msg(a) for a in (9, 123456789012345678, 9, 123456789012345678)]
        return [m for m in msgs if kw["check"](m)]

    ctx, *_ = _purge_ctx(purge)
    await mod.ModerationCog.purge_cmd.callback(cog, ctx, "10", "123456789012345678")
    ctx.send.assert_awaited_with("Deleted 2 message(s).")
    assert cog._audit.log.await_args.kwargs["detail"]["filtered_user_id"] == 123456789012345678


async def test_purge_bad_amount_shows_usage(monkeypatch):
    mod, cog, _ = _purge_cog(monkeypatch, confirmed=True)
    purge = AsyncMock()
    ctx, *_ = _purge_ctx(purge)
    await mod.ModerationCog.purge_cmd.callback(cog, ctx, "lots")
    purge.assert_not_awaited()
    assert "Usage" in ctx.reply.await_args.args[0]


async def test_purge_command_excludes_command_message(monkeypatch):
    """!purge 5 purges 5 messages BEFORE the command message, then deletes the
    command message itself."""
    from cogs import moderation as mod

    cog = mod.ModerationCog.__new__(mod.ModerationCog)
    cog.bot = SimpleNamespace(permission_service=SimpleNamespace(has_permission=AsyncMock(return_value=True)))
    cog._audit = None
    cog._send_mod_log = AsyncMock()
    monkeypatch.setattr(mod.asyncio, "sleep", AsyncMock())
    seen = {}

    async def purge(**kw):
        seen.update(kw)
        msgs = [_msg(1) for _ in range(10)]
        return [m for m in msgs if kw["check"](m)][: kw["limit"]]

    cmd_message = SimpleNamespace(delete=AsyncMock())
    channel = SimpleNamespace(id=5, mention="#c", purge=purge)
    ctx = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID), author=SimpleNamespace(id=1), channel=channel,
        message=cmd_message, reply=AsyncMock(), send=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
    )
    await mod.ModerationCog.purge_cmd.callback(cog, ctx, 5)
    assert seen["limit"] == 5 and seen["before"] is cmd_message
    cmd_message.delete.assert_awaited_once()
    ctx.send.assert_awaited_with("Deleted 5 message(s).")


async def test_delwarn_passes_user_and_actor():
    from cogs import moderation as mod

    cog = mod.ModerationCog.__new__(mod.ModerationCog)
    cog.bot = SimpleNamespace(permission_service=SimpleNamespace(has_permission=AsyncMock(return_value=True)))
    cog._warnings = SimpleNamespace(remove_warning=AsyncMock(return_value=None))
    cog.moderation_service = None
    ctx = SimpleNamespace(guild=SimpleNamespace(id=GUILD_ID), author=SimpleNamespace(id=42), reply=AsyncMock())
    await mod.ModerationCog.delwarn_cmd.callback(cog, ctx, "<@111>", 7)
    cog._warnings.remove_warning.assert_awaited_once_with(GUILD_ID, 7, user_id=111, actor_id=42)
    assert "not found for <@111>" in ctx.reply.await_args.args[0]


async def test_warn_command_blocked_by_rank():
    from cogs import moderation as mod

    cog = mod.ModerationCog.__new__(mod.ModerationCog)
    cog.bot = SimpleNamespace(permission_service=SimpleNamespace(has_permission=AsyncMock(return_value=True)))
    cog._warnings = SimpleNamespace(add_warning=AsyncMock())
    target = member(2, 6)
    g = guild()
    g.get_member = lambda uid: target if uid == 2 else None
    ctx = SimpleNamespace(guild=g, author=member(1, 6), reply=AsyncMock())
    await mod.ModerationCog.warn_cmd.callback(cog, ctx, "2", reason="x")
    cog._warnings.add_warning.assert_not_awaited()
    assert "at or above yours" in ctx.reply.await_args.args[0]


async def test_timeout_command_rejects_out_of_range():
    from cogs import moderation as mod

    cog = mod.ModerationCog.__new__(mod.ModerationCog)
    cog.bot = SimpleNamespace(permission_service=SimpleNamespace(has_permission=AsyncMock(return_value=True)))
    cog.moderation_service = SimpleNamespace(timeout=AsyncMock())
    ctx = SimpleNamespace(guild=guild(), author=member(1, 6), reply=AsyncMock())
    await mod.ModerationCog.timeout_cmd.callback(cog, ctx, "2", MAX_TIMEOUT_MINUTES + 1)
    cog.moderation_service.timeout.assert_not_awaited()
    assert "28 days" in ctx.reply.await_args.args[0]


async def test_ban_command_passes_check_hierarchy():
    from cogs import moderation as mod

    cog = mod.ModerationCog.__new__(mod.ModerationCog)
    cog.bot = SimpleNamespace(permission_service=SimpleNamespace(has_permission=AsyncMock(return_value=True)))
    cog.moderation_service = SimpleNamespace(ban=AsyncMock(side_effect=ModerationBlocked("nope")))
    g = guild()
    g.get_member = lambda uid: None
    ctx = SimpleNamespace(guild=g, author=member(1, 6), reply=AsyncMock())
    await mod.ModerationCog.ban_cmd.callback(cog, ctx, "2", reason="x")
    assert cog.moderation_service.ban.await_args.kwargs["check_hierarchy"] is True
    ctx.reply.assert_awaited_with("nope")


def test_discord_forbidden_is_not_swallowed_as_blocked():
    # ModerationBlocked must not be a discord.HTTPException (callers map those
    # to 409/4xx separately).
    assert not issubclass(ModerationBlocked, discord.HTTPException)
