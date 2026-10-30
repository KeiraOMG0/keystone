from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from services.honeypot_service import HoneypotError
from tests.unit.test_honeypot_support import (
    GUILD_ID, configured, db, make_guild, make_service, repo,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def svc_env(repo, db):
    guild = make_guild()
    svc, bot = make_service(guild, repo, db)
    bot.get_user = MagicMock(return_value=None)
    user = MagicMock()
    user.send = AsyncMock()
    bot.fetch_user = AsyncMock(return_value=user)
    return svc, guild, bot, user


async def expect(coro, status, detail):
    with pytest.raises(HoneypotError) as ei:
        await coro
    assert ei.value.status == status and detail in (ei.value.detail, ei.value.code)


async def test_lookup_valid_and_invalid_token(repo, svc_env):
    svc, *_ = svc_env
    await repo.create_appeal(GUILD_ID, 5, "tok", username="bob")
    assert await svc.lookup_appeal(GUILD_ID, "tok") == {
        "user_id": 5, "username": "bob", "status": "invited",
        "submitted_at": None, "reviewed_at": None, "rejoin_invite_url": None, "rejoined": False,
    }
    await expect(svc.lookup_appeal(GUILD_ID, "wrong"), 404, "invalid_token")
    await expect(svc.lookup_appeal(GUILD_ID + 1, "tok"), 404, "invalid_token")  # other guild


async def test_submit_with_token_flow(repo, svc_env):
    svc, *_ = svc_env
    await repo.create_appeal(GUILD_ID, 5, "tok")
    assert await svc.submit_appeal(GUILD_ID, "Bob", "I was hacked", token="tok") == {"ok": True, "status": "pending"}
    a = await repo.get_appeal_by_token(GUILD_ID, "tok")
    assert a["status"] == "pending" and a["name"] == "Bob" and a["message"] == "I was hacked" and a["submitted_at"]
    await expect(svc.submit_appeal(GUILD_ID, "Bob", "again", token="tok"), 409, "already_submitted")
    await expect(svc.submit_appeal(GUILD_ID, "Bob", "x", token="unknown"), 404, "not_eligible")


async def test_submit_validation(svc_env):
    svc, *_ = svc_env
    for name, msg in (("", "m"), ("   ", "m"), ("n" * 101, "m"), ("ok", "m" * 1501)):
        with pytest.raises(HoneypotError) as ei:
            await svc.submit_appeal(GUILD_ID, name, msg, token="t")
        assert ei.value.status == 400
    # boundary values are accepted (then rejected as not eligible: no such token)
    await expect(svc.submit_appeal(GUILD_ID, "n" * 100, "m" * 1500, token="t"), 404, "not_eligible")


async def test_submit_without_token_needs_a_ban_event(repo, svc_env):
    svc, *_ = svc_env
    assert await svc.submit_appeal(GUILD_ID, "Bob", "hi", user_id=None) == {"ok": True}
    assert await svc.submit_appeal(GUILD_ID, "Bob", "hi", user_id=77) == {"ok": True}
    await repo.add_event(GUILD_ID, 78, "kick")
    await svc.submit_appeal(GUILD_ID, "Bob", "hi", user_id=78)  # kicks aren't appealable
    await repo.add_event(GUILD_ID, 79, "ban", action_result="failed")
    await svc.submit_appeal(GUILD_ID, "Bob", "hi", user_id=79)  # never actually banned
    assert await repo.list_appeals(GUILD_ID) == []


async def test_submit_without_token_creates_unverified_pending_appeal(repo, svc_env):
    svc, *_ = svc_env
    eid = await repo.add_event(GUILD_ID, 80, "ban", username="eve")
    assert await svc.submit_appeal(GUILD_ID, "Eve", "hacked", user_id=80) == {"ok": True}
    a = await repo.get_latest_appeal_for_user(GUILD_ID, 80)
    assert a["status"] == "pending" and a["catch_id"] == eid and a["username"] == "eve" and a["token"]
    assert a["verified"] == 0
    from services.honeypot_service import appeal_to_dict
    assert appeal_to_dict(a)["verified"] is False and "token" not in appeal_to_dict(a)
    # repeating is silently a no-op: no second row, same response
    assert await svc.submit_appeal(GUILD_ID, "Eve", "again", user_id=80) == {"ok": True}
    assert len(await repo.list_appeals(GUILD_ID)) == 1


async def test_token_appeals_are_verified(repo, svc_env):
    svc, *_ = svc_env
    aid = await repo.create_appeal(GUILD_ID, 5, "tok")
    await svc.submit_appeal(GUILD_ID, "Bob", "x", token="tok")
    from services.honeypot_service import appeal_to_dict
    assert appeal_to_dict(await repo.get_appeal(aid))["verified"] is True


@pytest.mark.parametrize("case", ["not_banned", "banned", "already_submitted", "garbage", "kick_only", "failed_ban"])
async def test_tokenless_responses_are_identical_for_every_state(repo, svc_env, case):
    """No enumeration oracle: same return value in every case (route maps it to a fixed body)."""
    svc, *_ = svc_env
    uid = 4242
    if case == "banned":
        await repo.add_event(GUILD_ID, uid, "ban")
    elif case == "already_submitted":
        eid = await repo.add_event(GUILD_ID, uid, "ban")
        await repo.create_appeal(GUILD_ID, uid, "tt", status="pending", catch_id=eid, name="N", message="M")
    elif case == "garbage":
        uid = None
    elif case == "kick_only":
        await repo.add_event(GUILD_ID, uid, "kick")
    elif case == "failed_ban":
        await repo.add_event(GUILD_ID, uid, "ban", action_result="failed")
    before = len(await repo.list_appeals(GUILD_ID))
    assert await svc.submit_appeal(GUILD_ID, "Name", "msg", user_id=uid) == {"ok": True}
    after = len(await repo.list_appeals(GUILD_ID))
    assert after - before == (1 if case == "banned" else 0)


async def test_tokenless_does_not_consume_dm_invite_row(repo, svc_env):
    svc, *_ = svc_env
    eid = await repo.add_event(GUILD_ID, 81, "ban")
    aid = await repo.create_appeal(GUILD_ID, 81, "t81", catch_id=eid)
    await svc.submit_appeal(GUILD_ID, "X", "y", user_id=81)
    assert (await repo.get_appeal(aid))["status"] == "invited"  # the real user's link still works
    assert len(await repo.list_appeals(GUILD_ID)) == 2
    await svc.submit_appeal(GUILD_ID, "Real", "me", token="t81")
    assert (await repo.get_appeal(aid))["status"] == "pending" and (await repo.get_appeal(aid))["verified"] == 1


async def test_new_ban_after_old_appeal_allows_new_unverified_appeal(repo, svc_env):
    svc, *_ = svc_env
    old = await repo.add_event(GUILD_ID, 82, "ban")
    aid = await repo.create_appeal(GUILD_ID, 82, "t82", status="approved", catch_id=old)
    await svc.submit_appeal(GUILD_ID, "Z", "z", user_id=82)
    assert len(await repo.list_appeals(GUILD_ID)) == 1  # old appeal covers the old ban
    await repo.add_event(GUILD_ID, 82, "ban")  # caught again later
    await svc.submit_appeal(GUILD_ID, "Z", "z", user_id=82)
    assert len(await repo.list_appeals(GUILD_ID)) == 2 and (await repo.get_appeal(aid))["status"] == "approved"


async def pending_appeal(repo, user_id=90):
    eid = await repo.add_event(GUILD_ID, user_id, "ban")
    aid = await repo.create_appeal(GUILD_ID, user_id, f"t{user_id}", status="pending", catch_id=eid,
                                   name="N", message="M", submitted_at="2026-01-01T00:00:00+00:00")
    return aid


async def test_approve_unbans_dms_and_records(repo, db, svc_env):
    svc, guild, bot, user = svc_env
    aid = await pending_appeal(repo)
    assert await svc.approve_appeal(guild, aid, 5, "looks legit") == {"ok": True, "unbanned": True, "dm_sent": True}
    guild.unban.assert_awaited_once()
    assert guild.unban.await_args.args[0].id == 90
    a = await repo.get_appeal(aid)
    assert a["status"] == "approved" and a["reviewed_by"] == 5 and a["review_note"] == "looks legit"
    rows = await db.fetchall("SELECT action_type, moderator_id FROM mod_actions")
    assert [(r["action_type"], r["moderator_id"]) for r in rows] == [("unban", 5)]
    user.send.assert_awaited_once()
    await expect(svc.approve_appeal(guild, aid, 5), 409, "not_pending")


async def test_approve_when_not_banned_is_success_without_unban(repo, svc_env):
    svc, guild, bot, user = svc_env
    guild.unban.side_effect = discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Ban")
    user.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="x"), "closed")
    aid = await pending_appeal(repo, 91)
    assert await svc.approve_appeal(guild, aid, 5) == {"ok": True, "unbanned": False, "dm_sent": False}
    assert (await repo.get_appeal(aid))["status"] == "approved"


