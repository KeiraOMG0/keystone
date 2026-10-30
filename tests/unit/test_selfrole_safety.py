"""Self-role escalation checks (services/self_role_policy.py) at configure time
(/reactionrole add, /autorole add, dashboard Self-roles via the internal API)
and at grant time (reaction-role button clicks, autorole application).
Fake discord objects only; no network."""

from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest

import config
from services import self_role_policy
from services.autorole_service import AutoroleService
from services.reaction_role_service import ReactionRoleService

pytestmark = pytest.mark.asyncio

GUILD_ID = 100000000000000001
OWNER = 11
MOD = 22
STAFF_ROLE = 900
GRANTED_ROLE = 901  # holds a bot permission grant (like 200000000000000002)
AUTH = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}


def role(rid, pos, *, perms=None, managed=False, guild_id=GUILD_ID, name=None):
    return NS(
        id=rid, position=pos, managed=managed, name=name or f"r{rid}",
        is_default=lambda: rid == guild_id,
        permissions=perms or discord.Permissions.none(),
        color=discord.Colour(0),
    )


class FakeGrants:
    def __init__(self, role_ids=(), fail=False):
        self.role_ids = list(role_ids)
        self.fail = fail

    async def list_grants(self, guild_id):
        if self.fail:
            raise RuntimeError("db down")
        return [{"role_id": r, "permission": "staff_base"} for r in self.role_ids]


def make_guild():
    roles = {
        GUILD_ID: role(GUILD_ID, 0),
        100: role(100, 1, name="Colour"),
        101: role(101, 2, name="Ping"),
        STAFF_ROLE: role(STAFF_ROLE, 3, name="Staff"),
        GRANTED_ROLE: role(GRANTED_ROLE, 4, name="Helper"),
        102: role(102, 5, name="Kicker", perms=discord.Permissions(kick_members=True)),
        103: role(103, 6, name="Mod"),
        150: role(150, 20, name="Bot", managed=True),
        104: role(104, 30, name="Above bot"),
    }
    g = NS(id=GUILD_ID, owner_id=OWNER)
    g.roles = list(roles.values())
    g.get_role = roles.get
    g.me = NS(id=1, top_role=roles[150])
    g.members = {
        MOD: NS(id=MOD, top_role=roles[103], roles=[roles[103]]),
        OWNER: NS(id=OWNER, top_role=roles[GUILD_ID], roles=[]),
    }
    g.get_member = g.members.get
    g.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown member"))
    return g


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(config, "STAFF_ROLE_IDS", {STAFF_ROLE})
    monkeypatch.setattr(config, "OWNER_ID", 0)


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

async def test_privileged_ids_are_staff_roles_plus_every_granted_role():
    ids = await self_role_policy.privileged_role_ids(FakeGrants([GRANTED_ROLE, 555]), GUILD_ID)
    assert ids == {STAFF_ROLE, GRANTED_ROLE, 555}


async def test_configure_rules():
    g, grants = make_guild(), FakeGrants([GRANTED_ROLE])
    mod = g.members[MOD]
    ok = await self_role_policy.configure_block_reason(g.get_role(100), g, mod, grants)
    assert ok is None
    assert "staff" in await self_role_policy.configure_block_reason(g.get_role(STAFF_ROLE), g, mod, grants)
    assert "staff" in await self_role_policy.configure_block_reason(g.get_role(GRANTED_ROLE), g, mod, grants)
    assert "kick_members" in await self_role_policy.configure_block_reason(g.get_role(102), g, mod, grants)
    assert "your top role" in await self_role_policy.configure_block_reason(g.get_role(103), g, mod, grants)
    assert "my top role" in await self_role_policy.configure_block_reason(g.get_role(104), g, mod, grants)
    assert "managed" in await self_role_policy.configure_block_reason(g.get_role(150), g, mod, grants)
    assert "@everyone" in await self_role_policy.configure_block_reason(g.get_role(GUILD_ID), g, mod, grants)
    assert "no longer exists" in await self_role_policy.configure_block_reason(None, g, mod, grants)
    assert await self_role_policy.configure_block_reason(g.get_role(100), g, None, grants)


async def test_guild_owner_skips_only_the_actor_rank_check():
    g, grants = make_guild(), FakeGrants([GRANTED_ROLE])
    owner = g.members[OWNER]  # top role = @everyone, but owns the guild
    assert await self_role_policy.configure_block_reason(g.get_role(103), g, owner, grants) is None
    assert await self_role_policy.configure_block_reason(g.get_role(GRANTED_ROLE), g, owner, grants)


