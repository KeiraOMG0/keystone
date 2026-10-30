import asyncio
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from database.repositories.honeypot_repo import hash_token
from services.honeypot_service import is_exempt
from tests.unit.test_honeypot_support import (
    CHANNEL_ID, GUILD_ID, LOG_CHANNEL_ID, configured, db, make_guild, make_member, make_message,
    make_service, make_text_channel, repo,
)

pytestmark = pytest.mark.asyncio


def forbidden():
    return discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "nope")


class Env:
    """A honeypot channel with a stats message, a log channel, a guild, and a service."""


async def build(repo, db, *, action="ban", **cfg_over):
    trap = make_text_channel(CHANNEL_ID, "do-not-type")
    stats_msg = MagicMock()
    stats_msg.edit = AsyncMock()
    trap.fetch_message = AsyncMock(return_value=stats_msg)
    log_ch = make_text_channel(LOG_CHANNEL_ID, "mod-log")
    guild = make_guild([trap, log_ch])
    await configured(repo, action=action, log_channel_id=LOG_CHANNEL_ID, stats_message_id=777, **cfg_over)
    svc, bot = make_service(guild, repo, db)
    env = Env()
    trap.guild = guild
    env.trap, env.log, env.guild, env.svc, env.bot, env.stats_msg = trap, log_ch, guild, svc, bot, stats_msg
    return env


async def mod_actions(db):
    rows = await db.fetchall("SELECT action_type, reason, moderator_id FROM mod_actions ORDER BY id")
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Exemptions
# ---------------------------------------------------------------------------
async def test_is_exempt_rules():
    perm = MagicMock()
    perm.is_staff = MagicMock(return_value=False)
    assert not is_exempt(make_member(), perm)
    assert is_exempt(make_member(bot=True), perm)
    assert is_exempt(make_member(admin=True), perm)
    assert is_exempt(make_member(manage_messages=True), perm)
    assert is_exempt(make_member(ban_members=True), perm)
    perm.is_staff = MagicMock(return_value=True)
    assert is_exempt(make_member(), perm)


async def test_is_exempt_fails_safe_when_staff_check_raises():
    perm = MagicMock()
    perm.is_staff = MagicMock(side_effect=RuntimeError("boom"))
    assert is_exempt(make_member(), perm)


@pytest.mark.parametrize("kind", ["admin", "manage_messages", "ban_members", "dm", "wrong_channel"])
async def test_ignored_messages_are_not_punished(repo, db, kind):
    env = await build(repo, db)
    kw = {"admin": dict(admin=True), "manage_messages": dict(manage_messages=True),
          "ban_members": dict(ban_members=True), "bot": dict(bot=True)}.get(kind, {})
    member = make_member(**kw)
    channel = env.trap if kind != "wrong_channel" else make_text_channel(5, "general")
    msg = make_message(member, channel, guild=None if kind == "dm" else env.guild)
    if kind == "webhook":
        msg.webhook_id = 123
    await env.svc.handle_message(msg, env.bot.permission_service)
    env.guild.ban.assert_not_awaited()
    msg.delete.assert_not_awaited()
    assert await repo.count_events(GUILD_ID) == 0


async def test_disabled_honeypot_does_nothing(repo, db):
    env = await build(repo, db, enabled=False)
    msg = make_message(make_member(), env.trap, guild=env.guild)
    await env.svc.handle_message(msg)
    env.guild.ban.assert_not_awaited()


async def test_unconfigured_guild_does_nothing(repo, db):
    guild = make_guild([make_text_channel(CHANNEL_ID)])
    svc, _ = make_service(guild, repo, db)
    await svc.handle_message(make_message(make_member(), guild.get_channel(CHANNEL_ID), guild=guild))
    guild.ban.assert_not_awaited()