async def test_approve_forbidden_leaves_appeal_pending(repo, svc_env):
    svc, guild, *_ = svc_env
    guild.unban.side_effect = discord.Forbidden(MagicMock(status=403, reason="x"), "no perms")
    aid = await pending_appeal(repo, 92)
    with pytest.raises(HoneypotError) as ei:
        await svc.approve_appeal(guild, aid, 5)
    assert ei.value.status == 502
    assert (await repo.get_appeal(aid))["status"] == "pending"


async def test_approve_unknown_or_not_pending(repo, svc_env):
    svc, guild, *_ = svc_env
    await expect(svc.approve_appeal(guild, 12345, 5), 404, "not_found")
    aid = await repo.create_appeal(GUILD_ID, 93, "t93")  # still 'invited'
    await expect(svc.approve_appeal(guild, aid, 5), 409, "not_pending")
    guild.unban.assert_not_awaited()


async def test_deny(repo, svc_env):
    svc, *_ = svc_env
    aid = await pending_appeal(repo, 94)
    assert await svc.deny_appeal(GUILD_ID, aid, 5, "nope") == {"ok": True, "dm_sent": True}
    assert (await repo.get_appeal(aid))["outcome_dm_sent"] == 1
    a = await repo.get_appeal(aid)
    assert a["status"] == "denied" and a["review_note"] == "nope"
    await expect(svc.deny_appeal(GUILD_ID, aid, 5), 409, "not_pending")
    await expect(svc.deny_appeal(GUILD_ID, 999, 5), 404, "not_found")


