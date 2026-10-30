"""User cache: services/user_cache_service.py (own SQLite file in tmp_path),
cogs/user_cache.py (departure + kick attribution), the internal resolve
fallback / forget route (internal_api/routes/resolve.py) and the dashboard
proxies + the "any bot permission" resolve gate (server/routes/resolve.py).
Fake discord objects, no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from fastapi.testclient import TestClient

import config
from services import user_cache_service as ucs
from services.user_cache_service import UserCacheStore

GUILD_ID = 100000000000000001
BOT_ID = 900000000000000001
MOD = 700000000000000007
AUTH = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}


def member(uid, name="alice", *, nick=None, avatar="abc", guild_avatar=None, colour=0, joined=None):
    return SimpleNamespace(
        id=uid, name=name, global_name=name.title(), nick=nick, display_name=nick or name.title(),
        avatar=SimpleNamespace(key=avatar) if avatar else None,
        guild_avatar=SimpleNamespace(key=guild_avatar) if guild_avatar else None,
        display_avatar=SimpleNamespace(url=f"https://cdn/{guild_avatar or avatar}.png", key=guild_avatar or avatar),
        color=discord.Colour(colour), bot=False, joined_at=joined or datetime(2026, 1, 1, tzinfo=timezone.utc),
        guild=SimpleNamespace(id=GUILD_ID),
    )


def user(uid, name="alice", avatar="new"):
    return SimpleNamespace(
        id=uid, name=name, global_name=name.title(), display_name=name.title(),
        avatar=SimpleNamespace(key=avatar), display_avatar=SimpleNamespace(url=f"https://cdn/{avatar}.png", key=avatar),
        bot=False,
    )


@pytest.fixture
async def store(tmp_path):
    s = UserCacheStore(str(tmp_path / "user_cache.db"), retention_days=365)
    await s.open()
    yield s
    await s.close()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

async def test_member_then_left(store):
    store.upsert_member(member(1, nick="Ali", colour=0xFF0000))
    store.departed(1, "left")
    row = await store.get(1)
    assert row["status"] == "left" and row["nick"] == "Ali" and row["color"] == "#ff0000"
    assert row["departed_at"] and row["last_seen"]


async def test_rejoin_clears_departure(store):
    store.upsert_member(member(1))
    store.departed(1, "kicked")
    store.upsert_member(member(1))
    row = await store.get(1)
    assert row["status"] == "member" and row["departed_at"] is None


async def test_left_after_ban_does_not_downgrade(store):
    store.upsert_member(member(1))
    store.departed(1, "banned")
    store.departed(1, "left")  # the member-remove of the ban, arriving second
    assert (await store.get(1))["status"] == "banned"


async def test_ban_after_left_upgrades(store):
    store.upsert_member(member(1))
    store.departed(1, "left")
    store.departed(1, "banned")
    store.attribute(1, "banned", MOD, "moderator")
    row = await store.get(1)
    assert row["status"] == "banned" and row["departed_by_id"] == MOD


async def test_old_ban_then_new_leave_overwrites(store):
    store.upsert_member(member(1))
    store.departed(1, "banned", at=datetime.now(timezone.utc) - timedelta(days=3))
    store.departed(1, "left")
    assert (await store.get(1))["status"] == "left"


async def test_kick_attribution_upgrades_left_only(store):
    store.upsert_member(member(1))
    store.departed(1, "left")
    store.attribute(1, "kicked", MOD, "moderator")
    row = await store.get(1)
    assert (row["status"], row["departed_by_id"], row["departure_source"]) == ("kicked", MOD, "moderator")
    store.upsert_member(member(2))
    store.attribute(2, "kicked", MOD, "moderator")  # still a member: untouched
    assert (await store.get(2))["status"] == "member"


async def test_unban(store):
    store.departed(5, "banned")
    store.unbanned(5)
    store.unbanned(6)  # unknown user: no row
    assert (await store.get(5))["status"] == "unbanned"
    assert await store.get(6) is None


async def test_user_profile_keeps_nick_and_guild_avatar(store):
    store.upsert_member(member(1, nick="Ali", guild_avatar="gav"))
    store.touch_profile(user(1, name="alice2", avatar="new"))
    row = await store.get(1)
    assert row["username"] == "alice2" and row["display_name"] == "Ali"
    assert row["avatar_hash"] == "new" and row["avatar_url"] == "https://cdn/gav.png"


async def test_coalesces_profile_bursts(store):
    for i in range(5):
        store.upsert_member(member(1, nick=f"n{i}"))
    assert store.pending() == 1
    assert (await store.get(1))["nick"] == "n4"


async def test_sweep_marks_missing_only_when_complete(store):
    store.upsert_member(member(1))
    store.upsert_member(member(2))
    await store.flush()
    await store.sweep_members([member(1)], complete=False)
    assert (await store.get(2))["status"] == "member"
    res = await store.sweep_members([member(1)], complete=True)
    assert res == {"members": 1, "marked_left": 1}
    row = await store.get(2)
    assert row["status"] == "left" and row["departure_source"] == "detected_at_startup"


async def test_sweep_bans(store):
    store.departed(3, "banned")
    await store.flush()
    res = await store.sweep_bans([user(4, "bob")], complete=True)
    assert res == {"banned": 1, "marked_unbanned": 1}
    assert (await store.get(4))["status"] == "banned"
    assert (await store.get(3))["status"] == "unbanned"


async def test_prune_respects_retention_and_bans(store):
    old = datetime.now(timezone.utc) - timedelta(days=400)
    store.departed(1, "left", at=old)
    store.departed(2, "banned", at=old)
    store.departed(3, "left")
    store.upsert_member(member(4), at=old)
    assert await store.prune() == 1
    assert await store.get(1) is None
    assert {r for r in (await store.get_many([2, 3, 4]))} == {2, 3, 4}


async def test_prune_zero_keeps_forever(tmp_path):
    s = UserCacheStore(str(tmp_path / "f.db"), retention_days=0)
    await s.open()
    s.departed(1, "left", at=datetime.now(timezone.utc) - timedelta(days=4000))
    assert await s.prune() == 0
    assert await s.get(1) is not None
    await s.close()


async def test_forget_drops_row_and_pending_ops(store):
    store.upsert_member(member(1))
    await store.flush()
    store.departed(1, "left")
    assert await store.forget(1) is True
    await store.flush()
    assert await store.get(1) is None
    assert await store.forget(1) is False


def test_serialize_departed_shape():
    row = {"user_id": 9, "username": "x", "display_name": None, "global_name": "X", "avatar_url": None,
           "status": "member", "departed_by_id": MOD, "is_bot": 0}
    out = ucs.serialize_departed(row)
    assert out["departed"] is True and out["status"] == "left"
    assert out["display_name"] == "X" and out["departed_by_id"] == str(MOD)
    assert out["avatar_url"].startswith("https://cdn.discordapp.com/embed/avatars/")


# ---------------------------------------------------------------------------
# Cog: kick attribution
# ---------------------------------------------------------------------------

async def test_cog_remove_attributes_bot_kick_to_moderator(store, monkeypatch):
    from cogs import user_cache as cog_mod

    monkeypatch.setattr(cog_mod, "AUDIT_FIRST_DELAY", 0)
    monkeypatch.setattr(cog_mod, "AUDIT_RETRY_DELAY", 0)
    now = datetime.now(timezone.utc)
    entry = SimpleNamespace(target=SimpleNamespace(id=1), user=SimpleNamespace(id=BOT_ID), created_at=now)

    async def audit_logs(action=None, limit=None):
        assert action == discord.AuditLogAction.kick
        yield entry

    guild = SimpleNamespace(id=GUILD_ID, audit_logs=audit_logs)
    mod_actions = SimpleNamespace(get_actions=AsyncMock(return_value=[
        {"action_type": "kick", "moderator_id": MOD, "created_at": now.strftime("%Y-%m-%d %H:%M:%S")},
    ]))
    bot = SimpleNamespace(user=SimpleNamespace(id=BOT_ID), guilds=[])
    cog = cog_mod.UserCache(bot, store, mod_actions)
    m = member(1)
    m.guild = guild
    store.upsert_member(m)
    await cog.on_member_remove(m)
    await cog._attribute(guild, 1, "kicked")
    row = await store.get(1)
    assert (row["status"], row["departed_by_id"], row["departure_source"]) == ("kicked", MOD, "moderator")


async def test_cog_voluntary_leave_stays_left(store, monkeypatch):
    from cogs import user_cache as cog_mod

    monkeypatch.setattr(cog_mod, "AUDIT_FIRST_DELAY", 0)
    monkeypatch.setattr(cog_mod, "AUDIT_RETRY_DELAY", 0)

    async def audit_logs(action=None, limit=None):
        if False:
            yield None

    guild = SimpleNamespace(id=GUILD_ID, audit_logs=audit_logs)
    cog = cog_mod.UserCache(SimpleNamespace(user=SimpleNamespace(id=BOT_ID), guilds=[]), store,
                            SimpleNamespace(get_actions=AsyncMock(return_value=[])))
    store.departed(1, "left")
    await cog._attribute(guild, 1, "kicked")
    assert (await store.get(1))["status"] == "left"


# ---------------------------------------------------------------------------
# Internal API: resolve fallback + forget
# ---------------------------------------------------------------------------

def _not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")


class FakeGuild:
    def __init__(self, members):
        self.id = GUILD_ID
        self._members = {m.id: m for m in members}

    def get_member(self, uid):
        return self._members.get(uid)

    async def query_members(self, user_ids=None, limit=5):
        return []

    async def fetch_member(self, uid):
        if uid == 4040:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
        raise _not_found()

    def get_role(self, rid):
        return None

    def get_channel(self, cid):
        return None


@pytest.fixture
async def api(store):
    from internal_api.app import create_app

    present = member(1, "present")
    guild = FakeGuild([present])

    async def fetch_user(uid):
        if uid == 3:
            return user(3, "fetched", avatar="f")
        raise _not_found()

    bot = SimpleNamespace(get_guild=lambda gid: guild if gid == GUILD_ID else None, fetch_user=AsyncMock(side_effect=fetch_user),
                          db=None)
    app = create_app()
    app.state.bot = bot
    ucs.set_active(store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AUTH) as c:
        c.bot = bot  # type: ignore[attr-defined]
        yield c
    ucs.set_active(None)


async def test_resolve_member_shape_unchanged(api, store):
    r = await api.post(f"/guilds/{GUILD_ID}/resolve", json={"user_ids": [1]})
    assert r.status_code == 200
    assert set(r.json()["users"]["1"]) == {"id", "username", "display_name", "avatar_url"}


async def test_resolve_departed_from_cache(api, store):
    store.upsert_member(member(2, "gone", nick="Gone"))
    store.departed(2, "banned")
    store.attribute(2, "banned", MOD, "moderator")
    await store.flush()
    r = await api.post(f"/guilds/{GUILD_ID}/resolve", json={"user_ids": [2]})
    u = r.json()["users"]["2"]
    assert u["departed"] is True and u["status"] == "banned" and u["display_name"] == "Gone"
    assert u["departed_by_id"] == str(MOD) and u["departed_at"]
    api.bot.fetch_user.assert_not_called()  # fresh cache row: no refresh


async def test_resolve_unknown_uses_fetch_user_and_caches(api, store):
    r = await api.post(f"/guilds/{GUILD_ID}/resolve", json={"user_ids": [3, 99]})
    users = r.json()["users"]
    assert users["3"]["username"] == "fetched" and users["3"]["status"] == "left" and users["3"]["departed_at"] is None
    assert users["99"] is None
    assert (await store.get(3))["username"] == "fetched"


async def test_resolve_transient_still_omitted(api):
    r = await api.post(f"/guilds/{GUILD_ID}/resolve", json={"user_ids": [4040]})
    assert "4040" not in r.json()["users"]


async def test_resolve_without_cache_returns_null(api):
    ucs.set_active(None)
    r = await api.post(f"/guilds/{GUILD_ID}/resolve", json={"user_ids": [99]})
    assert r.json()["users"]["99"] is None


async def test_internal_get_and_forget(api, store):
    store.departed(7, "left")
    store.touch_profile(user(7, "seven"))
    await store.flush()
    r = await api.get(f"/guilds/{GUILD_ID}/user-cache/7")
    assert r.status_code == 200 and r.json()["user"]["username"] == "seven" and r.json()["in_guild"] is False
    r = await api.delete(f"/guilds/{GUILD_ID}/user-cache/7", params={"actor_id": MOD})
    assert r.json() == {"ok": True, "removed": True, "in_guild": False}
    assert (await api.get(f"/guilds/{GUILD_ID}/user-cache/7")).status_code == 404
    assert (await api.delete(f"/guilds/{GUILD_ID}/user-cache/7")).status_code == 422  # actor_id required


async def test_internal_user_cache_off_is_503(api):
    ucs.set_active(None)
    r = await api.get(f"/guilds/{GUILD_ID}/user-cache/7")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "user_cache_unavailable"


# ---------------------------------------------------------------------------
# Dashboard: resolve gate (any bot permission) + user-cache proxies
# ---------------------------------------------------------------------------

from server import config as server_config  # noqa: E402
from server.app import app as dash_app  # noqa: E402
from server.auth.dependencies import get_current_session, require_session  # noqa: E402
from server.db.models import Session  # noqa: E402
from server.services import internal_client  # noqa: E402
from server.services.internal_client import InternalAPIError  # noqa: E402

USER = 123456789012345678


@pytest.fixture
def dash(monkeypatch):
    state = {"grants": set(), "staff": False, "error": None, "forgot": None}
    sess = Session()
    sess.session_id = "sid"
    sess.discord_user_id = USER
    sess.discord_username = "u"
    sess.discord_avatar = None
    sess.is_guild_member = True
    sess.cached_role_ids = []

    async def check_permissions_bulk(guild_id, user_id, perms):
        if state["error"]:
            raise state["error"]
        return {p: state["staff"] or p in state["grants"] for p in perms}

    async def check_is_staff(guild_id, user_id):
        return state["staff"]

    async def resolve_ids(guild_id, u, r, c):
        return {"users": {}, "roles": {}, "channels": {}}

    async def forget_cached_user(guild_id, user_id, actor_id):
        state["forgot"] = (user_id, actor_id)
        return {"ok": True, "removed": True, "in_guild": False}

    monkeypatch.setattr(internal_client, "check_permissions_bulk", check_permissions_bulk)
    monkeypatch.setattr(internal_client, "check_is_staff", check_is_staff)
    monkeypatch.setattr(internal_client, "resolve_ids", resolve_ids)
    monkeypatch.setattr(internal_client, "forget_cached_user", forget_cached_user)
    dash_app.dependency_overrides[get_current_session] = lambda: sess
    dash_app.dependency_overrides[require_session] = lambda: sess
    c = TestClient(dash_app, headers={"Origin": server_config.FRONTEND_BASE_URL})
    c.state = state  # type: ignore[attr-defined]
    yield c
    dash_app.dependency_overrides.clear()


def test_resolve_allowed_with_only_a_feature_permission(dash):
    dash.state["grants"] = {"moderate_members"}
    assert dash.post("/api/resolve", json={"user_ids": ["1"]}).status_code == 200
    dash.state["grants"] = {"manage_tickets"}
    assert dash.post("/api/resolve", json={"user_ids": ["1"]}).status_code == 200


def test_resolve_forbidden_without_any_permission(dash):
    r = dash.post("/api/resolve", json={"user_ids": ["1"]})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "forbidden"


def test_resolve_member_gone_and_bot_down(dash):
    dash.state["error"] = InternalAPIError("x", status_code=404, detail="Member 1 not found in guild 2")
    r = dash.post("/api/resolve", json={"user_ids": ["1"]})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "not_a_member"
    dash.state["error"] = InternalAPIError("down")
    assert dash.post("/api/resolve", json={"user_ids": ["1"]}).status_code == 502


def test_forget_is_staff_only_and_sends_session_actor(dash):
    dash.state["grants"] = {"moderate_members"}
    assert dash.delete("/api/user-cache/42").status_code == 403
    dash.state["staff"] = True
    r = dash.delete("/api/user-cache/42")
    assert r.status_code == 200 and dash.state["forgot"] == (42, USER)
    assert dash.delete("/api/user-cache/abc").status_code == 400