# ---------------------------------------------------------------------------
# Action dispatch
# ---------------------------------------------------------------------------
async def test_ban_flow(repo, db):
    env = await build(repo, db, delete_message_days=3)
    member = make_member(42)
    msg = make_message(member, env.trap, "free nitro https://evil.example", guild=env.guild)
    await env.svc.handle_message(msg)

    msg.delete.assert_awaited_once()
    member.send.assert_awaited_once()
    dm = member.send.await_args.kwargs["embed"].description
    assert "/appeal/" in dm and "2FA" in dm

    env.guild.ban.assert_awaited_once()
    assert env.guild.ban.await_args.kwargs["delete_message_days"] == 3
    env.guild.unban.assert_not_awaited()

    (event,) = await repo.list_events(GUILD_ID)
    assert event["action"] == "ban" and event["action_result"] == "ok" and event["errors"] == []
    assert event["dm_sent"] is True and event["flag_link"] is True and event["content"].startswith("free nitro")
    appeal = await repo.get_latest_appeal_for_user(GUILD_ID, 42)
    assert appeal["status"] == "invited" and appeal["catch_id"] == event["id"]
    # Only the SHA-256 of the link token is stored (migration 030); the DM carries the raw token.
    raw = re.search(r"/appeal/([A-Za-z0-9_\-]+)", dm).group(1)
    assert appeal["token"].startswith("sha256:") and raw not in appeal["token"]
    assert appeal["token"] == hash_token(raw)
    assert (await repo.get_appeal_by_token(GUILD_ID, raw))["id"] == appeal["id"]

    log_embed = env.log.send.await_args.kwargs["embed"]
    assert log_embed.title == "Honeypot catch"
    env.stats_msg.edit.assert_awaited()  # live embed refreshed
    actions = await mod_actions(db)
    assert [a["action_type"] for a in actions] == ["ban"]
    assert actions[0]["reason"] == "Honeypot: posted in #do-not-type"
    assert actions[0]["moderator_id"] == 999


async def test_ban_without_appeals_has_no_link_or_row(repo, db):
    env = await build(repo, db, appeal_enabled=False)
    member = make_member(43)
    await env.svc.handle_message(make_message(member, env.trap, guild=env.guild))
    assert "/appeal/" not in member.send.await_args.kwargs["embed"].description
    assert await repo.get_latest_appeal_for_user(GUILD_ID, 43) is None


async def test_dm_before_off_skips_dm(repo, db):
    env = await build(repo, db, dm_before=False)
    member = make_member(44)
    await env.svc.handle_message(make_message(member, env.trap, guild=env.guild))
    member.send.assert_not_awaited()
    env.guild.ban.assert_awaited_once()
    assert (await repo.list_events(GUILD_ID))[0]["dm_sent"] is False


async def test_softban_bans_then_unbans_and_records_both(repo, db):
    env = await build(repo, db, action="softban", delete_message_days=0)
    order = []
    env.guild.ban.side_effect = lambda *a, **k: order.append("ban")
    env.guild.unban.side_effect = lambda *a, **k: order.append("unban")
    member = make_member(45)
    await env.svc.handle_message(make_message(member, env.trap, guild=env.guild))

    assert order == ["ban", "unban"]
    assert env.guild.ban.await_args.kwargs["delete_message_days"] == 1  # min 1 for a softban
    assert "softban" in env.guild.unban.await_args.kwargs["reason"].lower()
    actions = await mod_actions(db)
    assert [a["action_type"] for a in actions] == ["ban", "unban"]
    assert "softban" in actions[1]["reason"].lower()
    assert (await repo.list_events(GUILD_ID))[0]["action_result"] == "ok"
    assert await repo.get_latest_appeal_for_user(GUILD_ID, 45) is None  # no appeals for softban


async def test_softban_unban_failure_is_partial(repo, db):
    env = await build(repo, db, action="softban")
    env.guild.unban.side_effect = forbidden()
    await env.svc.handle_message(make_message(make_member(46), env.trap, guild=env.guild))
    (event,) = await repo.list_events(GUILD_ID)
    assert event["action_result"] == "partial"
    assert any(e.startswith("softban_unban_failed") for e in event["errors"])


async def test_kick_flow(repo, db):
    env = await build(repo, db, action="kick")
    member = make_member(47)
    await env.svc.handle_message(make_message(member, env.trap, guild=env.guild))
    member.kick.assert_awaited_once()
    env.guild.ban.assert_not_awaited()
    assert (await repo.list_events(GUILD_ID))[0]["action"] == "kick"
    assert [a["action_type"] for a in await mod_actions(db)] == ["kick"]
    assert await repo.get_latest_appeal_for_user(GUILD_ID, 47) is None