async def test_list_appeals_filters_and_hides_token(repo, svc_env):
    svc, *_ = svc_env
    await pending_appeal(repo, 95)
    await repo.create_appeal(GUILD_ID, 96, "t96")
    assert len(await svc.list_appeals(GUILD_ID)) == 2
    pending = await svc.list_appeals(GUILD_ID, "pending")
    assert len(pending) == 1 and "token" not in pending[0] and pending[0]["user_id"] == 95
    with pytest.raises(HoneypotError):
        await svc.list_appeals(GUILD_ID, "bogus")


async def test_approve_refuses_when_ban_is_not_a_honeypot_ban(repo, svc_env):
    svc, guild, bot, user = svc_env
    guild.fetch_ban.return_value = MagicMock(reason="Raiding")
    aid = await pending_appeal(repo, 97)
    await expect(svc.approve_appeal(guild, aid, 5), 409, "not_a_honeypot_ban")
    guild.unban.assert_not_awaited()
    assert (await repo.get_appeal(aid))["status"] == "pending"
    user.send.assert_not_awaited()


async def test_approve_ban_with_no_reason_is_not_a_honeypot_ban(repo, svc_env):
    svc, guild, *_ = svc_env
    guild.fetch_ban.return_value = MagicMock(reason=None)
    aid = await pending_appeal(repo, 98)
    await expect(svc.approve_appeal(guild, aid, 5), 409, "not_a_honeypot_ban")
    guild.unban.assert_not_awaited()


async def test_approve_not_banned_at_all_succeeds_without_unban_call(repo, svc_env):
    svc, guild, *_ = svc_env
    guild.fetch_ban.side_effect = discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Ban")
    aid = await pending_appeal(repo, 99)
    assert (await svc.approve_appeal(guild, aid, 5))["unbanned"] is False
    guild.unban.assert_not_awaited()
    assert (await repo.get_appeal(aid))["status"] == "approved"
