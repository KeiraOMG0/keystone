"""Anti-spam: services/spam_detector.py (pure rules, cooldown, memory sweep),
cogs/anti_spam.py (exemptions, delete/timeout/audit/log on a hit, cooldown
deletes only, bulk-delete fallback, !antispam toggle) and the antispam_*
settings (off-or-at-least floors, dashboard mirror). Fake discord objects,
no network."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from services import settings_service as ss
from services import spam_detector as sd
from services.spam_detector import SpamDetector, SpamRules, fingerprint


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def detector():
    clock = Clock()
    return SpamDetector(clock=clock), clock


RULES = SpamRules(flood_messages=5, flood_seconds=5, duplicate_messages=3, duplicate_seconds=30, mention_limit=4)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

def test_flood_triggers_at_threshold_and_lists_window():
    det, clock = detector()
    verdicts = []
    for i in range(5):
        verdicts.append(det.observe(1, 10, 100 + i, f"msg {i}", 0, RULES))
        clock.t += 0.5
    assert verdicts[:4] == [None] * 4
    v = verdicts[4]
    assert v.rule == "flood" and v.first
    assert v.messages == [(10, 100 + i) for i in range(5)]


def test_flood_needs_messages_inside_window():
    det, clock = detector()
    for i in range(10):
        assert det.observe(1, 10, i, f"m{i}", 0, RULES) is None
        clock.t += 1.5  # 4 messages per 5 s at most


def test_duplicates_across_channels_casefolded():
    det, clock = detector()
    assert det.observe(1, 10, 1, "Free  NITRO here", 0, RULES) is None
    clock.t += 5
    assert det.observe(1, 11, 2, "free nitro here", 0, RULES) is None
    clock.t += 5
    v = det.observe(1, 12, 3, " FREE nitro HERE ", 0, RULES)
    assert v.rule == "duplicate"
    assert v.messages == [(10, 1), (11, 2), (12, 3)]
    assert "across 3 channels" in v.detail


def test_commands_and_empty_messages_never_duplicates():
    assert fingerprint("!skip") is None
    assert fingerprint("   ") is None and fingerprint(None) is None
    det, clock = detector()
    for i in range(4):
        assert det.observe(1, 10, i, "!skip", 0, RULES) is None
        clock.t += 3
    for i in range(4):
        assert det.observe(2, 10, 50 + i, "", 0, RULES) is None
        clock.t += 3


def test_mentions_rule_flags_single_message():
    det, _ = detector()
    assert det.observe(1, 10, 1, "hi", 3, RULES) is None
    v = det.observe(1, 10, 2, "hi all", 4, RULES)
    assert v.rule == "mentions" and v.messages == [(10, 2)]


def test_zero_turns_rules_off():
    off = SpamRules(flood_messages=0, duplicate_messages=0, mention_limit=0)
    det, _ = detector()
    for i in range(30):
        assert det.observe(1, 10, i, "same", 20, off) is None


def test_cooldown_is_delete_only_and_not_extended():
    det, clock = detector()
    for i in range(5):
        det.observe(1, 10, i, f"m{i}", 0, RULES)
    clock.t += 1
    v = det.observe(1, 10, 99, "still here", 0, RULES)
    assert v.rule == "cooldown" and not v.first and v.messages == [(10, 99)]
    assert det.in_cooldown(1)
    clock.t += sd.COOLDOWN_SECONDS  # cooldown ran from the hit, not from msg 99
    assert not det.in_cooldown(1)
    assert det.observe(1, 10, 100, "normal", 0, RULES) is None


def test_history_cleared_after_hit_and_users_independent():
    det, clock = detector()
    for i in range(4):
        det.observe(1, 10, i, f"a{i}", 0, RULES)
        det.observe(2, 10, 50 + i, f"b{i}", 0, RULES)
    assert det.observe(1, 10, 4, "a4", 0, RULES).rule == "flood"
    assert det.observe(2, 10, 54, "b4", 0, RULES).rule == "flood"


def test_forget_and_sweep(monkeypatch):
    det, clock = detector()
    det.observe(1, 10, 1, "x", 0, RULES)
    det.forget(1)
    assert det.tracked_users() == 0
    monkeypatch.setattr(sd, "SWEEP_EVERY", 3)
    det, clock = detector()
    det.observe(1, 10, 1, "x", 0, RULES)
    det.observe(2, 10, 2, "y", 0, RULES)
    clock.t += 1000
    det.observe(3, 10, 3, "z", 0, RULES)  # third observation sweeps idle users
    assert det.tracked_users() == 1


def test_rules_from_effective_settings(monkeypatch):
    values = {
        "antispam_enabled": False, "antispam_flood_messages": 7, "antispam_flood_seconds": 4,
        "antispam_duplicate_messages": 0, "antispam_duplicate_seconds": 60,
        "antispam_mention_limit": 9, "antispam_timeout_minutes": 0,
    }
    r = SpamRules.from_settings(values)
    assert (r.enabled, r.flood_messages, r.flood_seconds, r.duplicate_messages, r.mention_limit, r.timeout_minutes) == (
        False, 7, 4, 0, 9, 0)


# ---------------------------------------------------------------------------
# Settings floors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,floor", sorted(ss.OFF_OR_MIN.items()))
def test_off_or_min_floor(key, floor):
    field = ss.SETTINGS_SCHEMA[key]
    assert ss.parse_value(field, 0) == 0
    assert ss.parse_value(field, floor) == floor
    with pytest.raises(ValueError, match="Use 0 to turn this rule off"):
        ss.parse_value(field, floor - 1)


def test_antispam_fields_are_live_and_mirrored():
    from server.routes.settings import _FIELD_RULES, _check

    keys = [k for k in ss.SETTINGS_SCHEMA if k.startswith("antispam_")]
    assert len(keys) == 7
    assert all(ss.SETTINGS_SCHEMA[k].live and ss.SETTINGS_SCHEMA[k].group == "antispam" for k in keys)
    for key, floor in ss.OFF_OR_MIN.items():
        rule = _FIELD_RULES[key]
        assert rule["off_or_min"] == floor
        assert _check(rule, floor - 1) and _check(rule, 0) is None and _check(rule, floor) is None
    assert {k for k, r in _FIELD_RULES.items() if "off_or_min" in r} == set(ss.OFF_OR_MIN)


def test_timeout_bound_matches_moderation_max():
    from services.moderation_service import MAX_TIMEOUT_MINUTES

    assert ss.SETTINGS_SCHEMA["antispam_timeout_minutes"].max == MAX_TIMEOUT_MINUTES


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

BOT_ID = 900000000000000001
GUILD_OWNER = 800000000000000008


class FakeChannel:
    def __init__(self, cid: int, fail_bulk: Exception | None = None) -> None:
        self.id = cid
        self.mention = f"<#{cid}>"
        self.bulk: list[list[int]] = []
        self.single: list[int] = []
        self.fail_bulk = fail_bulk

    async def delete_messages(self, objs, reason=None):
        if self.fail_bulk:
            raise self.fail_bulk
        self.bulk.append([o.id for o in objs])

    def get_partial_message(self, mid):
        async def delete():
            self.single.append(mid)
        return SimpleNamespace(delete=delete)


def http_error(status: int, cls=discord.HTTPException):
    return cls(SimpleNamespace(status=status, reason="x"), "x")


def make_guild(channels):
    by_id = {c.id: c for c in channels}
    return SimpleNamespace(
        id=100000000000000001, owner_id=GUILD_OWNER, me=SimpleNamespace(id=BOT_ID),
        get_channel_or_thread=lambda cid: by_id.get(cid),
    )


def make_member(uid: int, guild, *, admin=False, timed_out=False):
    return _MemberStub(uid, guild, admin, timed_out)


class _MemberStub(SimpleNamespace):
    """Stands in for discord.Member (make_cog patches the cog's isinstance target)."""

    def __init__(self, uid, guild, admin, timed_out):
        super().__init__(id=uid, guild=guild, bot=False, guild_permissions=SimpleNamespace(administrator=admin))
        self._timed_out = timed_out

    def is_timed_out(self):
        return self._timed_out

    def __str__(self):
        return f"user{self.id}"


def make_cog(monkeypatch, *, staff=False, granted=False, rules=None):
    from cogs import anti_spam as mod

    monkeypatch.setattr(mod.discord, "Member", _MemberStub)
    # Like PermissionService: staff pass every has_permission check.
    perm = SimpleNamespace(is_staff=lambda m: staff, has_permission=AsyncMock(return_value=granted or staff))
    bot = SimpleNamespace(permission_service=perm)
    moderation = SimpleNamespace(timeout=AsyncMock(return_value=1))
    audit = SimpleNamespace(log=AsyncMock())
    settings = SimpleNamespace(get_effective=AsyncMock(), update=AsyncMock())
    cog = mod.AntiSpam(bot, settings, moderation, audit)
    cog.rules = AsyncMock(return_value=rules or RULES)
    posted = AsyncMock()
    monkeypatch.setattr(mod, "route_and_post", posted)
    return cog, moderation, audit, posted, perm


def message(member, channel, mid, content="spam", mentions=()):
    return SimpleNamespace(
        guild=member.guild, author=member, webhook_id=None, type=discord.MessageType.default,
        channel=channel, id=mid, content=content, raw_mentions=list(mentions), raw_role_mentions=[],
    )


async def flood(cog, member, channel, n=5, start=100):
    for i in range(n):
        await cog.on_message(message(member, channel, start + i, f"spam {i}"))


async def test_hit_deletes_times_out_audits_and_logs(monkeypatch):
    cog, moderation, audit, posted, _ = make_cog(monkeypatch)
    ch = FakeChannel(10)
    g = make_guild([ch])
    m = make_member(5, g)
    await flood(cog, m, ch)

    assert ch.bulk == [[100, 101, 102, 103, 104]]
    moderation.timeout.assert_awaited_once()
    args = moderation.timeout.await_args.args
    assert args[0] is m and args[2] is g.me and args[3] == RULES.timeout_minutes
    assert args[4].startswith("Anti-spam: ")
    assert "check_hierarchy" not in moderation.timeout.await_args.kwargs  # bot-initiated path
    audit.log.assert_awaited_once()
    a = audit.log.await_args
    assert a.args[1] == BOT_ID and a.args[2] == "spam_detected" and a.kwargs["target_id"] == 5
    assert a.kwargs["detail"]["rule"] == "flood" and a.kwargs["detail"]["messages_deleted"] == 5
    assert a.kwargs["detail"]["timeout_result"] == "ok"
    posted.assert_awaited_once()
    assert posted.await_args.args[2] == "spam_detected"


async def test_cooldown_message_deleted_without_second_action(monkeypatch):
    cog, moderation, audit, posted, _ = make_cog(monkeypatch)
    ch = FakeChannel(10)
    m = make_member(5, make_guild([ch]))
    await flood(cog, m, ch)
    await cog.on_message(message(m, ch, 200, "more"))
    assert ch.bulk[-1] == [200]
    assert moderation.timeout.await_count == 1 and audit.log.await_count == 1 and posted.await_count == 1


async def test_staff_admin_and_owner_never_observed(monkeypatch):
    for kwargs, uid in (({"staff": True}, 5), ({}, GUILD_OWNER)):
        cog, moderation, *_ = make_cog(monkeypatch, **kwargs)
        ch = FakeChannel(10)
        await flood(cog, make_member(uid, make_guild([ch])), ch, n=8)
        assert ch.bulk == [] and cog.detector.tracked_users() == 0
    cog, *_ = make_cog(monkeypatch)
    ch = FakeChannel(10)
    await flood(cog, make_member(5, make_guild([ch]), admin=True), ch, n=8)
    assert ch.bulk == []


async def test_granted_permission_exempt_and_forgotten(monkeypatch):
    cog, moderation, audit, _, perm = make_cog(monkeypatch, granted=True)
    ch = FakeChannel(10)
    m = make_member(5, make_guild([ch]))
    await flood(cog, m, ch, n=12)
    assert ch.bulk == [] and moderation.timeout.await_count == 0 and audit.log.await_count == 0
    # looked up once, then served from the 60 s cache
    assert perm.has_permission.await_count == 1


async def test_disabled_does_nothing(monkeypatch):
    cog, moderation, *_ = make_cog(monkeypatch, rules=SpamRules(enabled=False, flood_messages=3))
    ch = FakeChannel(10)
    await flood(cog, make_member(5, make_guild([ch])), ch, n=10)
    assert ch.bulk == [] and cog.detector.tracked_users() == 0


async def test_timeout_off_and_forbidden(monkeypatch):
    cog, moderation, audit, *_ = make_cog(monkeypatch, rules=SpamRules(flood_messages=3, timeout_minutes=0))
    ch = FakeChannel(10)
    await flood(cog, make_member(5, make_guild([ch])), ch, n=3)
    moderation.timeout.assert_not_awaited()
    assert audit.log.await_args.kwargs["detail"]["timeout_result"] == "off"

    cog, moderation, audit, *_ = make_cog(monkeypatch, rules=SpamRules(flood_messages=3))
    moderation.timeout.side_effect = http_error(403, discord.Forbidden)
    ch = FakeChannel(10)
    await flood(cog, make_member(5, make_guild([ch])), ch, n=3)
    d = audit.log.await_args.kwargs["detail"]
    assert d["timeout_result"] == "forbidden" and d["timeout_minutes"] == 0
    assert ch.bulk == [[100, 101, 102]]  # spam still removed


async def test_already_timed_out_not_retimed(monkeypatch):
    cog, moderation, audit, *_ = make_cog(monkeypatch, rules=SpamRules(flood_messages=3))
    ch = FakeChannel(10)
    await flood(cog, make_member(5, make_guild([ch]), timed_out=True), ch, n=3)
    moderation.timeout.assert_not_awaited()
    assert audit.log.await_args.kwargs["detail"]["timeout_result"] == "already"


async def test_bulk_failure_falls_back_to_single_deletes(monkeypatch):
    cog, *_ = make_cog(monkeypatch)
    ch = FakeChannel(10, fail_bulk=http_error(400))
    g = make_guild([ch])
    assert await cog.delete_messages(g, [(10, 1), (10, 2), (99, 3)]) == 2
    assert ch.single == [1, 2]

    forbidden = FakeChannel(10, fail_bulk=http_error(403, discord.Forbidden))
    assert await cog.delete_messages(make_guild([forbidden]), [(10, 1)]) == 0
    assert forbidden.single == []


async def test_duplicate_spam_deleted_in_every_channel(monkeypatch):
    cog, *_ = make_cog(monkeypatch)
    chans = [FakeChannel(10), FakeChannel(11), FakeChannel(12)]
    m = make_member(5, make_guild(chans))
    for i, ch in enumerate(chans):
        await cog.on_message(message(m, ch, 300 + i, "join my server discord.gg/x"))
    assert [c.bulk for c in chans] == [[[300]], [[301]], [[302]]]


async def test_self_mention_not_counted(monkeypatch):
    from cogs.anti_spam import mention_count

    m = SimpleNamespace(author=SimpleNamespace(id=5), raw_mentions=[5, 6, 6, 7], raw_role_mentions=[8])
    assert mention_count(m) == 3


async def test_antispam_toggle_staff_only(monkeypatch):
    cog, *_ = make_cog(monkeypatch, granted=True)  # may view status, not staff
    ctx = SimpleNamespace(guild=SimpleNamespace(id=1), author=SimpleNamespace(id=42), reply=AsyncMock())
    await type(cog).antispam_cmd.callback(cog, ctx, "off")
    cog._settings.update.assert_not_awaited()
    assert "Only staff" in ctx.reply.await_args.args[0]

    cog, *_ = make_cog(monkeypatch, staff=True)
    await type(cog).antispam_cmd.callback(cog, ctx, "off")
    cog._settings.update.assert_awaited_once_with(1, {"antispam_enabled": False}, 42, guild=ctx.guild)

    await type(cog).antispam_cmd.callback(cog, ctx, "maybe")
    assert "Usage" in ctx.reply.await_args.args[0]


def app_message(app_id, invoker, channel, mid, *, user_installed=True, embeds=()):
    """A reply an app posted because `invoker` ran its command (webhook_id is set, as on Discord)."""
    meta = SimpleNamespace(user=invoker, is_user_integration=lambda: user_installed)
    return SimpleNamespace(
        guild=invoker.guild, author=SimpleNamespace(id=app_id, bot=True, name="SpamApp"),
        webhook_id=app_id, type=discord.MessageType.chat_input_command, channel=channel, id=mid,
        content="", embeds=list(embeds), raw_mentions=[], raw_role_mentions=[],
        interaction_metadata=meta, application_id=app_id,
    )


async def test_user_installed_app_flood_times_out_the_invoker(monkeypatch):
    cog, moderation, audit, posted, _ = make_cog(monkeypatch)
    ch = FakeChannel(10)
    g = make_guild([ch])
    m = make_member(5, g)
    g.get_member = lambda uid: m if uid == 5 else None
    e = SimpleNamespace(title="FREE NITRO", description="discord.gg/x", fields=[])
    for i in range(5):
        await cog.on_message(app_message(777, m, ch, 400 + i, embeds=[e]))
    # Identical embeds: the duplicate rule (3 copies) fires first; the rest go in the cooldown.
    assert ch.bulk == [[400, 401, 402], [403], [404]]
    assert moderation.timeout.await_count == 1 and moderation.timeout.await_args.args[0] is m
    d = audit.log.await_args.kwargs["detail"]
    assert d["via_app_id"] == 777 and d["via_app_name"] == "SpamApp" and d["rule"] == "duplicate"
    fields = {f.name: f.value for f in posted.await_args.args[3].fields}
    assert "user-installed app" in fields["Posted via app"] and "FREE NITRO" in fields["Sample"]


async def test_server_bot_command_replies_are_not_checked(monkeypatch):
    cog, moderation, *_ = make_cog(monkeypatch)
    ch = FakeChannel(10)
    g = make_guild([ch])
    m = make_member(5, g)
    g.get_member = lambda uid: m
    for i in range(10):
        await cog.on_message(app_message(777, m, ch, 400 + i, user_installed=False))
    assert ch.bulk == [] and cog.detector.tracked_users() == 0


async def test_plain_webhook_and_bot_messages_ignored(monkeypatch):
    cog, *_ = make_cog(monkeypatch)
    ch = FakeChannel(10)
    g = make_guild([ch])
    for i in range(10):
        msg = message(make_member(5, g), ch, 500 + i)
        msg.webhook_id = 123
        await cog.on_message(msg)
    assert ch.bulk == [] and cog.detector.tracked_users() == 0