async def test_dm_failure_does_not_block_action(repo, db):
    env = await build(repo, db)
    member = make_member(48)
    member.send.side_effect = forbidden()
    await env.svc.handle_message(make_message(member, env.trap, guild=env.guild))
    env.guild.ban.assert_awaited_once()
    (event,) = await repo.list_events(GUILD_ID)
    assert event["dm_sent"] is False and event["action_result"] == "ok"


async def test_unexpected_dm_error_recorded_but_action_still_runs(repo, db):
    env = await build(repo, db)
    member = make_member(49)
    member.send.side_effect = discord.HTTPException(MagicMock(status=500, reason="x"), "server error")
    await env.svc.handle_message(make_message(member, env.trap, guild=env.guild))
    env.guild.ban.assert_awaited_once()
    (event,) = await repo.list_events(GUILD_ID)
    assert event["action_result"] == "partial" and any(e.startswith("dm_failed") for e in event["errors"])


async def test_ban_forbidden_marks_failed_but_still_logs(repo, db):
    env = await build(repo, db)
    env.guild.ban.side_effect = forbidden()
    msg = make_message(make_member(50), env.trap, guild=env.guild)
    await env.svc.handle_message(msg)
    (event,) = await repo.list_events(GUILD_ID)
    assert event["action_result"] == "failed" and any(e.startswith("ban_forbidden") for e in event["errors"])
    env.log.send.assert_awaited()
    assert "ban_forbidden" in env.log.send.await_args.kwargs["embed"].fields[-1].value
    assert await mod_actions(db) == []


async def test_every_step_failing_never_raises_and_others_still_run(repo, db):
    env = await build(repo, db)
    env.log.send.side_effect = forbidden()
    env.trap.fetch_message.side_effect = RuntimeError("boom")
    msg = make_message(make_member(51), env.trap, guild=env.guild)
    msg.delete.side_effect = forbidden()
    await env.svc.handle_message(msg)  # must not raise
    env.guild.ban.assert_awaited_once()
    (event,) = await repo.list_events(GUILD_ID)
    assert event["action_result"] == "partial"
    assert any(e.startswith("delete_forbidden") for e in event["errors"])
    assert any(e.startswith("log_post_failed") for e in event["errors"])


async def test_handle_message_swallows_internal_bugs(repo, db):
    env = await build(repo, db)
    env.svc._repo = None  # any use of the repo now raises AttributeError
    await env.svc.handle_message(make_message(make_member(52), env.trap, guild=env.guild))


async def test_double_fire_guard_acts_once(repo, db):
    env = await build(repo, db)
    member = make_member(53)
    msgs = [make_message(member, env.trap, f"spam {i}", guild=env.guild) for i in range(5)]
    await asyncio.gather(*(env.svc.handle_message(m) for m in msgs))
    env.guild.ban.assert_awaited_once()
    assert await repo.count_events(GUILD_ID) == 1
    for m in msgs:
        m.delete.assert_awaited()  # every message in the burst is cleaned up
    # ...and a late straggler after the action finished is also only cleaned up
    late = make_message(member, env.trap, "late", guild=env.guild)
    await env.svc.handle_message(late)
    env.guild.ban.assert_awaited_once()
    late.delete.assert_awaited_once()


async def test_channel_deleted_clears_message_ids_without_crashing(repo, db):
    env = await build(repo, db)
    await env.svc.handle_channel_deleted(env.trap)
    row = await repo.get_config(GUILD_ID)
    assert row["stats_message_id"] is None and row["warning_message_id"] is None
    env.log.send.assert_awaited()
    # a channel from nowhere / unrelated channel is harmless
    other = make_text_channel(5)
    other.guild = env.guild
    await env.svc.handle_channel_deleted(other)
    await env.svc.handle_channel_deleted(MagicMock(guild=None))


