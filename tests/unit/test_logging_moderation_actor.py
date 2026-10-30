"""Moderator attribution for native bans/timeouts (cogs/logging_moderation.py):
only an audit-log entry for THIS member, created moments ago, counts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs import logging_moderation as lm

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)


def entry(target_id, seconds_ago, user_id=9, **changes):
    return SimpleNamespace(
        target=SimpleNamespace(id=target_id), created_at=NOW - timedelta(seconds=seconds_ago),
        user=SimpleNamespace(id=user_id) if user_id else None,
        after=SimpleNamespace(**changes), before=SimpleNamespace(**changes),
    )


def test_pick_matches_target_not_newest():
    entries = [entry(222, 1, user_id=1), entry(111, 3, user_id=2)]
    assert lm.pick_actor_entry(entries, 111, NOW).user.id == 2


def test_pick_ignores_stale_entries():
    assert lm.pick_actor_entry([entry(111, 600)], 111, NOW) is None


def test_pick_ignores_entries_without_user():
    assert lm.pick_actor_entry([entry(111, 1, user_id=None)], 111, NOW) is None


def test_timeout_predicate_skips_nick_changes():
    nick = entry(111, 1, user_id=1, nick="x")
    timeout = entry(111, 2, user_id=2, timed_out_until=NOW)
    assert lm.pick_actor_entry([nick, timeout], 111, NOW, predicate=lm.is_timeout_change).user.id == 2


class FakeGuild:
    def __init__(self, entries=None, exc=None):
        self.id = 1
        self._entries = entries or []
        self._exc = exc
        self.calls = 0

    def audit_logs(self, **kw):
        self.calls += 1
        exc, entries = self._exc, self._entries

        async def gen():
            if exc:
                raise exc
            for e in entries:
                yield e

        return gen()


def _cog():
    audit = SimpleNamespace(log=AsyncMock())
    return lm.LoggingModerationCog(SimpleNamespace(user=SimpleNamespace(id=999)), audit), audit


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(lm, "AUDIT_RETRY_DELAY", 0)


async def test_resolve_actor_matches_fresh_entry():
    cog, _ = _cog()
    fresh = SimpleNamespace(target=SimpleNamespace(id=111), created_at=datetime.now(timezone.utc),
                            user=SimpleNamespace(id=42))
    other = SimpleNamespace(target=SimpleNamespace(id=5), created_at=datetime.now(timezone.utc),
                            user=SimpleNamespace(id=43))
    g = FakeGuild([other, fresh])
    assert await cog._resolve_actor_id(g, discord.AuditLogAction.ban, "member_banned_native", 111) == 42


async def test_resolve_actor_unknown_after_retry():
    cog, _ = _cog()
    g = FakeGuild([entry(5, 1)])
    assert await cog._resolve_actor_id(g, discord.AuditLogAction.ban, "x", 111) is None
    assert g.calls == 2


async def test_resolve_actor_forbidden_is_unknown():
    cog, _ = _cog()
    g = FakeGuild(exc=discord.Forbidden(MagicMock(status=403, reason="x"), "no"))
    assert await cog._resolve_actor_id(g, discord.AuditLogAction.ban, "x", 111) is None


async def test_unknown_actor_is_recorded_as_unknown(monkeypatch):
    cog, audit = _cog()
    posted = []
    monkeypatch.setattr(lm, "route_and_post", AsyncMock(side_effect=lambda bot, g, action, embed: posted.append(embed)))
    await cog.on_member_ban(FakeGuild([]), SimpleNamespace(id=111))
    args, kwargs = audit.log.await_args
    assert args[1] == lm.UNKNOWN_ACTOR_ID and kwargs["detail"] == {"actor": "unknown"}
    fields = {f.name: f.value for f in posted[0].fields}
    assert fields.get("Moderator") == "Unknown"
