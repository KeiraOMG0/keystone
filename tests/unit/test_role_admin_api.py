"""Internal role-admin routes (internal_api/routes/role_admin.py) and
RoleAdminService against fake discord objects. No network. The dashboard proxy
is covered in test_role_admin_dashboard_routes.py."""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
import pytest_asyncio

import config
from internal_api.app import create_app
from services.role_admin_service import RoleAdminService

GUILD_ID = 100000000000000001
BOT_ID = 900000000000000001
ACTOR = 424242424242424242
AUTH = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
BASE = f"/guilds/{GUILD_ID}/role-admin/roles"


class FakeRole:
    def __init__(self, guild, rid, name, position, *, managed=False, perms=None, members=None):
        self.guild = guild
        self.id = rid
        self.name = name
        self.position = position
        self.managed = managed
        self.colour = discord.Colour(0)
        self.hoist = False
        self.mentionable = False
        self.permissions = perms or discord.Permissions.none()
        self.members = members or []
        self.tags = None
        self.icon = None
        self.unicode_emoji = None
        self.created_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        self.edit = AsyncMock(side_effect=self._edit)
        self.delete = AsyncMock(side_effect=lambda reason=None: guild.roles.remove(self))

    def is_default(self):
        return self.id == self.guild.id

    async def _edit(self, reason=None, **kw):
        if "name" in kw:
            self.name = kw["name"]
        if "colour" in kw:
            self.colour = discord.Colour(kw["colour"])
        if "permissions" in kw:
            self.permissions = kw["permissions"]
        for k in ("hoist", "mentionable"):
            if k in kw:
                setattr(self, k, kw[k])


class FakeMember:
    def __init__(self, uid, name, roles, perms=None):
        self.id = uid
        self.name = name
        self.display_name = name
        self.bot = False
        self.roles = roles
        self.guild_permissions = perms or discord.Permissions.none()
        self.display_avatar = SimpleNamespace(url="https://cdn/x.png")
        self.add_roles = AsyncMock(side_effect=lambda role, reason=None: self.roles.append(role))
        self.remove_roles = AsyncMock(side_effect=lambda role, reason=None: self.roles.remove(role))

    @property
    def top_role(self):
        return max(self.roles, key=lambda r: (r.position, r.id))


STAFF = 777000000000000007
STAFF_PERMS = discord.Permissions(manage_roles=True, send_messages=True, view_channel=True)


def make_guild(bot_perms: discord.Permissions):
    g = SimpleNamespace(id=GUILD_ID, features=[], premium_tier=0, owner_id=None)
    g.roles = []
    everyone = FakeRole(g, GUILD_ID, "@everyone", 0)
    low = FakeRole(g, 101, "Low", 1)
    mid = FakeRole(g, 102, "Mid", 2)
    bot_role = FakeRole(g, 103, "KickBot", 3, managed=True)
    admin = FakeRole(g, 104, "Admin", 4, perms=discord.Permissions(administrator=True))
    g.roles.extend([everyone, low, mid, bot_role, admin])
    g.me = SimpleNamespace(id=BOT_ID, guild_permissions=bot_perms, top_role=bot_role)
    member = FakeMember(555000000000000001, "alice", [everyone, low])
    # A non-owner staff member whose top role is Mid (position 2).
    staff = FakeMember(STAFF, "mod", [everyone, mid], perms=STAFF_PERMS)
    low.members = [member]
    mid.members = [staff]
    g.members = [member, staff]
    g.member_count = 2
    g.get_role = lambda rid: next((r for r in g.roles if r.id == rid), None)
    g.get_member = lambda uid: next((m for m in g.members if m.id == uid), None)
    g.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown member"))

    async def create_role(*, name, permissions, colour, hoist, mentionable, reason=None, **kw):
        r = FakeRole(g, 200 + len(g.roles), name, 1, perms=permissions)
        r.colour, r.hoist, r.mentionable = discord.Colour(colour), hoist, mentionable
        for other in g.roles:
            if other.position >= 1 and other is not r:
                other.position += 1
        g.roles.append(r)
        return r

    g.create_role = AsyncMock(side_effect=create_role)

    async def edit_role_positions(mapping, reason=None):
        for obj, pos in mapping.items():
            g.get_role(obj.id).position = pos
        return list(g.roles)

    g.edit_role_positions = AsyncMock(side_effect=edit_role_positions)
    g.fetch_roles = AsyncMock(side_effect=lambda: list(g.roles))
    g.member = member
    g.staff = staff
    return g


