"""Member hub (/hub): services/member_hub_service.py, internal_api/routes/member_hub.py,
server/routes/hub.py and the dashboard login's `next` parameter.

Fake discord objects and fake ids only; no network, no live data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import config
from services import member_hub_service as hub
from services.member_hub_service import HubError, MemberHubService, member_toggle
from services.reaction_role_service import ReactionRoleService

GUILD_ID = 100000000000000001
USER_ID = 123456789012345678
STAFF_ROLE = 900


def role(rid, pos, *, name=None, perms=None, managed=False):
    return NS(
        id=rid, position=pos, managed=managed, name=name or f"r{rid}",
        is_default=lambda: rid == GUILD_ID,
        permissions=perms or discord.Permissions.none(),
        colour=discord.Colour(0x112233 if rid != GUILD_ID else 0),
        color=discord.Colour(0x112233 if rid != GUILD_ID else 0),
    )


ROLES = {
    GUILD_ID: role(GUILD_ID, 0, name="@everyone"),
    100: role(100, 1, name="Red"),
    101: role(101, 2, name="Blue"),
    102: role(102, 3, name="Pings"),
    STAFF_ROLE: role(STAFF_ROLE, 4, name="Staff"),
    150: role(150, 20, name="Bot", managed=True),
}


class FakeGrants:
    def __init__(self, role_ids=()):
        self.role_ids = list(role_ids)

    async def list_grants(self, guild_id):
        return [{"role_id": r, "permission": "staff_base"} for r in self.role_ids]


class FakeRRRepo:
    def __init__(self):
        self.panels = {
            1: {"id": 1, "guild_id": GUILD_ID, "channel_id": 5, "message_id": 77, "title": "Colours", "exclusive": 1},
            2: {"id": 2, "guild_id": GUILD_ID, "channel_id": 5, "message_id": None, "title": "Draft", "exclusive": 0},
            3: {"id": 3, "guild_id": GUILD_ID, "channel_id": 6, "message_id": 78, "title": "Pings", "exclusive": 0},
        }
        self.entries = {
            1: [{"id": 1, "panel_id": 1, "emoji": "🔴", "role_id": 100, "label": None, "position": 0},
                {"id": 2, "panel_id": 1, "emoji": "🔵", "role_id": 101, "label": None, "position": 1}],
            2: [{"id": 3, "panel_id": 2, "emoji": "x", "role_id": 102, "label": None, "position": 0}],
            3: [{"id": 4, "panel_id": 3, "emoji": "<:p:1>", "role_id": 102, "label": "Pings", "position": 0},
                {"id": 5, "panel_id": 3, "emoji": "s", "role_id": STAFF_ROLE, "label": None, "position": 1}],
        }

    async def get_panel(self, pid):
        return self.panels.get(pid)

    async def get_entries(self, pid):
        return list(self.entries.get(pid, []))

    async def list_panels(self, guild_id):
        return [p for p in self.panels.values() if p["guild_id"] == guild_id]


def make_guild(members=()):
    g = NS(id=GUILD_ID, owner_id=1, name="Test Server", icon=None, member_count=3,
           premium_subscription_count=2, premium_tier=1, scheduled_events=[])
    g.roles = list(ROLES.values())
    g.get_role = ROLES.get
    g.me = NS(id=1, top_role=ROLES[150])
    g.members = list(members)
    g.get_member = lambda uid: next((m for m in g.members if m.id == uid), None)
    g.get_channel = lambda cid: NS(name=f"ticket-{cid}")
    g.fetch_ban = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown ban"))
    return g


def make_member(roles=(), **kw):
    rs = [ROLES[GUILD_ID], *roles]
    m = NS(
        id=USER_ID, name="member", display_name="Member", roles=rs, top_role=rs[-1],
        joined_at=datetime(2026, 1, 2, tzinfo=timezone.utc), created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        premium_since=None, timed_out_until=None, display_avatar=NS(url="https://cdn.example/a.png"),
        status=discord.Status.online,
    )
    m.__dict__.update(kw)
    m.add_roles = AsyncMock(side_effect=lambda *r, reason=None: m.roles.extend(r))
    m.remove_roles = AsyncMock(side_effect=lambda *r, reason=None: [m.roles.remove(x) for x in r])
    return m


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(config, "STAFF_ROLE_IDS", {STAFF_ROLE})
    monkeypatch.setattr(config, "OWNER_ID", 0)
    monkeypatch.setattr(config, "VERIFIED_ROLE_ID", 0)
    monkeypatch.setattr(config, "UNVERIFIED_ROLE_ID", 0)


def rr_service(grants=()):
    return ReactionRoleService(FakeRRRepo(), MagicMock(), FakeGrants(grants))


# ---------------------------------------------------------------------------
# self-role toggles: same rules as the panel buttons
# ---------------------------------------------------------------------------

async def test_toggle_grants_and_removes_a_safe_role():
    member = make_member()
    g = make_guild([member])
    res = await member_toggle(rr_service(), g, member, 3, 102, True)
    assert res == {"result": "added", "role_id": "102", "removed_role_ids": []}
    assert ROLES[102] in member.roles
    assert (await member_toggle(rr_service(), g, member, 3, 102, True))["result"] == "unchanged"
    res = await member_toggle(rr_service(), g, member, 3, 102, False)
    assert res["result"] == "removed" and ROLES[102] not in member.roles


async def test_toggle_exclusive_panel_drops_the_other_role():
    member = make_member([ROLES[100]])
    g = make_guild([member])
    res = await member_toggle(rr_service(), g, member, 1, 101, True)
    assert res["removed_role_ids"] == ["100"]
    assert ROLES[101] in member.roles and ROLES[100] not in member.roles


async def test_toggle_refuses_privileged_role_but_allows_dropping_it():
    member = make_member()
    g = make_guild([member])
    with pytest.raises(HubError) as exc:
        await member_toggle(rr_service(), g, member, 3, STAFF_ROLE, True)
    assert exc.value.code == "role_blocked" and exc.value.status == 403
    member.add_roles.assert_not_awaited()
    # A role that became privileged (now holds a grant) is refused to new takers,
    # but someone who already has it can still drop it.
    with pytest.raises(HubError):
        await member_toggle(rr_service(grants=[102]), g, member, 3, 102, True)
    holder = make_member([ROLES[102]])
    res = await member_toggle(rr_service(grants=[102]), g, holder, 3, 102, False)
    assert res["result"] == "removed"


@pytest.mark.parametrize(
    "panel_id,role_id,code",
    [(2, 102, "panel_not_found"),  # never posted: not a live panel
     (99, 102, "panel_not_found"),
     (1, 102, "role_not_on_panel")],
)
async def test_toggle_refuses_unposted_missing_or_foreign(panel_id, role_id, code):
    member = make_member()
    g = make_guild([member])
    with pytest.raises(HubError) as exc:
        await member_toggle(rr_service(), g, member, panel_id, role_id, True)
    assert exc.value.code == code
    member.add_roles.assert_not_awaited()


# ---------------------------------------------------------------------------
# the hub view
# ---------------------------------------------------------------------------

class FakeWarnings:
    async def get_warnings(self, guild_id, user_id):
        return [{"id": 7, "created_at": "2026-10-01 10:00:00", "reason": "secret staff note", "moderator_id": 42}]


async def test_member_view_has_own_data_and_never_warning_reasons():
    member = make_member([ROLES[100]])
    g = make_guild([member])
    svc = MemberHubService(warnings=FakeWarnings(), reaction_roles=rr_service())
    out = await svc.build(g, member, member)
    assert out["status"] == "member"
    assert out["user"]["id"] == str(USER_ID)
    assert out["server"] == {"id": str(GUILD_ID), "name": "Test Server", "icon_url": None, "member_count": 3,
                             "online_count": 1, "boost_count": 2, "boost_tier": 1}
    assert [r["name"] for r in out["profile"]["roles"]] == ["Red"]  # no @everyone
    assert out["standing"] == {"level": "warned", "warning_count": 1,
                               "warnings": [{"id": 7, "created_at": "2026-10-01 10:00:00"}], "timed_out_until": None}
    assert "secret staff note" not in repr(out) and "42" not in repr(out["standing"])
    # Only POSTED panels; the held role is marked.
    assert [p["panel_id"] for p in out["self_roles"]] == [1, 3]
    colours = out["self_roles"][0]["roles"]
    assert [(r["id"], r["has"]) for r in colours] == [("100", True), ("101", False)]
    # Features that aren't wired are listed, not fatal.
    assert {"birthday", "tickets", "qotd", "music"} <= set(out["unavailable"])


async def test_timeout_wins_over_warnings():
    until = datetime.now(timezone.utc) + timedelta(hours=1)
    member = make_member(timed_out_until=until)
    g = make_guild([member])
    out = await MemberHubService(warnings=FakeWarnings()).build(g, member, member)
    assert out["standing"]["level"] == "timed_out" and out["standing"]["timed_out_until"]


class FakeHoneypot:
    def __init__(self, data):
        self.data = data
        self.asked = []

    async def appeal_status_for_user(self, guild_id, user_id):
        self.asked.append(user_id)
        return self.data


async def test_outsider_removed_by_trap_sees_own_appeal_status():
    g = make_guild()
    honeypot = FakeHoneypot({"eligible": False, "appeal": {"status": "pending", "submitted_at": "x", "reviewed_at": None,
                                                           "staff_note": "never shown"}})
    out = await MemberHubService(honeypot=honeypot).build(g, NS(id=USER_ID, name="u", display_name="u"), None)
    assert out["status"] == "trap" and honeypot.asked == [USER_ID]
    assert out["trap"] == {"eligible": False, "appeal": {"status": "pending", "submitted_at": "x", "reviewed_at": None}}
    assert "profile" not in out and "self_roles" not in out


async def test_outsider_banned_and_not_member():
    g = make_guild()
    nobody = FakeHoneypot({"eligible": False, "appeal": None})
    out = await MemberHubService(honeypot=nobody).build(g, NS(id=USER_ID, name="u", display_name="u"), None)
    assert out["status"] == "not_member" and out["banned"] is False
    g.fetch_ban = AsyncMock(return_value=NS())
    out = await MemberHubService(honeypot=nobody).build(g, NS(id=USER_ID, name="u", display_name="u"), None)
    assert out["status"] == "banned" and out["banned"] is True


# ---------------------------------------------------------------------------
# dashboard backend: always the SESSION user
# ---------------------------------------------------------------------------

@pytest.fixture
def dash(monkeypatch):
    from fastapi.testclient import TestClient

    from server import config as sconfig
    from server.app import app
    from server.auth.dependencies import require_session
    from server.auth.oauth import limiter
    from server.db.models import Session
    from server.services import internal_client

    calls = []

    async def fake_session():
        s = Session()
        s.session_id = "s"
        s.discord_user_id = USER_ID
        s.discord_username = "member"
        s.is_guild_member = False  # the hub never trusts the login-time flag
        return s

    async def fake_hub(guild_id, user_id):
        calls.append(("hub", user_id))
        return {"status": "member"}

    async def fake_toggle(guild_id, user_id, panel_id, role_id, assign):
        calls.append(("toggle", user_id, panel_id, role_id, assign))
        if role_id == STAFF_ROLE:
            raise internal_client.InternalAPIError(
                "blocked", status_code=403, detail={"error": "role_blocked", "message": "no"},
            )
        return {"result": "added"}

    monkeypatch.setattr(internal_client, "get_member_hub", fake_hub)
    monkeypatch.setattr(internal_client, "toggle_hub_self_role", fake_toggle)
    limiter.reset()
    app.dependency_overrides[require_session] = fake_session
    client = TestClient(app, headers={"Origin": sconfig.FRONTEND_BASE_URL})
    client.calls = calls
    yield client
    app.dependency_overrides.pop(require_session, None)
    limiter.reset()


def test_hub_uses_session_user(dash):
    r = dash.get("/api/hub", params={"user_id": "1"})
    assert r.status_code == 200 and dash.calls == [("hub", USER_ID)]


def test_toggle_uses_session_user_and_passes_refusals(dash):
    r = dash.post("/api/hub/self-roles", json={"panel_id": 3, "role_id": "102", "assign": True, "user_id": "1"})
    assert r.status_code == 200 and dash.calls[-1] == ("toggle", USER_ID, 3, 102, True)
    r = dash.post("/api/hub/self-roles", json={"panel_id": 3, "role_id": str(STAFF_ROLE), "assign": True})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "role_blocked"


def test_toggle_is_rate_limited(dash):
    codes = [dash.post("/api/hub/self-roles", json={"panel_id": 3, "role_id": "102", "assign": True}).status_code
             for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


# ---------------------------------------------------------------------------
# login ?next=: only the allowlisted path, never an open redirect
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("/hub", "/hub"), ("https://evil.example", None), ("//evil.example", None), ("/hub/../x", None), (None, None),
])
def test_safe_next(value, expected):
    from server.auth.oauth import safe_next

    assert safe_next(value) == expected


def test_login_sets_next_cookie_only_for_allowed_paths():
    from fastapi.testclient import TestClient

    from server.app import app
    from server.auth.oauth import NEXT_COOKIE_NAME, limiter

    limiter.reset()
    client = TestClient(app, follow_redirects=False)
    r = client.get("/api/auth/login", params={"next": "/hub"})
    assert r.status_code == 302 and f"{NEXT_COOKIE_NAME}=hub;" in r.headers.get("set-cookie", "")
    r = client.get("/api/auth/login", params={"next": "https://evil.example"})
    assert "evil" not in r.headers.get("set-cookie", "")
    assert f'{NEXT_COOKIE_NAME}=""' in r.headers.get("set-cookie", "")  # a stale next is cleared
    limiter.reset()


def test_next_cookie_maps_back_to_the_allowlisted_path_only():
    from server.auth.oauth import _next_from_cookie

    assert _next_from_cookie("hub") == "/hub"
    assert _next_from_cookie("/hub") is None and _next_from_cookie("https://evil.example") is None
    assert _next_from_cookie(None) is None


# ---------------------------------------------------------------------------
# internal API wiring (no db, only the ReactionRoles cog loaded)
# ---------------------------------------------------------------------------

@pytest.fixture
async def internal():
    import httpx

    from internal_api.app import create_app

    member = make_member()
    g = make_guild([member])
    svc = rr_service()
    bot = NS(
        db=None, birthday_core=None, intents=NS(presences=True),
        get_guild=lambda gid: g if gid == GUILD_ID else None,
        get_cog=lambda name: NS(reaction_role_service=svc) if name == "ReactionRoles" else None,
        get_user=lambda uid: None,
        fetch_user=AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown user")),
    )
    g.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown member"))
    app = create_app()
    app.state.bot = bot
    auth = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=auth) as c:
        c.member = member
        yield c


async def test_internal_hub_route_member_and_outsider(internal):
    r = await internal.get(f"/guilds/{GUILD_ID}/hub/{USER_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "member" and [p["panel_id"] for p in body["self_roles"]] == [1, 3]
    assert {"standing", "birthday", "tickets", "qotd", "music"} <= set(body["unavailable"])

    r = await internal.get(f"/guilds/{GUILD_ID}/hub/100000000000000009")  # not in the guild
    assert r.status_code == 200 and r.json()["status"] == "not_member"
    assert "profile" not in r.json()


async def test_internal_toggle_route(internal):
    url = f"/guilds/{GUILD_ID}/hub/{USER_ID}/self-roles"
    r = await internal.post(url, json={"panel_id": 3, "role_id": 102, "assign": True})
    assert r.status_code == 200 and r.json()["result"] == "added"
    r = await internal.post(url, json={"panel_id": 3, "role_id": STAFF_ROLE, "assign": True})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "role_blocked"
    r = await internal.post(f"/guilds/{GUILD_ID}/hub/100000000000000009/self-roles",
                            json={"panel_id": 3, "role_id": 102, "assign": True})
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_a_member"
    r = await internal.post(url, json={"panel_id": 3, "role_id": 102, "assign": True}, headers={"Authorization": "Bearer x"})
    assert r.status_code in (401, 403)
