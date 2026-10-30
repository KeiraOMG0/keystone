"""Dashboard member tools: member profile, verify, purge job, message-log DM
export, server info (services + internal_api/routes/member_tools.py +
server/routes/members.py / moderation.py). Fake discord objects, no network."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
import pytest_asyncio

import config
from services import purge_service
from services.purge_service import PurgeJobs, needs_confirm, parse_purge_amount

GUILD_ID = 100000000000000001
BOT_ID = 900000000000000001
OWNER = 424242424242424242
MOD = 555000000000000005
TARGET = 555000000000000006
HIGH = 555000000000000007
OUTSIDER = 555000000000000009
CHANNEL = 700000000000000001
HIDDEN = 700000000000000002
AUTH = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}


# ---------------------------------------------------------------------------
# purge_service
# ---------------------------------------------------------------------------

def test_purge_bounds_and_confirm_rule():
    assert parse_purge_amount("50") == (50, None, None)
    assert parse_purge_amount("1001")[2] and parse_purge_amount("0")[2]
    assert parse_purge_amount("15m")[1] == timedelta(minutes=15)
    assert parse_purge_amount("15d")[2] and parse_purge_amount("soon")[2]
    assert not needs_confirm(100, None) and needs_confirm(101, None) and needs_confirm(None, timedelta(minutes=1))


@pytest.mark.parametrize("raw", ["1", "100", "101", "1000", "1001", "0", "-5", "30s", "15m", "2h", "14d", "15d",
                                 "336h", "337h", "0m", "abc", "5w", " 20 "])
def test_dashboard_purge_validation_mirrors_service(raw):
    from server.routes.moderation import purge_amount_error

    count, window, error = parse_purge_amount(raw)
    dash_error, dash_confirm = purge_amount_error(raw)
    assert (error is None) == (dash_error is None)
    if error is None:
        assert dash_confirm == needs_confirm(count, window)


async def test_purge_jobs_one_per_channel_and_outcomes():
    jobs = PurgeJobs()
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return 7

    job = jobs.start(1, 10, slow, summary={"amount": "7"})
    assert job["status"] == "running" and job["amount"] == "7"
    assert jobs.start(1, 10, slow) is None  # same channel busy
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    done = jobs.get(1, job["job_id"])
    assert done["status"] == "done" and done["deleted"] == 7 and done["finished_at"]
    assert jobs.get(2, job["job_id"]) is None  # other guild

    async def forbidden():
        raise discord.Forbidden(MagicMock(status=403, reason="x"), "Missing Permissions")

    j2 = jobs.start(1, 10, forbidden)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    failed = jobs.get(1, j2["job_id"])
    assert failed["status"] == "failed" and "Manage Messages" in failed["error"]


# ---------------------------------------------------------------------------
# verify_service
# ---------------------------------------------------------------------------

async def test_verify_member_swaps_roles_and_audits_source(monkeypatch):
    from services import verify_service

    monkeypatch.setattr(config, "VERIFIED_ROLE_ID", 11)
    monkeypatch.setattr(config, "UNVERIFIED_ROLE_ID", 12)
    verified, unverified = SimpleNamespace(id=11), SimpleNamespace(id=12)
    guild = SimpleNamespace(id=GUILD_ID, name="G", get_role=lambda rid: {11: verified, 12: unverified}.get(rid))
    member = SimpleNamespace(id=TARGET, guild=guild, roles=[unverified], add_roles=AsyncMock(), remove_roles=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock())
    templates = SimpleNamespace(get_full=AsyncMock(return_value=None))
    audit = SimpleNamespace(log=AsyncMock())
    result = await verify_service.verify_member(
        member, SimpleNamespace(id=MOD), channel, templates, None, audit, source="dashboard",
    )
    assert result.ok
    member.remove_roles.assert_awaited_once()
    member.add_roles.assert_awaited_once()
    channel.send.assert_awaited_once()
    assert audit.log.await_args.kwargs["detail"]["source"] == "dashboard"

    member.roles = [verified]
    again = await verify_service.verify_member(member, SimpleNamespace(id=MOD), channel, templates, None, audit)
    assert not again.ok and again.already_verified


def test_cog_verify_reexports_service():
    from cogs.verify import verify_member as cog_fn
    from services.verify_service import verify_member as svc_fn

    assert cog_fn is svc_fn


# ---------------------------------------------------------------------------
# member_profile_service
# ---------------------------------------------------------------------------

def _role(rid, pos, default=False):
    return SimpleNamespace(id=rid, name=f"r{rid}", position=pos, color=SimpleNamespace(value=0),
                           is_default=lambda: default)


async def test_profile_member_and_departed_states(monkeypatch):
    from services.member_profile_service import build_profile

    monkeypatch.setattr(config, "VERIFIED_ROLE_ID", 31)
    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    target = SimpleNamespace(
        id=TARGET, name="t", global_name=None, display_name="T", bot=False, nick=None, joined_at=None,
        premium_since=None, pending=False, color=SimpleNamespace(value=0), timed_out_until=None,
        is_timed_out=lambda: False, roles=[_role(GUILD_ID, 0, True), _role(31, 3), _role(32, 5)],
        top_role=SimpleNamespace(position=5), display_avatar=SimpleNamespace(url="https://cdn/a.png"),
    )
    mod = SimpleNamespace(id=MOD, top_role=SimpleNamespace(position=2))
    guild = SimpleNamespace(id=GUILD_ID, owner_id=None, me=SimpleNamespace(id=BOT_ID, top_role=SimpleNamespace(position=10)))
    guild.get_member = lambda uid: target if uid == TARGET else None
    guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Member"))
    guild.fetch_ban = AsyncMock(return_value=SimpleNamespace(reason="spam"))
    bot = SimpleNamespace(get_user=lambda uid: None, fetch_user=AsyncMock(return_value=None))
    warnings = SimpleNamespace(get_warnings=AsyncMock(return_value=[{"id": 1, "user_id": TARGET}]))
    history = [{"action_type": "kick", "created_at": "2026-10-01"}]
    mod_svc = SimpleNamespace(get_history=AsyncMock(return_value=history))
    hp = SimpleNamespace(list_events_for_user=AsyncMock(return_value=[]), get_latest_real_appeal_for_user=AsyncMock(return_value=None))

    p = await build_profile(bot, guild, TARGET, warning_service=warnings, moderation_service=mod_svc,
                            honeypot_repo=hp, include_honeypot=True, actor=mod)
    assert p["status"] == "member" and p["in_guild"] and p["member"]["verified"]
    assert [r["id"] for r in p["member"]["roles"]] == ["32", "31"]  # @everyone dropped, highest first
    assert p["actor_block_reason"]  # the target ranks above the moderator
    assert p["honeypot"] == {"catches": [], "latest_appeal": None}
    guild.fetch_ban.assert_not_awaited()

    p = await build_profile(bot, guild, OUTSIDER, warning_service=warnings, moderation_service=mod_svc)
    assert p["status"] == "banned" and p["ban"] == {"banned": True, "reason": "spam"}
    assert p["user"]["known"] is False and p["user"]["created_at"] and p["honeypot"] is None

    guild.fetch_ban = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Ban"))
    p = await build_profile(bot, guild, OUTSIDER, warning_service=warnings, moderation_service=mod_svc)
    assert p["status"] == "kicked" and p["last_departure"]["action"] == "kick"


# ---------------------------------------------------------------------------
# Internal API routes
# ---------------------------------------------------------------------------

def _perms(**flags):
    base = dict(view_channel=True, read_message_history=True, manage_messages=True)
    base.update(flags)
    return SimpleNamespace(**base)


def _member(uid, pos, staff=False):
    m = SimpleNamespace(id=uid, top_role=SimpleNamespace(position=pos), name=f"u{uid}", display_name=f"U{uid}",
                        roles=[], display_avatar=SimpleNamespace(url="https://cdn/x.png"), send=AsyncMock())
    m.staff = staff
    return m


def _make_guild():
    members = {MOD: _member(MOD, 5), TARGET: _member(TARGET, 2), OWNER: _member(OWNER, 1, staff=True)}
    guild = SimpleNamespace(id=GUILD_ID, owner_id=None, members=list(members.values()))
    guild.me = _member(BOT_ID, 10)
    guild.get_member = lambda uid: members.get(uid)
    guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "Unknown Member"))
    visible = MagicMock(spec=discord.TextChannel)
    visible.id, visible.name, visible.mention = CHANNEL, "general", f"<#{CHANNEL}>"
    visible.permissions_for = lambda who: _perms()
    visible.purge = AsyncMock(return_value=[1, 2, 3])
    hidden = MagicMock(spec=discord.TextChannel)
    hidden.id, hidden.name = HIDDEN, "staff"
    hidden.permissions_for = lambda who: _perms(view_channel=getattr(who, "id", None) == BOT_ID)
    for i, ch in enumerate((visible, hidden)):
        ch.category, ch.position = None, i
    channels = {CHANNEL: visible, HIDDEN: hidden}
    guild.text_channels = [visible, hidden]
    guild.get_channel = lambda cid: channels.get(cid)
    guild.m, guild.ch = members, channels
    return guild


@pytest_asyncio.fixture
async def api(monkeypatch):
    from internal_api.app import create_app
    from internal_api.routes import member_tools

    monkeypatch.setattr(config, "OWNER_ID", OWNER)
    guild = _make_guild()
    # resolve_actor wants discord.Member for rank checks; for these routes a
    # member-shaped object is enough, so treat our fakes as Members.
    monkeypatch.setattr(member_tools.discord, "Member", SimpleNamespace, raising=False)
    bot = MagicMock()
    bot.get_guild = lambda gid: guild if gid == GUILD_ID else None
    bot.permission_service = SimpleNamespace(is_staff=lambda m: getattr(m, "staff", False))
    audit = SimpleNamespace(log=AsyncMock())
    monkeypatch.setattr(member_tools, "_audit", lambda db: audit)
    mod = SimpleNamespace(send_mod_log=AsyncMock())
    monkeypatch.setattr(member_tools, "_get_moderation_service", lambda b: mod)
    monkeypatch.setattr(purge_service, "JOBS", PurgeJobs())
    app = create_app()
    app.state.bot = bot
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AUTH) as c:
        c.guild, c.bot, c.audit, c.mod = guild, bot, audit, mod
        yield c


async def test_purge_route_rules_and_job(api):
    base = f"/guilds/{GUILD_ID}/moderation/purge"
    body = {"moderator_id": MOD, "channel_id": CHANNEL, "amount": "500"}
    r = await api.post(base, json=body)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "confirm_required"
    r = await api.post(base, json={**body, "amount": "15d", "confirm": True})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_input"
    r = await api.post(base, json={**body, "channel_id": HIDDEN, "amount": "5"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "channel_forbidden"
    r = await api.post(base, json={**body, "amount": "5", "user_id": TARGET})
    assert r.status_code == 200
    job = r.json()
    assert job["status"] == "running" and job["channel_id"] == str(CHANNEL) and job["user_id"] == str(TARGET)
    for _ in range(5):
        await asyncio.sleep(0)
    r = await api.get(f"{base}/{job['job_id']}")
    assert r.json()["status"] == "done" and r.json()["deleted"] == 3
    kwargs = api.guild.ch[CHANNEL].purge.await_args.kwargs
    assert kwargs["limit"] == purge_service.PURGE_FILTERED_SCAN_LIMIT and kwargs["before"] is None
    detail = api.audit.log.await_args.kwargs["detail"]
    assert api.audit.log.await_args.args[2] == "messages_purged" and detail["source"] == "dashboard"
    api.mod.send_mod_log.assert_awaited_once()
    assert (await api.get(f"{base}/nope")).status_code == 404
    r = await api.post(base, json={**body, "moderator_id": OUTSIDER, "amount": "5"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "actor_not_member"


async def test_purge_channels_only_visible(api):
    r = await api.get(f"/guilds/{GUILD_ID}/moderation/purge-channels", params={"actor_id": MOD})
    assert r.status_code == 200
    assert r.json()["channels"] == [{"id": str(CHANNEL), "name": "general", "category": None, "bot_can_purge": True}]
    r = await api.get(f"/guilds/{GUILD_ID}/moderation/purge-channels", params={"actor_id": OWNER})
    assert [c["id"] for c in r.json()["channels"]] == [str(CHANNEL), str(HIDDEN)]


async def test_message_log_export_staff_only_and_dm(api, tmp_path):
    from services.message_log_service import MessageLogStore

    store = MessageLogStore(str(tmp_path / "m.db"), 7)
    await store.open()
    try:
        store.record({
            "message_id": 1, "guild_id": GUILD_ID, "channel_id": CHANNEL, "channel_name": "general",
            "author_id": TARGET, "author_name": "t", "author_kind": "member", "invoker_id": None, "app_id": None,
            "app_name": None, "webhook_id": None, "content": "hello there", "attachments": None,
            "created_at": __import__("services.message_log_service", fromlist=["_ts"])._ts(
                __import__("services.message_log_service", fromlist=["_now"])._now()),
        })
        api.bot.get_cog = lambda name: SimpleNamespace(store=store) if name == "MessageLog" else None
        url = f"/guilds/{GUILD_ID}/message-log/export"
        r = await api.post(url, json={"actor_id": MOD, "kind": "user", "user_id": TARGET})
        assert r.status_code == 403 and r.json()["detail"]["error"] == "not_staff"
        r = await api.post(url, json={"actor_id": OWNER, "kind": "user", "user_id": TARGET, "window": "2h"})
        assert r.status_code == 200 and r.json() == {"sent": True, "count": 1}
        sent = api.guild.m[OWNER].send.await_args
        assert b"hello there" in sent.kwargs["file"].fp.read()
        assert api.audit.log.await_args.args[2] == "message_log_exported"
        r = await api.post(url, json={"actor_id": OWNER, "kind": "user", "user_id": OUTSIDER})
        assert r.status_code == 404 and r.json()["detail"]["error"] == "no_messages"
        r = await api.post(url, json={"actor_id": OWNER, "kind": "deleted", "window": "soon"})
        assert r.status_code == 422
        api.guild.m[OWNER].send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "no dms"))
        r = await api.post(url, json={"actor_id": OWNER, "kind": "channel", "channel_id": CHANNEL})
        assert r.status_code == 409 and r.json()["detail"]["error"] == "dm_failed"
    finally:
        await store.close()


async def test_message_log_unavailable(api):
    api.bot.get_cog = lambda name: None
    r = await api.post(f"/guilds/{GUILD_ID}/message-log/export", json={"actor_id": OWNER, "kind": "deleted"})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "message_log_unavailable"
    r = await api.get(f"/guilds/{GUILD_ID}/message-log/status")
    assert r.json()["enabled"] is False


async def test_verify_route_requires_unverified_category_channel(api, monkeypatch):
    monkeypatch.setattr(config, "UNVERIFIED_CATEGORY_ID", 0)
    r = await api.post(f"/guilds/{GUILD_ID}/members/{TARGET}/verify", json={"actor_id": MOD, "channel_id": CHANNEL})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_channel"
    r = await api.post(f"/guilds/{GUILD_ID}/members/{OUTSIDER}/verify", json={"actor_id": MOD, "channel_id": CHANNEL})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "member_not_found"


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------

SESSION_USER = 424242424242424243


@pytest.fixture
def dash(monkeypatch):
    from fastapi.testclient import TestClient

    from server import config as sconfig
    from server.app import app
    from server.auth.dependencies import require_session
    from server.db.models import Session
    from server.services import internal_client

    granted = {"staff_base", "moderate_members", "manage_messages_bulk", "verify_members"}

    async def check(guild_id, user_id, permission):
        return permission in granted

    async def bulk(guild_id, user_id, permissions):
        return {p: p in granted for p in permissions}

    async def fake_session():
        s = Session()
        s.session_id = "s"
        s.discord_user_id = SESSION_USER
        s.discord_username = "keira"
        s.is_guild_member = True
        return s

    monkeypatch.setattr(internal_client, "check_permission", check)
    monkeypatch.setattr(internal_client, "check_permissions_bulk", bulk)
    app.dependency_overrides[require_session] = fake_session
    client = TestClient(app, headers={"Origin": sconfig.FRONTEND_BASE_URL})
    client.ic, client.granted = internal_client, granted
    yield client
    app.dependency_overrides.pop(require_session, None)


def test_dash_purge_validates_confirms_and_uses_session(dash, monkeypatch):
    seen = {}

    async def fake_start(guild_id, **kw):
        seen.update(kw)
        return {"job_id": "abc", "status": "running"}

    monkeypatch.setattr(dash.ic, "start_purge", fake_start)
    assert dash.post("/api/moderation/purge", json={"channel_id": CHANNEL, "amount": "1001"}).status_code == 422
    r = dash.post("/api/moderation/purge", json={"channel_id": CHANNEL, "amount": "2h"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "confirm_required"
    r = dash.post("/api/moderation/purge", json={"channel_id": CHANNEL, "amount": "2h", "confirm": True, "moderator_id": 1})
    assert r.status_code == 202 and seen["moderator_id"] == SESSION_USER and seen["amount"] == "2h"
    dash.granted.discard("manage_messages_bulk")
    r = dash.post("/api/moderation/purge", json={"channel_id": CHANNEL, "amount": "5"})
    assert r.status_code == 403 and r.json()["detail"]["required_permission"] == "manage_messages_bulk"


def test_dash_message_log_export_is_staff_only(dash, monkeypatch):
    seen = {}

    async def fake_export(guild_id, **kw):
        seen.update(kw)
        return {"sent": True, "count": 2}

    async def not_staff(guild_id, user_id):
        return False

    async def staff(guild_id, user_id):
        return True

    monkeypatch.setattr(dash.ic, "export_message_log", fake_export)
    monkeypatch.setattr(dash.ic, "check_is_staff", not_staff)
    assert dash.post("/api/moderation/message-log/export", json={"kind": "deleted"}).status_code == 403
    monkeypatch.setattr(dash.ic, "check_is_staff", staff)
    r = dash.post("/api/moderation/message-log/export", json={"kind": "user", "user_id": str(TARGET), "window": "6h"})
    assert r.status_code == 200 and seen["actor_id"] == SESSION_USER and seen["user_id"] == TARGET
    assert dash.post("/api/moderation/message-log/export", json={"kind": "user"}).status_code == 422
    assert dash.post("/api/moderation/message-log/export", json={"kind": "deleted", "window": "1w"}).status_code == 422


def test_dash_profile_hides_records_by_permission(dash, monkeypatch):
    calls = {}

    async def fake_profile(guild_id, user_id, *, actor_id, include_honeypot):
        calls.update(actor_id=actor_id, include_honeypot=include_honeypot)
        return {"user_id": str(user_id), "warnings": [1], "history": [2], "honeypot": {"catches": []}}

    monkeypatch.setattr(dash.ic, "get_member_profile", fake_profile)
    r = dash.get(f"/api/members/{TARGET}/profile")
    assert r.status_code == 200 and r.json()["restricted"] == [] and calls["include_honeypot"] is True
    assert calls["actor_id"] == SESSION_USER
    dash.granted.discard("moderate_members")
    dash.granted.add("kick_members")
    body = dash.get(f"/api/members/{TARGET}/profile").json()
    assert body["warnings"] is None and body["honeypot"] is None and body["history"] == [2]
    assert calls["include_honeypot"] is False
    dash.granted.discard("staff_base")
    assert dash.get(f"/api/members/{TARGET}/profile").status_code == 403


def test_dash_member_read_routes_accept_kick_or_ban(dash, monkeypatch):
    async def fake_search(guild_id, q, limit):
        return {"members": []}

    async def fake_history(guild_id, user_id):
        return {"actions": []}

    monkeypatch.setattr(dash.ic, "search_members", fake_search)
    monkeypatch.setattr(dash.ic, "get_mod_history", fake_history)
    dash.granted.clear()
    assert dash.get("/api/moderation/members/search?q=ab").status_code == 403
    dash.granted.add("ban_members")
    assert dash.get("/api/moderation/members/search?q=ab").status_code == 200
    assert dash.get(f"/api/moderation/history/{TARGET}").status_code == 200
    # warnings stay moderate_members (`!warnings`)
    assert dash.get(f"/api/moderation/warnings/{TARGET}").status_code == 403


def test_dash_verify_passes_session(dash, monkeypatch):
    seen = {}

    async def fake_verify(guild_id, user_id, *, actor_id, channel_id):
        seen.update(user_id=user_id, actor_id=actor_id, channel_id=channel_id)
        return {"ok": True}

    monkeypatch.setattr(dash.ic, "verify_member", fake_verify)
    r = dash.post(f"/api/members/{TARGET}/verify", json={"channel_id": str(CHANNEL)})
    assert r.status_code == 200 and seen == {"user_id": TARGET, "actor_id": SESSION_USER, "channel_id": CHANNEL}
    dash.granted.discard("verify_members")
    assert dash.post(f"/api/members/{TARGET}/verify", json={"channel_id": str(CHANNEL)}).status_code == 403


async def test_manual_kick_fails_closed_without_moderation_service(monkeypatch):
    """A human `!kick` is never done without the rank check (no bot.db)."""
    import cogs.kick as kick_mod
    from services.moderation_service import ModerationBlocked

    number = AsyncMock(return_value=1)
    monkeypatch.setattr(kick_mod, "next_kick_number", number)
    fake_self = SimpleNamespace(moderation_service=None, bot=SimpleNamespace(user=SimpleNamespace(id=BOT_ID)))
    target = SimpleNamespace(id=TARGET, kick=AsyncMock())
    guild = SimpleNamespace(id=GUILD_ID, get_member=lambda uid: None)
    with pytest.raises(ModerationBlocked, match="unavailable"):
        await kick_mod.KickCog.do_kick(fake_self, target, guild, None, actor_id=MOD, reason="x")
    target.kick.assert_not_awaited()
    number.assert_not_awaited()