class FakeGrants:
    def __init__(self):
        self.rows: set[tuple[int, str]] = set()

    async def list_grants(self, guild_id):
        return [{"role_id": r, "permission": p} for r, p in sorted(self.rows)]

    async def grant(self, guild_id, role_id, permission, granted_by):
        from services.permission_service import VALID_PERMISSIONS

        if permission not in VALID_PERMISSIONS:
            return ["bad"]
        self.rows.add((role_id, permission))
        return []

    async def revoke(self, guild_id, role_id, permission, actor_id=None):
        if (role_id, permission) in self.rows:
            self.rows.remove((role_id, permission))
            return True
        return False


BOT_PERMS = discord.Permissions(manage_roles=True, send_messages=True, embed_links=True, kick_members=True, view_channel=True)


@pytest.fixture(autouse=True)
def _owner_is_actor(monkeypatch):
    # ACTOR is the configured owner (bypasses the actor hierarchy) and is not a
    # guild member; STAFF is an ordinary staff member held to the hierarchy.
    monkeypatch.setattr(config, "OWNER_ID", ACTOR)


@pytest_asyncio.fixture
async def client():
    guild = make_guild(BOT_PERMS)
    bot = MagicMock()
    bot.get_guild = lambda gid: guild if gid == GUILD_ID else None
    bot.db = None
    bot.permission_service = FakeGrants()
    app = create_app()
    app.state.bot = bot
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AUTH) as c:
        c.guild, c.grants = guild, bot.permission_service
        yield c


pytestmark = pytest.mark.asyncio


