"""Appeal outcomes (migration 030): hashed link tokens, the outcome shown to the person,
outcome DMs, appeal by Discord sign-in, one-click rejoin (guilds.join), the rejoin
invite link, and the dashboard's public appeal sign-in routes."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from fastapi.testclient import TestClient

from database.repositories.honeypot_repo import hash_token
from services.honeypot_service import HoneypotError
from tests.unit.test_honeypot_support import (  # noqa: F401
    CHANNEL_ID, GUILD_ID, configured, db, make_guild, make_member, make_message, make_service,
    make_text_channel, repo,
)

INVITE = "https://discord.gg/abcDEF"


def _http_error(status: int, code: int, cls=discord.HTTPException):
    return cls(MagicMock(status=status, reason="x"), {"code": code, "message": "m"})


def _not_found():
    return discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Ban")


async def _svc(repo, db, **kw):
    guild = make_guild([make_text_channel(CHANNEL_ID, "trap")])
    service, bot = make_service(guild, repo, db, **kw)
    bot.get_user = MagicMock(return_value=None)
    user = MagicMock(send=AsyncMock())
    bot.fetch_user = AsyncMock(return_value=user)
    await configured(repo)
    return service, bot, guild, user


async def _ban_event(repo, uid: int) -> int:
    return await repo.add_event(GUILD_ID, uid, "ban", username=f"u{uid}")


# --------------------------------------------------------------------------
# Token hashing
# --------------------------------------------------------------------------

async def test_tokens_are_stored_hashed_and_legacy_rows_converted(repo, db):
    service, *_ = await _svc(repo, db)
    aid = await repo.create_appeal(GUILD_ID, 5, "rawtoken123")
    row = await repo.get_appeal(aid)
    assert row["token"] == hash_token("rawtoken123") and "rawtoken123" not in row["token"]
    # A row written before migration 030 (raw token in the column):
    await db.execute(
        "INSERT INTO honeypot_appeals (guild_id, user_id, token, status, created_at) VALUES (?, ?, ?, 'invited', 'x')",
        (GUILD_ID, 6, "legacytoken456"),
    )
    assert await service.hash_legacy_tokens() == 1
    assert await service.hash_legacy_tokens() == 0  # idempotent
    assert (await repo.get_appeal_by_token(GUILD_ID, "legacytoken456"))["user_id"] == 6
    assert await repo.get_appeal_by_token(GUILD_ID, hash_token("legacytoken456")) is None  # the hash is not a key


# --------------------------------------------------------------------------
# Outcome shown to the person + outcome DMs
# --------------------------------------------------------------------------

async def test_lookup_shows_own_outcome_and_invite_only_when_approved(repo, db):
    service, _bot, guild, user = await _svc(repo, db)
    await repo.upsert_config(GUILD_ID, rejoin_invite_url=INVITE)
    eid = await _ban_event(repo, 50)
    await repo.create_appeal(GUILD_ID, 50, "tok50", status="pending", catch_id=eid, name="n", message="m",
                             submitted_at="2026-10-28T00:00:00+00:00")
    out = await service.lookup_appeal(GUILD_ID, "tok50")
    assert out["status"] == "pending" and out["rejoin_invite_url"] is None and out["submitted_at"]
    guild.fetch_ban = AsyncMock(side_effect=_not_found())
    res = await service.approve_appeal(guild, (await repo.get_appeal_by_token(GUILD_ID, "tok50"))["id"], 9, "ok")
    assert res["dm_sent"] is True
    dm = user.send.await_args.kwargs["embed"].description
    assert INVITE in dm and "/appeal" in dm
    out = await service.lookup_appeal(GUILD_ID, "tok50")
    assert out["status"] == "approved" and out["rejoin_invite_url"] == INVITE and out["reviewed_at"]
    assert "review_note" not in out and "reviewed_by" not in out


async def test_failed_outcome_dm_is_recorded(repo, db):
    service, bot, *_ = await _svc(repo, db)
    bot.fetch_user = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "Cannot send"))
    aid = await repo.create_appeal(GUILD_ID, 51, "t51", status="pending", name="n", message="m")
    assert await service.deny_appeal(GUILD_ID, aid, 9) == {"ok": True, "dm_sent": False}
    assert (await repo.get_appeal(aid))["outcome_dm_sent"] == 0


# --------------------------------------------------------------------------
# Appeal by Discord sign-in
# --------------------------------------------------------------------------

async def test_status_and_verified_submit_consume_the_dm_row(repo, db):
    service, *_ = await _svc(repo, db)
    assert await service.appeal_status_for_user(GUILD_ID, 60) == {"user_id": 60, "eligible": False, "appeal": None}
    eid = await _ban_event(repo, 60)
    invited = await repo.create_appeal(GUILD_ID, 60, "t60", catch_id=eid)
    st = await service.appeal_status_for_user(GUILD_ID, 60)
    assert st["eligible"] is True and st["appeal"] is None  # an untouched DM row is not "an appeal"
    assert await service.submit_verified_appeal(GUILD_ID, 60, "u60", "Name", "msg") == {"ok": True, "status": "pending"}
    row = await repo.get_appeal(invited)
    assert row["status"] == "pending" and row["verified_via"] == "discord_login" and row["verified"] == 1
    st = await service.appeal_status_for_user(GUILD_ID, 60)
    assert st["eligible"] is False and st["appeal"]["status"] == "pending"
    with pytest.raises(HoneypotError) as e:
        await service.submit_verified_appeal(GUILD_ID, 60, "u60", "Name", "again")
    assert e.value.code == "already_submitted" and e.value.status == 409


async def test_verified_submit_without_dm_row_and_not_eligible(repo, db):
    service, *_ = await _svc(repo, db)
    with pytest.raises(HoneypotError) as e:
        await service.submit_verified_appeal(GUILD_ID, 61, None, "Name", "")
    assert e.value.code == "not_eligible" and e.value.status == 404
    await _ban_event(repo, 61)
    await service.submit_verified_appeal(GUILD_ID, 61, "u61", "Name", "")
    row = await repo.get_latest_appeal_for_user(GUILD_ID, 61)
    assert row["status"] == "pending" and row["verified_via"] == "discord_login" and row["token"].startswith("sha256:")


# --------------------------------------------------------------------------
# One-click rejoin
# --------------------------------------------------------------------------

async def _approved(repo, uid: int) -> int:
    eid = await _ban_event(repo, uid)
    return await repo.create_appeal(GUILD_ID, uid, f"t{uid}", status="approved", catch_id=eid)


async def test_rejoin_requires_an_approved_appeal_and_no_ban(repo, db):
    service, bot, guild, _ = await _svc(repo, db)
    bot.http.request = AsyncMock(return_value={"user": {"id": "70"}})
    with pytest.raises(HoneypotError) as e:
        await service.rejoin(guild, 70, "oauth-token")
    assert e.value.code == "not_approved"
    await _approved(repo, 70)
    with pytest.raises(HoneypotError) as e:  # make_guild's fetch_ban says: still banned
        await service.rejoin(guild, 70, "oauth-token")
    assert e.value.code == "still_banned"
    bot.http.request.assert_not_awaited()


async def test_rejoin_adds_member_with_their_token(repo, db):
    audit = MagicMock(log=AsyncMock())
    service, bot, guild, user = await _svc(repo, db, audit=audit)
    guild.fetch_ban = AsyncMock(side_effect=_not_found())
    aid = await _approved(repo, 71)
    bot.http.request = AsyncMock(return_value={"user": {"id": "71"}})
    assert await service.rejoin(guild, 71, "oauth-token") == {"result": "joined"}
    route = bot.http.request.await_args.args[0]
    assert route.method == "PUT" and route.path == "/guilds/{guild_id}/members/{user_id}"
    assert bot.http.request.await_args.kwargs["json"] == {"access_token": "oauth-token"}
    assert (await repo.get_appeal(aid))["rejoined_at"]
    assert user.send.await_count == 1  # welcome DM (now sharing a server)
    assert audit.log.await_args.args[2] == "honeypot_appeal_rejoin"
    assert "oauth-token" not in repr(audit.log.await_args)  # the token never reaches the audit log
    bot.http.request = AsyncMock(return_value="")  # 204: already a member
    assert await service.rejoin(guild, 71, "oauth-token") == {"result": "already_member"}


@pytest.mark.parametrize("exc, code", [
    (_http_error(400, 30001), "too_many_guilds"),
    (_http_error(401, 50025), "login_expired"),
    (_http_error(403, 40007, discord.Forbidden), "still_banned"),
    (_http_error(403, 50013, discord.Forbidden), "missing_permissions"),
    (_http_error(500, 0), "discord_error"),
    # Discord's error code wins whatever the HTTP status (agent review, 2026-10-28):
    (_http_error(400, 40007), "still_banned"),
    (_http_error(403, 30001, discord.Forbidden), "too_many_guilds"),
])
async def test_rejoin_error_mapping(repo, db, exc, code):
    service, bot, guild, _ = await _svc(repo, db)
    guild.fetch_ban = AsyncMock(side_effect=_not_found())
    await _approved(repo, 72)
    bot.http.request = AsyncMock(side_effect=exc)
    with pytest.raises(HoneypotError) as e:
        await service.rejoin(guild, 72, "oauth-token")
    assert e.value.code == code


# --------------------------------------------------------------------------
# Rejoin invite link
# --------------------------------------------------------------------------

async def test_invite_url_validation(repo, db):
    service, bot, guild, _ = await _svc(repo, db)
    good = MagicMock(guild=MagicMock(id=GUILD_ID), expires_at=None)
    bot.fetch_invite = AsyncMock(return_value=good)
    cfg = await service.update_config(guild, {"rejoin_invite_url": "discord.com/invite/abcDEF"})
    assert cfg["rejoin_invite_url"] == INVITE
    assert bot.fetch_invite.await_args.args[0] == "abcDEF"
    for bad, invite in (("https://evil.example/x", good),
                        (INVITE, MagicMock(guild=MagicMock(id=1), expires_at=None)),
                        (INVITE, MagicMock(guild=MagicMock(id=GUILD_ID), expires_at="2026-11-01"))):
        bot.fetch_invite = AsyncMock(return_value=invite)
        with pytest.raises(HoneypotError) as e:
            await service.update_config(guild, {"rejoin_invite_url": bad})
        assert e.value.status == 400
    assert (await service.update_config(guild, {"rejoin_invite_url": None}))["rejoin_invite_url"] is None


async def test_create_rejoin_invite(repo, db):
    service, _bot, guild, _ = await _svc(repo, db)
    channel = guild.get_channel(CHANNEL_ID)
    channel.create_invite = AsyncMock(return_value=MagicMock(code="zzz111"))
    cfg = await service.create_rejoin_invite(guild, CHANNEL_ID, 9)
    assert cfg["rejoin_invite_url"] == "https://discord.gg/zzz111"
    assert channel.create_invite.await_args.kwargs["max_age"] == 0
    channel.create_invite = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "no"))
    with pytest.raises(HoneypotError) as e:
        await service.create_rejoin_invite(guild, CHANNEL_ID, 9)
    assert e.value.status == 403


async def test_invite_in_kick_dm_but_never_ban_dm(repo, db):
    service, *_ = await _svc(repo, db)
    await repo.upsert_config(GUILD_ID, rejoin_invite_url=INVITE, invite_in_removal_dm=True)
    guild = make_guild()
    channel = make_text_channel(CHANNEL_ID, "trap")
    kick = service._build_dm_embed(guild, channel, "kick", None, INVITE).description
    assert INVITE in kick
    ban = service._build_dm_embed(guild, channel, "ban", "https://x/appeal/t", INVITE).description
    assert INVITE not in ban


# --------------------------------------------------------------------------
# Debug-page appeal-flow test cases (migration 031)
# --------------------------------------------------------------------------

OWNER = 1000


async def test_test_cases_drive_the_real_flow_and_stay_out_of_stats(repo, db):
    audit = MagicMock(log=AsyncMock())
    service, bot, guild, _ = await _svc(repo, db, audit=audit)
    bot.fetch_user = AsyncMock(return_value=MagicMock(name="alt", send=AsyncMock()))
    real = await _ban_event(repo, 80)  # one real catch, for comparison
    res = await service.create_test_case(guild, 81, "eligible", OWNER)
    assert res["appeal_id"] is None
    # The test catch makes the alt eligible like a real one...
    assert (await service.appeal_status_for_user(GUILD_ID, 81))["eligible"] is True
    await service.submit_verified_appeal(GUILD_ID, 81, "alt", "N", "m")
    # ...but never shows in the feed, the stats or the "Caught so far" count.
    assert [e["id"] for e in await repo.list_events(GUILD_ID)] == [real]
    assert await repo.count_events(GUILD_ID) == 1 and (await service.get_stats(GUILD_ID))["totals"]["all"] == 1
    assert (await service.get_stats(GUILD_ID))["appeals"]["pending"] == 0
    appeals = await service.list_appeals(GUILD_ID, "pending")
    assert len(appeals) == 1 and appeals[0]["is_test"] is True and appeals[0]["user_id"] == 81
    assert audit.log.await_args_list[0].args[2] == "honeypot_test_case_created"


async def test_approved_test_case_can_rejoin_and_clear(repo, db):
    service, bot, guild, _ = await _svc(repo, db)
    guild.fetch_ban = AsyncMock(side_effect=_not_found())  # test cases never ban anyone
    await service.create_test_case(guild, 82, "approved", OWNER)
    st = await service.appeal_status_for_user(GUILD_ID, 82)
    assert st["eligible"] is False and st["appeal"]["status"] == "approved"
    bot.http.request = AsyncMock(return_value={"user": {"id": "82"}})
    assert await service.rejoin(guild, 82, "tok") == {"result": "joined"}
    await service.create_test_case(guild, 83, "pending", OWNER)
    cases = await service.list_test_cases(GUILD_ID)
    assert {c["user_id"]: c["status"] for c in cases} == {83: "pending", 82: "approved"}
    assert cases[1]["rejoined_at"]
    assert await service.delete_test_cases(GUILD_ID, OWNER) == {"catches": 2, "appeals": 2}
    assert await service.list_test_cases(GUILD_ID) == []
    assert await service.appeal_status_for_user(GUILD_ID, 82) == {"user_id": 82, "eligible": False, "appeal": None}


async def test_test_case_refusals(repo, db):
    service, bot, guild, _ = await _svc(repo, db)
    await _ban_event(repo, 84)
    for uid, state in ((84, "eligible"), (OWNER, "eligible"), (bot.user.id, "eligible"), (85, "banned")):
        with pytest.raises(HoneypotError) as e:
            await service.create_test_case(guild, uid, state, OWNER)
        assert e.value.status == 400
    bot.fetch_user = AsyncMock(side_effect=_not_found())
    with pytest.raises(HoneypotError):
        await service.create_test_case(guild, 86, "eligible", OWNER)


# --------------------------------------------------------------------------
# Dashboard: public appeal sign-in routes
# --------------------------------------------------------------------------

@pytest.fixture
def web():
    from server.app import app
    from server.auth.oauth import limiter

    limiter.reset()
    yield TestClient(app)
    limiter.reset()


def _identity_cookie(uid="123456789012345678", name="alt"):
    from server.routes import appeal_auth

    return appeal_auth._identity_signer.dumps({"uid": uid, "name": name})


def test_me_and_submit_need_a_valid_sign_in(web):
    from server import config

    assert web.get("/api/public/appeal-auth/me").json() == {"signed_in": False}
    web.cookies.set("appeal_identity", "forged.value", path="/api/public/appeal-auth")
    assert web.get("/api/public/appeal-auth/me").json() == {"signed_in": False}
    r = web.post("/api/public/appeal-auth/submit", json={"name": "a", "message": ""},
                 headers={"Origin": config.FRONTEND_BASE_URL})
    assert r.status_code == 401 and r.json() == {"error": "not_signed_in"}


def test_me_and_submit_signed_in(web, monkeypatch):
    from server import config
    from server.services import internal_client

    seen = {}

    async def status(guild_id, uid):
        seen["status"] = uid
        return {"eligible": False, "appeal": {"status": "approved", "rejoin_invite_url": INVITE,
                                              "reviewed_at": "x", "submitted_at": "y", "rejoined": False,
                                              "review_note": "internal!"}}

    async def submit(guild_id, uid, username, name, message):
        seen["submit"] = (uid, username, name)
        return {"ok": True, "status": "pending"}

    monkeypatch.setattr(internal_client, "get_appeal_status_for_user", status)
    monkeypatch.setattr(internal_client, "submit_verified_appeal", submit)
    web.cookies.set("appeal_identity", _identity_cookie(), path="/api/public/appeal-auth")
    body = web.get("/api/public/appeal-auth/me").json()
    assert body["signed_in"] and body["user"] == {"id": "123456789012345678", "username": "alt"}
    assert body["appeal"]["rejoin_invite_url"] == INVITE and "review_note" not in body["appeal"]
    assert seen["status"] == 123456789012345678
    r = web.post("/api/public/appeal-auth/submit", json={"name": " N ", "message": "m"},
                 headers={"Origin": config.FRONTEND_BASE_URL})
    assert r.json() == {"ok": True, "status": "pending"} and seen["submit"] == (123456789012345678, "alt", "N")


class _FakeDiscord:
    """Stands in for httpx.AsyncClient inside appeal_auth.callback."""

    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, **k):
        _FakeDiscord.calls.append(("POST", url, dict(data or {})))
        if url.endswith("/token"):
            return MagicMock(status_code=200, json=lambda: {"access_token": "user-oauth-token"})
        return MagicMock(status_code=200)

    async def get(self, url, headers=None, **k):
        _FakeDiscord.calls.append(("GET", url, headers))
        return MagicMock(status_code=200, json=lambda: {"id": "123456789012345678", "username": "alt"})


def test_callback_rejoin_uses_token_once_then_revokes(web, monkeypatch):
    from server.routes import appeal_auth
    from server.services import internal_client

    _FakeDiscord.calls = []
    monkeypatch.setattr(appeal_auth.httpx, "AsyncClient", _FakeDiscord)
    handed = {}

    async def rejoin(guild_id, uid, token):
        handed.update(uid=uid, token=token)
        return {"result": "joined"}

    monkeypatch.setattr(internal_client, "rejoin_after_appeal", rejoin)
    r = web.get("/api/public/appeal-auth/login?intent=rejoin", follow_redirects=False)
    assert r.status_code == 302 and "guilds.join" in r.headers["location"].replace("+", " ").replace("%20", " ")
    state = [p.split("=", 1)[1] for p in r.headers["location"].split("?", 1)[1].split("&") if p.startswith("state=")][0]
    r = web.get(f"/api/public/appeal-auth/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/appeal?rejoin=joined"
    assert handed == {"uid": 123456789012345678, "token": "user-oauth-token"}
    revokes = [c for c in _FakeDiscord.calls if c[1].endswith("/token/revoke")]
    assert revokes and revokes[0][2]["token"] == "user-oauth-token"
    assert "user-oauth-token" not in r.headers.get("set-cookie", "")  # never kept in a cookie either


def test_callback_rejects_bad_state(web, monkeypatch):
    from server.routes import appeal_auth

    _FakeDiscord.calls = []
    monkeypatch.setattr(appeal_auth.httpx, "AsyncClient", _FakeDiscord)
    r = web.get("/api/public/appeal-auth/callback?code=abc&state=forged", follow_redirects=False)
    assert r.headers["location"] == "/appeal?signin=failed" and _FakeDiscord.calls == []
    r = web.get("/api/public/appeal-auth/callback?error=access_denied", follow_redirects=False)
    assert r.headers["location"] == "/appeal?signin=cancelled"