async def test_config_reports_channel_missing(repo, db):
    env = await build(repo, db)
    assert (await env.svc.get_config(GUILD_ID))["channel_exists"] is True
    env.guild.get_channel = lambda cid: None
    cfg = await env.svc.get_config(GUILD_ID)
    assert cfg["configured"] is True and cfg["channel_exists"] is False


async def test_unconfigured_config_defaults(repo, db):
    guild = make_guild()
    svc, _ = make_service(guild, repo, db)
    cfg = await svc.get_config(GUILD_ID)
    assert cfg["configured"] is False and cfg["enabled"] is False and cfg["action"] == "ban"
    assert cfg["channel_exists"] is False and cfg["appeal_base_url"].startswith("http")


async def test_update_config_validation(repo, db):
    env = await build(repo, db)
    from services.honeypot_service import HoneypotError
    for bad in ({"action": "nuke"}, {"delete_message_days": 9}, {"delete_message_days": True},
                {"enabled": "yes"}, {"log_channel_id": 12345}, {"bogus": 1}):
        with pytest.raises(HoneypotError) as ei:
            await env.svc.update_config(env.guild, bad)
        assert ei.value.status == 400
    cfg = await env.svc.update_config(env.guild, {"action": "kick", "log_channel_id": None, "enabled": False})
    assert cfg["action"] == "kick" and cfg["log_channel_id"] is None and cfg["enabled"] is False


async def test_setup_blocked_only_when_channel_exists(repo, db):
    from services.honeypot_service import HoneypotError
    env = await build(repo, db)
    with pytest.raises(HoneypotError) as ei:
        await env.svc.setup(env.guild, 1)
    assert ei.value.status == 409 and ei.value.detail == "already_configured"

    # channel gone -> setup may run again (here it reaches the permission check and fails with 502, not 409)
    env.guild.get_channel = lambda cid: None
    env.guild.me.guild_permissions.manage_channels = False
    with pytest.raises(HoneypotError) as ei:
        await env.svc.setup(env.guild, 1)
    assert ei.value.status == 502 and "Manage Channels" in ei.value.detail


async def test_setup_creates_channel_and_embeds(repo, db):
    guild = make_guild()
    guild.me.guild_permissions.manage_channels = True
    guild.me.guild_permissions.ban_members = True
    guild.me.guild_permissions.kick_members = True
    guild.me.guild_permissions.manage_messages = True
    guild.default_role = MagicMock()
    category = MagicMock(spec=discord.CategoryChannel)
    category.id = 2000000000000000001
    trap = make_text_channel(2000000000000000002, "do-not-type")
    msg_ids = iter([11, 12])
    trap.send = AsyncMock(side_effect=lambda **kw: MagicMock(id=next(msg_ids)))
    guild.create_category = AsyncMock(return_value=category)
    guild.create_text_channel = AsyncMock(return_value=trap)
    svc, _ = make_service(guild, repo, db)
    svc._bot.get_guild = lambda gid: guild
    by_id = {trap.id: trap}
    guild.get_channel = lambda cid: by_id.get(cid)

    cfg, warnings = await svc.setup(guild, 7, action="softban")
    assert cfg["configured"] and cfg["enabled"] and cfg["action"] == "softban"
    assert cfg["channel_id"] == trap.id and cfg["category_id"] == category.id
    assert cfg["warning_message_id"] == 11 and cfg["stats_message_id"] == 12
    assert guild.create_category.await_args.kwargs["position"] == 0
    warn_embed = trap.send.await_args_list[0].kwargs["embed"]
    assert "Do **NOT** type here" in warn_embed.description and "trap for hacked accounts and bots" in warn_embed.description
    assert warnings == []


# ---------------------------------------------------------------------------
# Exemption gaps: DB grants, owner-by-id, bare User / member lookup failures
# ---------------------------------------------------------------------------
def perm_service(*, grant=False, grant_exc=None):
    perm = MagicMock()
    perm.is_staff = MagicMock(return_value=False)
    perm.has_any_grant = AsyncMock(return_value=grant, side_effect=grant_exc)
    return perm