async def test_fails_closed_when_grants_unreadable():
    g = make_guild()
    reason = await self_role_policy.configure_block_reason(g.get_role(100), g, g.members[MOD], FakeGrants(fail=True))
    assert reason == self_role_policy.GRANTS_UNAVAILABLE
    assert await self_role_policy.grant_block_reason(g.get_role(100), g, FakeGrants(fail=True))


# ---------------------------------------------------------------------------
# configure paths: ReactionRoleService.add_entry, AutoroleService.add_rule
# ---------------------------------------------------------------------------

class FakeRRRepo:
    def __init__(self):
        self.panels = {1: {"id": 1, "guild_id": GUILD_ID, "channel_id": 5, "message_id": 77, "title": "t", "exclusive": 0}}
        self.entries = {1: [{"id": 1, "panel_id": 1, "emoji": "a", "role_id": 100, "label": None, "position": 0}]}

    async def get_panel(self, pid):
        return self.panels.get(pid)

    async def get_entries(self, pid):
        return list(self.entries.get(pid, []))

    async def add_entry(self, pid, emoji, role_id, label, position=0):
        self.entries.setdefault(pid, []).append({"id": 99, "panel_id": pid, "emoji": emoji, "role_id": role_id,
                                                 "label": label, "position": position})
        return 99


async def test_reaction_role_add_entry_blocks_privileged_role():
    g, repo = make_guild(), FakeRRRepo()
    svc = ReactionRoleService(repo, MagicMock(), FakeGrants([GRANTED_ROLE]))
    svc.sync_panel_message = AsyncMock(return_value=[])
    entry_id, errors = await svc.add_entry(1, "b", GRANTED_ROLE, None, guild=g, actor=g.members[MOD])
    assert entry_id is None and "staff" in errors[0]
    assert len(repo.entries[1]) == 1
    entry_id, errors = await svc.add_entry(1, "b", 101, None, guild=g, actor=g.members[MOD])
    assert entry_id == 99 and errors == []
    # No guild / no actor = refused (fail closed).
    assert (await svc.add_entry(1, "c", 101, None))[1]
    assert (await svc.add_entry(1, "c", 101, None, guild=g, actor=None))[1]


class FakeAutoroleRepo:
    def __init__(self, rules=()):
        self.rules = list(rules)
        self.added = []

    async def add(self, **kw):
        self.added.append(kw)
        return 5

    async def list_for_guild(self, guild_id):
        return list(self.rules)


async def test_autorole_add_rule_blocks_privileged_role():
    g, repo = make_guild(), FakeAutoroleRepo()
    svc = AutoroleService(repo, MagicMock(), FakeGrants([GRANTED_ROLE]))
    rule_id, errors = await svc.add_rule(GUILD_ID, "join", GRANTED_ROLE, MOD, guild=g, actor=g.members[MOD])
    assert rule_id is None and "staff" in errors[0] and repo.added == []
    rule_id, errors = await svc.add_rule(GUILD_ID, "join", 100, MOD, guild=g, actor=g.members[MOD])
    assert rule_id == 5 and errors == []


# ---------------------------------------------------------------------------
# grant time: autorole application
# ---------------------------------------------------------------------------

def _member_for(g):
    m = NS(id=333, add_roles=AsyncMock())
    g.fetch_member = AsyncMock(return_value=m)
    return m


async def test_autorole_apply_rechecks_rule_and_role():
    g = make_guild()
    member = _member_for(g)
    rule = {"id": 1, "role_id": 100, "trigger_type": "join", "delay_seconds": 0, "enabled": 1}

    # Rule removed after it fired -> skipped.
    svc = AutoroleService(FakeAutoroleRepo([]), MagicMock(), FakeGrants())
    await svc._apply(g, member, rule)
    member.add_roles.assert_not_awaited()

    # Role now holds a grant -> refused.
    svc = AutoroleService(FakeAutoroleRepo([rule]), MagicMock(), FakeGrants([100]))
    await svc._apply(g, member, rule)
    member.add_roles.assert_not_awaited()

    # Still configured and safe -> granted.
    svc = AutoroleService(FakeAutoroleRepo([rule]), MagicMock(), FakeGrants())
    await svc._apply(g, member, rule)
    member.add_roles.assert_awaited_once()


# ---------------------------------------------------------------------------
# grant time: reaction-role button click
# ---------------------------------------------------------------------------

class FakeMember(discord.Member):
    """Passes isinstance(…, discord.Member) without a gateway."""

    def __init__(self, mid, roles):  # noqa: D401 - deliberately skips Member.__init__
        self._fake_id = mid
        self._fake_roles = roles
        self.add_roles = AsyncMock()
        self.remove_roles = AsyncMock()

    @property
    def id(self):
        return self._fake_id

    @property
    def roles(self):
        return self._fake_roles


