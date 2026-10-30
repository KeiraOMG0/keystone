"""kicks.log counter in cogs/kick.py: serialized, atomic, format preserved.
Only ever touches a temp file (never the real kicks.log)."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs import kick


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    path = tmp_path / "kicks.log"
    # Belt and braces: any call that forgets `path` still lands in tmp.
    monkeypatch.setattr(kick, "LOG_FILE", str(path))
    return path


def test_missing_file_starts_at_zero(log_path):
    assert kick.get_total_kicks(str(log_path)) == 0
    kick.save_total_kicks(1, str(log_path))
    assert log_path.read_bytes() == b"TOTAL_KICKS:1\n"


def test_preserves_every_other_line(log_path):
    original = "TOTAL_KICKS:41\n[a] KICK #40 | x\n\n[b] KICK #41 | y ✨\nno newline at end"
    log_path.write_bytes(original.encode("utf-8"))
    kick.write_kicks_log(42, "[c] KICK #42 | z\n", str(log_path))
    text = log_path.read_bytes().decode("utf-8")
    assert text == "TOTAL_KICKS:42\n[a] KICK #40 | x\n\n[b] KICK #41 | y ✨\nno newline at end\n[c] KICK #42 | z\n"
    assert kick.get_total_kicks(str(log_path)) == 42


def test_crlf_counter_line_kept(log_path):
    log_path.write_bytes(b"TOTAL_KICKS:3\r\nold line\r\n")
    kick.save_total_kicks(4, str(log_path))
    assert log_path.read_bytes() == b"TOTAL_KICKS:4\r\nold line\r\n"


def test_no_temp_files_left(log_path):
    kick.save_total_kicks(5, str(log_path))
    assert os.listdir(log_path.parent) == ["kicks.log"]


def test_failed_write_leaves_original(log_path, monkeypatch):
    log_path.write_bytes(b"TOTAL_KICKS:9\nhistory\n")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(kick.os, "replace", boom)
    with pytest.raises(OSError):
        kick.save_total_kicks(10, str(log_path))
    assert log_path.read_bytes() == b"TOTAL_KICKS:9\nhistory\n"
    assert os.listdir(log_path.parent) == ["kicks.log"]


def test_corrupt_counter_raises_instead_of_resetting(log_path):
    log_path.write_bytes(b"TOTAL_KICKS:abc\nhistory\n")
    with pytest.raises(ValueError):
        kick.get_total_kicks(str(log_path))


async def test_concurrent_reservations_do_not_lose_counts(log_path):
    log_path.write_bytes(b"TOTAL_KICKS:100\nhistory\n")
    totals = await asyncio.gather(*(kick.next_kick_number(str(log_path)) for _ in range(25)))
    assert sorted(totals) == list(range(101, 126))
    assert kick.get_total_kicks(str(log_path)) == 125
    assert log_path.read_bytes().endswith(b"history\n")


async def test_release_only_when_untouched(log_path):
    log_path.write_bytes(b"TOTAL_KICKS:7\n")
    total = await kick.next_kick_number(str(log_path))
    await kick.release_kick_number(total, str(log_path))
    assert kick.get_total_kicks(str(log_path)) == 7
    a = await kick.next_kick_number(str(log_path))
    await kick.next_kick_number(str(log_path))
    await kick.release_kick_number(a, str(log_path))  # someone else bumped it: keep
    assert kick.get_total_kicks(str(log_path)) == 9


# ---------------------------------------------------------------------------
# do_kick: manual vs auto reason, rank check, counter release on failure
# ---------------------------------------------------------------------------

def _cog(service):
    cog = kick.KickCog.__new__(kick.KickCog)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=1))
    cog.moderation_service = service
    return cog


def _member(uid=5, pos=1):
    from datetime import datetime, timezone

    return SimpleNamespace(
        id=uid, roles=[], top_role=SimpleNamespace(position=pos), joined_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        guild=SimpleNamespace(name="G"), kick=AsyncMock(), __str__=lambda self: "m",
    )


async def test_manual_kick_has_own_reason(log_path):
    from services.moderation_service import ModerationService

    svc = ModerationService(SimpleNamespace(add_action=AsyncMock(return_value=1)), SimpleNamespace(log=AsyncMock()))
    svc.kick = AsyncMock(return_value=1)
    actor = _member(9, 5)
    guild = SimpleNamespace(id=10, name="G", owner_id=None, me=_member(1, 50), get_member=lambda uid: actor)
    target = _member(5, 1)
    await _cog(svc).do_kick(target, guild, None, actor_id=9, reason="spamming")
    args, kwargs = svc.kick.await_args
    assert args[3] == "spamming" and kwargs["audit_detail"]["source"] == "manual"
    assert kick.get_total_kicks(str(log_path)) == 1
    await _cog(svc).do_kick(target, guild, None)
    args, kwargs = svc.kick.await_args
    assert args[3] == kick.AUTO_KICK_REASON and kwargs["audit_detail"]["source"] == "auto_unverified"


async def test_manual_kick_blocked_by_rank_does_not_count(log_path):
    from services.moderation_service import ModerationBlocked, ModerationService

    svc = ModerationService(SimpleNamespace(add_action=AsyncMock()), None)
    svc.kick = AsyncMock()
    actor = _member(9, 2)
    guild = SimpleNamespace(id=10, name="G", owner_id=None, me=_member(1, 50), get_member=lambda uid: actor)
    with pytest.raises(ModerationBlocked):
        await _cog(svc).do_kick(_member(5, 3), guild, None, actor_id=9)
    svc.kick.assert_not_awaited()
    assert not log_path.exists() or kick.get_total_kicks(str(log_path)) == 0


async def test_failed_kick_releases_number(log_path):
    log_path.write_bytes(b"TOTAL_KICKS:3\n")
    svc = SimpleNamespace(kick=AsyncMock(side_effect=RuntimeError("discord down")), ensure_allowed=lambda *a: None)
    guild = SimpleNamespace(id=10, name="G", get_member=lambda uid: None)
    with pytest.raises(RuntimeError):
        await _cog(svc).do_kick(_member(), guild, None)
    assert kick.get_total_kicks(str(log_path)) == 3


async def test_auto_kick_iteration_survives_unexpected_errors(monkeypatch):
    """A non-Discord exception for one member is logged; the next is still kicked."""
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(days=30)
    m1 = SimpleNamespace(id=1, joined_at=old, roles=[])
    m2 = SimpleNamespace(id=2, joined_at=old, roles=[])
    unverified = SimpleNamespace(members=[m1, m2])
    guild = SimpleNamespace(name="G", get_role=lambda rid: unverified if rid == kick.UNVERIFIED_ROLE_ID else None,
                            get_channel=lambda cid: None)
    cog = kick.KickCog.__new__(kick.KickCog)
    cog.bot = SimpleNamespace(guilds=[guild])
    kicked = []

    async def do_kick(member, g, ch, **kw):
        if member.id == 1:
            raise RuntimeError("boom")
        kicked.append(member.id)

    cog.do_kick = do_kick
    await cog._run_unverified_check()
    assert kicked == [2]