async def test_db_permission_grant_holder_is_never_punished(repo, db):
    env = await build(repo, db)
    msg = make_message(make_member(60), env.trap, guild=env.guild)
    await env.svc.handle_message(msg, perm_service(grant=True))
    env.guild.ban.assert_not_awaited()
    msg.delete.assert_not_awaited()
    assert await repo.count_events(GUILD_ID) == 0


async def test_grant_check_failure_fails_closed_to_exempt(repo, db):
    env = await build(repo, db)
    msg = make_message(make_member(61), env.trap, guild=env.guild)
    await env.svc.handle_message(msg, perm_service(grant_exc=RuntimeError("db down")))
    env.guild.ban.assert_not_awaited()
    assert await repo.count_events(GUILD_ID) == 0


async def test_member_without_grants_is_still_punished(repo, db):
    env = await build(repo, db)
    await env.svc.handle_message(make_message(make_member(62), env.trap, guild=env.guild), perm_service())
    env.guild.ban.assert_awaited_once()


async def test_is_exempt_owner_by_id_even_for_bare_user(monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "OWNER_ID", 777)
    user = MagicMock(spec=discord.User)
    user.id, user.bot = 777, False
    assert is_exempt(user, None)
    other = MagicMock(spec=discord.User)
    other.id, other.bot = 778, False
    assert not is_exempt(other, None)


async def test_owner_posting_as_bare_user_is_not_punished(repo, db, monkeypatch):
    import config as cfg
    monkeypatch.setattr(cfg, "OWNER_ID", 777)
    env = await build(repo, db)
    user = MagicMock(spec=discord.User)
    user.id, user.bot, user.name = 777, False, "owner"
    await env.svc.handle_message(make_message(user, env.trap, guild=env.guild), perm_service())
    env.guild.ban.assert_not_awaited()
    assert await repo.count_events(GUILD_ID) == 0


async def test_bare_user_is_resolved_with_fetch_member_and_staff_is_exempt(repo, db):
    env = await build(repo, db)
    user = MagicMock(spec=discord.User)
    user.id, user.bot = 63, False
    env.guild.fetch_member = AsyncMock(return_value=make_member(63, admin=True))
    await env.svc.handle_message(make_message(user, env.trap, guild=env.guild), perm_service())
    env.guild.fetch_member.assert_awaited_once_with(63)
    env.guild.ban.assert_not_awaited()


async def test_bare_user_resolved_to_normal_member_is_punished(repo, db):
    env = await build(repo, db)
    user = MagicMock(spec=discord.User)
    user.id, user.bot = 64, False
    member = make_member(64)
    env.guild.fetch_member = AsyncMock(return_value=member)
    await env.svc.handle_message(make_message(user, env.trap, guild=env.guild), perm_service())
    env.guild.ban.assert_awaited_once()
    member.send.assert_awaited_once()  # the resolved Member is used (DM works)


async def test_member_lookup_error_fails_closed(repo, db):
    env = await build(repo, db)
    user = MagicMock(spec=discord.User)
    user.id, user.bot = 65, False
    env.guild.fetch_member = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500, reason="x"), "boom"))
    await env.svc.handle_message(make_message(user, env.trap, guild=env.guild), perm_service())
    env.guild.ban.assert_not_awaited()
    assert await repo.count_events(GUILD_ID) == 0


async def test_genuine_non_member_is_still_banned_by_id(repo, db):
    env = await build(repo, db)
    user = MagicMock(spec=discord.User)  # spammed then left: fetch_member -> NotFound (default fake)
    user.id, user.bot, user.name = 66, False, "leaver"
    user.send = AsyncMock()
    user.display_avatar = MagicMock(url="https://cdn.example/a.png")
    await env.svc.handle_message(make_message(user, env.trap, guild=env.guild), perm_service())
    env.guild.ban.assert_awaited_once()


# ---------------------------------------------------------------------------
# Softban partial failure, failed-ban DM spam
# ---------------------------------------------------------------------------
async def test_softban_unban_failure_is_loud(repo, db):
    env = await build(repo, db, action="softban")
    env.guild.unban.side_effect = forbidden()
    member = make_member(67)
    await env.svc.handle_message(make_message(member, env.trap, guild=env.guild))
    (event,) = await repo.list_events(GUILD_ID)
    assert event["action_result"] == "partial"
    assert event["errors"][0] == "softban unban failed - user is STILL BANNED, unban manually"
    embed = env.log.send.await_args.kwargs["embed"]
    assert "STILL BANNED" in embed.title and "STILL BANNED" in embed.description
    # and the pre-ban DM no longer promises they can rejoin
    dm = member.send.await_args.kwargs["embed"].description
    assert "welcome to rejoin" not in dm and "may be able to rejoin" in dm