def _interaction(g, member, panel_id, role_id, message_id=77):
    response = NS(send_message=AsyncMock(), defer=AsyncMock())
    return NS(
        type=discord.InteractionType.component,
        data={"custom_id": f"rr:{panel_id}:{role_id}"},
        guild=g, user=member, message=NS(id=message_id),
        response=response, followup=NS(send=AsyncMock()),
    )


def _cog(repo, grants):
    from cogs.reaction_roles import ReactionRoleCog

    return ReactionRoleCog(MagicMock(), repo, grants)


async def _click(cog, inter):
    await type(cog).on_interaction(cog, inter)


async def test_click_grants_configured_safe_role():
    g, repo = make_guild(), FakeRRRepo()
    member = FakeMember(333, [])
    inter = _interaction(g, member, 1, 100)
    await _click(_cog(repo, FakeGrants()), inter)
    member.add_roles.assert_awaited_once()
    inter.response.send_message.assert_not_awaited()


async def test_click_refuses_removed_button():
    g, repo = make_guild(), FakeRRRepo()
    member = FakeMember(333, [])
    inter = _interaction(g, member, 1, 101)  # 101 is not (or no longer) on panel 1
    await _click(_cog(repo, FakeGrants()), inter)
    member.add_roles.assert_not_awaited()
    assert "no longer active" in inter.response.send_message.await_args.args[0]
    assert inter.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_click_refuses_old_panel_message():
    g, repo = make_guild(), FakeRRRepo()
    member = FakeMember(333, [])
    inter = _interaction(g, member, 1, 100, message_id=12345)
    await _click(_cog(repo, FakeGrants()), inter)
    member.add_roles.assert_not_awaited()


async def test_click_refuses_role_that_became_privileged():
    g, repo = make_guild(), FakeRRRepo()
    member = FakeMember(333, [])
    inter = _interaction(g, member, 1, 100)
    await _click(_cog(repo, FakeGrants([100])), inter)
    member.add_roles.assert_not_awaited()
    assert "can't be self-assigned" in inter.response.send_message.await_args.args[0]


# ---------------------------------------------------------------------------
# internal API: POST entries requires an actor and returns 409 on a block
# ---------------------------------------------------------------------------

@pytest.fixture
async def api():
    from internal_api.app import create_app

    g, repo = make_guild(), FakeRRRepo()
    grants = FakeGrants([GRANTED_ROLE])
    bot = MagicMock()
    bot.get_guild = lambda gid: g if gid == GUILD_ID else None
    svc = ReactionRoleService(repo, bot, grants)
    svc.sync_panel_message = AsyncMock(return_value=[])
    bot.get_cog = lambda name: NS(reaction_role_service=svc) if name == "ReactionRoles" else None
    app = create_app()
    app.state.bot = bot
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=AUTH) as c:
        c.repo = repo
        yield c


ENTRIES = f"/guilds/{GUILD_ID}/reaction-roles/panels/1/entries"


async def test_api_add_entry_blocked_is_409_with_reason(api):
    r = await api.post(ENTRIES, json={"emoji": "b", "role_id": GRANTED_ROLE, "actor_id": MOD})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "role_not_allowed" and "staff" in r.json()["detail"]["message"]
    assert len(api.repo.entries[1]) == 1


async def test_api_add_entry_needs_actor(api):
    assert (await api.post(ENTRIES, json={"emoji": "b", "role_id": 100})).status_code == 422
    r = await api.post(ENTRIES, json={"emoji": "b", "role_id": 100, "actor_id": 4242})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "actor_not_found"


async def test_api_add_entry_allowed(api):
    r = await api.post(ENTRIES, json={"emoji": "b", "role_id": 101, "actor_id": MOD})
    assert r.status_code == 201 and r.json() == {"entry_id": 99}


async def test_api_role_picker_marks_blocked_roles(api):
    body = (await api.get(f"/guilds/{GUILD_ID}/reaction-roles/roles", params={"actor_id": MOD})).json()
    by_id = {r["id"]: r for r in body["roles"]}
    assert str(GUILD_ID) not in by_id
    assert by_id["100"]["blocked_reason"] is None
    assert "staff" in by_id[str(GRANTED_ROLE)]["blocked_reason"]
    assert "your top role" in by_id["103"]["blocked_reason"]
    assert body["roles"][0]["position"] >= body["roles"][-1]["position"]