async def test_requires_secret(client):
    r = await client.get(BASE, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


async def test_overview_marks_hierarchy_and_bot_permissions(client):
    client.grants.rows.add((101, "manage_tickets"))
    body = (await client.get(BASE)).json()
    by_name = {r["name"]: r for r in body["roles"]}
    assert [r["name"] for r in body["roles"]] == ["Admin", "KickBot", "Mid", "Low", "@everyone"]
    assert by_name["Low"]["manageable"] and by_name["Mid"]["manageable"]
    assert not by_name["Admin"]["manageable"] and "above" in by_name["Admin"]["unmanageable_reason"]
    assert by_name["KickBot"]["is_bot_top_role"] and not by_name["KickBot"]["manageable"]
    assert not by_name["@everyone"]["manageable"]
    assert by_name["Low"]["internal_permissions"] == ["manage_tickets"] and by_name["Low"]["member_count"] == 1
    assert by_name["Mid"]["member_count"] == 1 and by_name["@everyone"]["member_count"] == 2
    assert body["actor"] is None
    assert by_name["Low"]["id"] == "101"
    assert body["bot"]["top_role_id"] == "103" and body["bot"]["top_position"] == 3
    assert "administrator" not in body["bot"]["permissions"] and "kick_members" in body["bot"]["permissions"]
    assert body["guild"]["role_icons"] is False
    assert "manage_tickets" in body["valid_internal_permissions"]


async def test_create_role_with_position_and_internal_grants(client):
    r = await client.post(
        BASE,
        json={
            "actor_id": ACTOR,
            "actor_name": "keira",
            "name": "Helpers",
            "color": "#ff0000",
            "permissions": ["send_messages", "kick_members"],
            "position": 3,
            "internal_permissions": ["manage_tickets"],
        },
    )
    assert r.status_code == 201, r.text
    role = r.json()["role"]
    assert role["name"] == "Helpers" and role["color"] == "#ff0000"
    assert role["internal_permissions"] == ["manage_tickets"]
    kwargs = client.guild.create_role.await_args.kwargs
    assert kwargs["reason"].startswith("Dashboard: keira")
    client.guild.edit_role_positions.assert_awaited()
    created = client.guild.get_role(int(role["id"]))
    assert created.position == 3 and client.guild.me.top_role.position == 4


async def test_create_refuses_permissions_the_bot_lacks(client):
    r = await client.post(BASE, json={"actor_id": ACTOR, "name": "X", "permissions": ["ban_members"]})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "permission_not_held"
    r = await client.post(BASE, json={"actor_id": ACTOR, "name": "X", "permissions": ["administrator"], "confirm_administrator": True})
    assert r.status_code == 403
    client.guild.create_role.assert_not_awaited()


async def test_edit_above_bot_refused_but_below_ok(client):
    r = await client.patch(f"{BASE}/104", json={"actor_id": ACTOR, "name": "Nope"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "role_not_manageable"
    r = await client.patch(f"{BASE}/101", json={"actor_id": ACTOR, "name": "Lower", "permissions": ["send_messages"]})
    assert r.status_code == 200 and r.json()["role"]["name"] == "Lower"
    assert r.json()["role"]["permissions"] == ["send_messages"]


async def test_internal_grants_editable_on_non_manageable_but_not_managed(client):
    r = await client.patch(f"{BASE}/104", json={"actor_id": ACTOR, "internal_permissions": ["view_audit"]})
    assert r.status_code == 200 and r.json()["role"]["internal_permissions"] == ["view_audit"]
    r = await client.patch(f"{BASE}/103", json={"actor_id": ACTOR, "internal_permissions": ["view_audit"]})
    assert r.status_code == 409


async def test_move_clamps_below_bot(client):
    r = await client.post(f"{BASE}/101/move", json={"actor_id": ACTOR, "position": 99})
    assert r.status_code == 200
    assert client.guild.get_role(101).position == 2 and client.guild.get_role(102).position == 1
    r = await client.post(f"{BASE}/104/move", json={"actor_id": ACTOR, "position": 1})
    assert r.status_code == 409


async def test_delete_requires_exact_name_and_cleans_grants(client):
    client.grants.rows.add((102, "staff_base"))
    r = await client.request("DELETE", f"{BASE}/102", json={"actor_id": ACTOR, "confirm_name": "mid"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "confirm_mismatch"
    r = await client.request("DELETE", f"{BASE}/102", json={"actor_id": ACTOR, "confirm_name": "Mid"})
    assert r.status_code == 200 and client.guild.get_role(102) is None and not client.grants.rows
    r = await client.request("DELETE", f"{BASE}/{GUILD_ID}", json={"actor_id": ACTOR, "confirm_name": "@everyone"})
    assert r.status_code == 409


async def test_duplicate_drops_unheld_permissions(client):
    client.guild.get_role(102).permissions = discord.Permissions(send_messages=True, ban_members=True)
    client.grants.rows.add((102, "staff_base"))
    r = await client.post(f"{BASE}/102/duplicate", json={"actor_id": ACTOR})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"]["name"] == "Mid (copy)" and body["role"]["permissions"] == ["send_messages"]
    assert body["dropped_permissions"] == ["ban_members"] and body["role"]["internal_permissions"] == ["staff_base"]


async def test_members_list_add_remove(client):
    r = await client.get(f"{BASE}/101/members", params={"limit": 10})
    assert r.json()["total"] == 1 and r.json()["members"][0]["user_id"] == "555000000000000001"
    m = client.guild.member
    r = await client.post(f"{BASE}/102/members/{m.id}", json={"actor_id": ACTOR, "actor_name": "keira"})
    assert r.status_code == 200 and r.json()["changed"] is True
    assert m.add_roles.await_args.kwargs["reason"].startswith("Dashboard: keira")
    r = await client.request("DELETE", f"{BASE}/102/members/{m.id}", json={"actor_id": ACTOR})
    assert r.json()["changed"] is True
    r = await client.post(f"{BASE}/102/members/1", json={"actor_id": ACTOR})
    assert r.status_code == 404
    r = await client.post(f"{BASE}/104/members/{m.id}", json={"actor_id": ACTOR})
    assert r.status_code == 409


async def test_discord_forbidden_maps_to_clean_error(client):
    client.guild.get_role(101).edit = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Missing Permissions"))
    r = await client.patch(f"{BASE}/101", json={"actor_id": ACTOR, "name": "x"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "discord_forbidden"


async def test_audit_rows_written_for_mutations():
    guild = make_guild(BOT_PERMS)
    bot = SimpleNamespace(permission_service=FakeGrants())
    audit = SimpleNamespace(log=AsyncMock())
    svc = RoleAdminService(bot, guild, audit)
    from services.role_admin_service import Actor

    await svc.update_role(Actor(ACTOR, "keira"), 101, fields={"name": "Renamed"}, internal_permissions=None, confirm_administrator=False)
    args = audit.log.await_args.args
    assert args[0] == GUILD_ID and args[1] == ACTOR and args[2] == "role_update" and args[3] == "role" and args[4] == 101
    assert args[5]["changes"]["name"] == ["Low", "Renamed"] and args[5]["via"] == "dashboard"


async def test_create_above_role_lands_directly_above_it(client):
    r = await client.post(BASE, json={"actor_id": ACTOR, "name": "Between", "above_role_id": 101})
    assert r.status_code == 201, r.text
    g = client.guild
    order = [x.name for x in sorted(g.roles, key=lambda x: x.position)]
    assert order.index("Between") == order.index("Low") + 1 and order.index("Mid") == order.index("Between") + 1
    assert g.me.top_role.position > g.get_role(102).position


# --- acting-user hierarchy (non-owner staff) ---------------------------------


async def test_overview_applies_actor_hierarchy(client):
    body = (await client.get(BASE, params={"actor_id": STAFF})).json()
    by_name = {r["name"]: r for r in body["roles"]}
    assert by_name["Low"]["manageable"]
    assert not by_name["Mid"]["manageable"] and "your highest role" in by_name["Mid"]["unmanageable_reason"]
    actor = body["actor"]
    assert actor["user_id"] == str(STAFF) and actor["is_owner"] is False and actor["top_position"] == 2
    assert actor["top_role_id"] == "102"
    assert "send_messages" in actor["permissions"] and "kick_members" not in actor["permissions"]
    owner = (await client.get(BASE, params={"actor_id": ACTOR})).json()
    assert owner["actor"]["is_owner"] is True and {r["name"]: r for r in owner["roles"]}["Mid"]["manageable"]


async def test_staff_cannot_touch_roles_at_or_above_their_top(client):
    r = await client.patch(f"{BASE}/102", json={"actor_id": STAFF, "name": "Mine"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "actor_hierarchy"
    r = await client.post(f"{BASE}/102/move", json={"actor_id": STAFF, "position": 1})
    assert r.status_code == 403
    r = await client.request("DELETE", f"{BASE}/102", json={"actor_id": STAFF, "confirm_name": "Mid"})
    assert r.status_code == 403 and client.guild.get_role(102) is not None
    m = client.guild.member
    r = await client.post(f"{BASE}/102/members/{m.id}", json={"actor_id": STAFF})
    assert r.status_code == 403
    r = await client.request("DELETE", f"{BASE}/102/members/{STAFF}", json={"actor_id": STAFF})
    assert r.status_code == 403
    r = await client.patch(f"{BASE}/101", json={"actor_id": STAFF, "name": "Lower"})
    assert r.status_code == 200 and r.json()["role"]["name"] == "Lower"


async def test_staff_can_only_grant_permissions_they_hold(client):
    # The bot holds kick_members; the staff member does not.
    r = await client.patch(f"{BASE}/101", json={"actor_id": STAFF, "permissions": ["kick_members"]})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "actor_permission_not_held"
    r = await client.post(BASE, json={"actor_id": STAFF, "name": "X", "permissions": ["kick_members"]})
    assert r.status_code == 403
    client.guild.create_role.assert_not_awaited()
    r = await client.patch(f"{BASE}/101", json={"actor_id": STAFF, "permissions": ["send_messages"]})
    assert r.status_code == 200


async def test_staff_moves_are_clamped_below_their_top_role(client):
    r = await client.post(f"{BASE}/101/move", json={"actor_id": STAFF, "position": 99})
    assert r.status_code == 200
    g = client.guild
    assert g.get_role(101).position == 1 and g.get_role(102).position == 2
    g.edit_role_positions.assert_not_awaited()
    r = await client.post(BASE, json={"actor_id": STAFF, "name": "New", "position": 250})
    assert r.status_code == 201, r.text
    new = g.get_role(int(r.json()["role"]["id"]))
    assert new.position == g.get_role(102).position - 1
    r = await client.post(BASE, json={"actor_id": STAFF, "name": "Above", "above_role_id": 102})
    assert r.status_code == 201, r.text
    above = g.get_role(int(r.json()["role"]["id"]))
    assert above.position < g.get_role(102).position


async def test_staff_cannot_self_assign_or_hand_out_stronger_roles(client):
    r = await client.post(f"{BASE}/101/members/{STAFF}", json={"actor_id": STAFF})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "self_assign_forbidden"
    m = client.guild.member
    client.guild.get_role(101).permissions = discord.Permissions(kick_members=True)
    r = await client.post(f"{BASE}/101/members/{m.id}", json={"actor_id": STAFF})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "actor_permission_not_held"
    client.guild.get_role(101).permissions = discord.Permissions(send_messages=True)
    r = await client.request("DELETE", f"{BASE}/101/members/{m.id}", json={"actor_id": STAFF})
    assert r.status_code == 200 and r.json()["changed"] is True
    r = await client.post(f"{BASE}/101/members/{m.id}", json={"actor_id": STAFF})
    assert r.status_code == 200 and r.json()["changed"] is True


async def test_non_member_non_owner_is_refused(client):
    r = await client.patch(f"{BASE}/101", json={"actor_id": 123456789012345678, "name": "x"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "actor_not_member"


async def test_duplicate_route_passes_administrator_confirm(client):
    client.guild.me.guild_permissions = discord.Permissions.all()
    client.guild.get_role(102).permissions = discord.Permissions(administrator=True)
    r = await client.post(f"{BASE}/102/duplicate", json={"actor_id": ACTOR})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "administrator_needs_confirm"
    client.guild.create_role.assert_not_awaited()
    r = await client.post(f"{BASE}/102/duplicate", json={"actor_id": ACTOR, "confirm_administrator": True})
    assert r.status_code == 201 and "administrator" in r.json()["role"]["permissions"]


async def test_duplicate_administrator_is_owner_only():
    # discord.py expands an Administrator member to every flag; mirror that here.
    guild = make_guild(discord.Permissions.all())
    bot = SimpleNamespace(permission_service=FakeGrants())
    svc = RoleAdminService(bot, guild, None)
    from services.role_admin_service import Actor

    # A non-owner, even one whose Discord permissions include Administrator,
    # never copies Administrator: it is dropped and reported.
    guild.staff.guild_permissions = discord.Permissions.all()
    guild.get_role(101).permissions = discord.Permissions(administrator=True, send_messages=True)
    res = await svc.duplicate_role(Actor(STAFF, "mod"), 101, "Low copy", confirm_administrator=True)
    assert res["role"]["permissions"] == ["send_messages"] and res["dropped_permissions"] == ["administrator"]