async def test_failed_ban_dms_and_opens_appeal_at_most_once_per_hour(repo, db, monkeypatch):
    import services.honeypot_service as hs
    env = await build(repo, db)
    env.guild.ban.side_effect = forbidden()
    member = make_member(68)
    for i in range(3):
        monkeypatch.setattr(hs, "CATCH_COOLDOWN", 0.0)  # let each message be a fresh catch
        msg = make_message(member, env.trap, f"spam {i}", guild=env.guild)
        await env.svc.handle_message(msg)
        msg.delete.assert_awaited_once()  # still cleaned up every time
    member.send.assert_awaited_once()
    events = await repo.list_events(GUILD_ID)
    assert len(events) == 3 and all(e["action_result"] == "failed" for e in events)
    assert len(await repo.list_appeals(GUILD_ID)) == 1
    assert [e["dm_sent"] for e in reversed(events)] == [True, False, False]


async def test_failed_ban_dm_allowed_again_after_an_hour(repo, db, monkeypatch):
    import services.honeypot_service as hs
    monkeypatch.setattr(hs, "CATCH_COOLDOWN", 0.0)
    env = await build(repo, db)
    env.guild.ban.side_effect = forbidden()
    member = make_member(69)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await repo.add_event(GUILD_ID, 69, "ban", action_result="failed", created_at=old)
    await env.svc.handle_message(make_message(member, env.trap, guild=env.guild))
    member.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# Apps, webhooks and bots in the trap (2026-10-20)
# ---------------------------------------------------------------------------
from services.honeypot_service import app_source, catch_to_dict, message_text  # noqa: E402

APP_ID = 1300000000000000001


def app_reply(env, invoker, *, user_installed=True, content="", embeds=()):
    """A message an app posted because `invoker` ran its command."""
    app_user = make_member(APP_ID, bot=True)
    app_user.name = "SpamApp"
    msg = make_message(app_user, env.trap, content=content, guild=env.guild)
    meta = MagicMock()
    meta.user = invoker
    meta.is_user_integration = MagicMock(return_value=user_installed)
    msg.interaction_metadata = meta
    msg.application_id = APP_ID
    msg.embeds = list(embeds)
    return msg


def embed(title=None, description=None, fields=()):
    e = MagicMock()
    e.title, e.description = title, description
    e.fields = [MagicMock(name=n, value=v) for n, v in fields]
    for f, (n, _) in zip(e.fields, fields):
        f.name = n
    return e


def test_message_text_includes_embeds():
    msg = MagicMock(content="hi", embeds=[embed("Free Nitro", "click", [("Link", "x.y")])])
    assert message_text(msg) == "hi\nFree Nitro\nclick\nLink: x.y"
    assert message_text(MagicMock(content=None, embeds=[])) == ""


def test_app_source():
    assert app_source(MagicMock(interaction_metadata=None)) is None
    invoker = make_member(7)
    meta = MagicMock(user=invoker)
    meta.is_user_integration = MagicMock(return_value=True)
    author = make_member(APP_ID, bot=True)
    author.name = "SpamApp"
    src = app_source(MagicMock(interaction_metadata=meta, application_id=APP_ID, author=author))
    assert src == {"invoker": invoker, "app_id": APP_ID, "app_name": "SpamApp", "user_installed": True}


async def test_user_installed_app_reply_catches_the_invoker(repo, db):
    env = await build(repo, db)
    invoker = make_member(42)
    msg = app_reply(env, invoker, embeds=[embed("FREE NITRO", "discord.gg/abc")])
    await env.svc.handle_message(msg)

    msg.delete.assert_awaited_once()
    env.guild.ban.assert_awaited_once()
    assert env.guild.ban.await_args.args[0] is invoker
    reason = env.guild.ban.await_args.kwargs["reason"]
    assert reason.startswith("Honeypot: posted in #do-not-type via app SpamApp")
    invoker.send.assert_awaited_once()  # the invoker gets the DM, not the app
    rows = await repo.list_events(GUILD_ID, limit=5)
    assert len(rows) == 1 and rows[0]["user_id"] == 42
    assert rows[0]["via_app_id"] == APP_ID and rows[0]["via_app_name"] == "SpamApp"
    assert "FREE NITRO" in rows[0]["content"] and rows[0]["flag_invite"]
    assert catch_to_dict(rows[0])["via_app"] == {"id": str(APP_ID), "name": "SpamApp"}
    log_embed = env.log.send.await_args.kwargs["embed"]
    assert any(f.name == "Posted via app" and "user-installed" in f.value for f in log_embed.fields)


async def test_app_reply_from_staff_invoker_is_left_alone(repo, db):
    env = await build(repo, db)
    msg = app_reply(env, make_member(42, admin=True))
    await env.svc.handle_message(msg, env.bot.permission_service)
    msg.delete.assert_not_awaited()
    env.guild.ban.assert_not_awaited()


async def test_normal_catch_has_no_via_app(repo, db):
    env = await build(repo, db)
    await env.svc.handle_message(make_message(make_member(), env.trap, guild=env.guild))
    row = (await repo.list_events(GUILD_ID, limit=5))[0]
    assert row["via_app_id"] is None and catch_to_dict(row)["via_app"] is None


async def test_own_bot_messages_are_ignored(repo, db):
    env = await build(repo, db)
    msg = make_message(make_member(999, bot=True), env.trap, guild=env.guild)  # BOT_ID
    await env.svc.handle_message(msg)
    msg.delete.assert_not_awaited()


async def test_webhook_message_deleted_with_its_webhook_and_logged_once(repo, db):
    audit = MagicMock()
    audit.log = AsyncMock()
    env = await build(repo, db)
    env.svc._audit = audit
    hook = MagicMock(id=4242)
    hook.delete = AsyncMock()
    env.trap.webhooks = AsyncMock(return_value=[MagicMock(id=1), hook])

    first = make_message(make_member(4242, bot=True), env.trap, guild=env.guild)
    first.webhook_id = 4242
    second = make_message(make_member(4242, bot=True), env.trap, guild=env.guild)
    second.webhook_id = 4242
    await env.svc.handle_message(first)
    await env.svc.handle_message(second)

    first.delete.assert_awaited_once()
    second.delete.assert_awaited_once()
    hook.delete.assert_awaited_once()
    env.guild.ban.assert_not_awaited()
    assert await repo.count_events(GUILD_ID) == 0
    assert env.log.send.await_count == 1
    assert "webhook was deleted" in env.log.send.await_args.kwargs["embed"].fields[1].value
    audit.log.assert_awaited_once()
    a = audit.log.await_args
    assert a.args[2] == "honeypot_foreign_message" and a.kwargs["detail"]["webhook_deleted"] is True


async def test_webhook_delete_forbidden_is_reported(repo, db):
    env = await build(repo, db)
    env.trap.webhooks = AsyncMock(side_effect=forbidden())
    msg = make_message(make_member(4242, bot=True), env.trap, guild=env.guild)
    msg.webhook_id = 4242
    await env.svc.handle_message(msg)
    msg.delete.assert_awaited_once()
    fields = {f.name: f.value for f in env.log.send.await_args.kwargs["embed"].fields}
    assert "Manage Webhooks" in fields["Errors"]


async def test_bot_message_deleted_but_bot_not_kicked(repo, db):
    env = await build(repo, db)
    bot_member = make_member(5555, bot=True)
    msg = make_message(bot_member, env.trap, guild=env.guild)
    await env.svc.handle_message(msg)
    msg.delete.assert_awaited_once()
    bot_member.kick.assert_not_awaited()
    env.guild.ban.assert_not_awaited()
    assert await repo.count_events(GUILD_ID) == 0
    assert "not kicked" in env.log.send.await_args.kwargs["embed"].fields[1].value
