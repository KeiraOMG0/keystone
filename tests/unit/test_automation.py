"""Dashboard Automation page: the shared services (events, schedules,
autoroles, managed messages, template export/import), the internal API routes
(actor resolution + self-role policy on autoroles) and the dashboard routes
(per-tab permission gates, actor always the session user). Temp in-memory DB
and fake discord objects only; no network."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import config
from database.connection import Database
from services import live_events

GUILD_ID = 100000000000000001
OTHER_GUILD = 1278422221613830178
ACTOR = 222222222222222222
STAFF_ROLE = 900
GRANTED_ROLE = 901
CHANNEL = 333333333333333333
AUTH = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
_SQL = pathlib.Path(__file__).resolve().parents[2] / "database" / "migrations" / "sql"


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    for name in ("001_initial", "002_templates", "003_events", "004_managed", "006_advanced",
                 "017_welcome_boost", "021_event_bindings_audience"):
        await database.executescript((_SQL / f"{name}.sql").read_text(encoding="utf-8"))
    yield database
    await database.close()


class FakeAudit:
    def __init__(self):
        self.rows: list[tuple] = []

    async def log(self, guild_id, actor_id, action, target_type=None, target_id=None, detail=None):
        self.rows.append((guild_id, actor_id, action, target_type, target_id, detail))
        return len(self.rows)


@pytest.fixture
def published(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(live_events.hub, "publish", lambda topic, data, kind="event": seen.append((topic, data)))
    return seen


async def _template(db, name="hello", guild_id=GUILD_ID) -> int:
    from database.repositories.template_repo import TemplateRepository

    return await TemplateRepository(db).create(guild_id, name, ACTOR, content="hi {server}")


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------

async def test_event_binding_set_clear_audits_and_publishes(db, published):
    from core.event_bus import EventBus
    from core.renderer import TemplateRenderer
    from database.repositories.event_repo import EventRepository
    from database.repositories.template_repo import TemplateRepository
    from services.event_service import EventService

    await _template(db)
    audit = FakeAudit()
    svc = EventService(EventRepository(db), TemplateRepository(db), TemplateRenderer(), EventBus(), MagicMock(), audit=audit)
    assert await svc.set_binding(GUILD_ID, "nope", CHANNEL, "hello", ACTOR) != []
    assert "No template" in (await svc.set_binding(GUILD_ID, "member_join", CHANNEL, "missing", ACTOR))[0]
    assert await svc.set_binding(GUILD_ID, "member_join", CHANNEL, "hello", ACTOR, send_dm=True) == []
    bindings = await svc.list_bindings(GUILD_ID)
    assert bindings[0]["event_type"] == "member_join" and bindings[0]["templates"][0]["name"] == "hello"
    assert await svc.active_channel_id(GUILD_ID, "member_join") == CHANNEL
    assert await svc.clear_binding(GUILD_ID, "member_join", actor_id=ACTOR) is True
    assert await svc.clear_binding(GUILD_ID, "member_join", actor_id=ACTOR) is False
    assert [r[2] for r in audit.rows] == ["event_binding_set", "event_binding_clear"]
    assert all(r[1] == ACTOR for r in audit.rows)
    assert [(t, d["section"], d["action"]) for t, d in published] == [
        ("automation", "events", "set"), ("automation", "events", "clear"),
    ]


async def test_scheduler_validates_cron_scopes_toggle_and_lists_next_runs(db, published):
    from core.renderer import TemplateRenderer
    from database.repositories.advanced_repo import ScheduleRepository
    from database.repositories.template_repo import TemplateRepository
    from services.scheduler_service import SchedulerService, next_runs

    await _template(db)
    audit = FakeAudit()
    svc = SchedulerService(ScheduleRepository(db), TemplateRepository(db), TemplateRenderer(), MagicMock(), audit=audit)
    sid, errors = await svc.add(GUILD_ID, CHANNEL, "hello", "not a cron", ACTOR)
    assert sid is None and "Invalid cron" in errors[0]
    sid, errors = await svc.add(GUILD_ID, CHANNEL, "hello", "0 9 * * 1", ACTOR, label="x" * 101)
    assert sid is None and "Label" in errors[0]
    sid, errors = await svc.add(GUILD_ID, CHANNEL, "hello", "0 9 * * 1", ACTOR, label="weekly")
    assert errors == [] and sid
    assert await svc.toggle(OTHER_GUILD, sid, False, actor_id=ACTOR) is False  # other guild: not found
    assert await svc.toggle(GUILD_ID, sid, False, actor_id=ACTOR) is True
    assert (await svc.list_schedules(GUILD_ID))[0]["enabled"] == 0
    assert await svc.remove(GUILD_ID, sid, actor_id=ACTOR) is True
    assert [r[2] for r in audit.rows] == ["schedule_add", "schedule_toggle", "schedule_remove"]
    assert {d["section"] for _, d in published} == {"schedules"}

    runs = next_runs("0 9 * * 1", 3)
    assert len(runs) == 3 and all(r.weekday() == 0 and r.hour == 9 for r in runs) and runs[0] < runs[1] < runs[2]
    assert next_runs("61 * * * *") is None and next_runs("") is None


async def test_template_export_import_roundtrip(db):
    from database.repositories.audit_repo import AuditRepository
    from database.repositories.embed_repo import EmbedRepository
    from database.repositories.template_repo import TemplateRepository
    from database.repositories.version_repo import VersionRepository
    from services.template_service import TemplateService

    svc = TemplateService(TemplateRepository(db), EmbedRepository(db), VersionRepository(db), AuditRepository(db))
    tid, _ = await svc.create(GUILD_ID, "src", ACTOR, content="hello", category="c")
    await svc.replace_embeds(GUILD_ID, "src", ACTOR, [{"title": "T", "color": 5, "fields": [{"name": "a", "value": "b"}]}])
    export = await svc.export_template(GUILD_ID, "src")
    assert export["name"] == "src" and export["embeds"][0]["title"] == "T"
    assert "id" not in export["embeds"][0] and "template_id" not in export["embeds"][0]

    _, errors, _ = await svc.import_template(GUILD_ID, export, ACTOR)  # same name -> refused, never overwritten
    assert "already exists" in errors[0]
    new_id, errors, name = await svc.import_template(GUILD_ID, export, ACTOR, name_override="copy")
    assert errors == [] and name == "copy"
    copy = await svc.get_full(GUILD_ID, "copy")
    assert copy["content"] == "hello" and copy["embeds"][0]["fields"][0]["value"] == "b"
    assert (await svc.import_template(GUILD_ID, ["not", "a", "dict"], ACTOR))[1]
    assert await svc.export_template(GUILD_ID, "missing") is None


async def test_managed_untrack_is_guild_scoped_and_audited(db, published):
    from core.renderer import TemplateRenderer
    from database.repositories.managed_repo import ManagedMessageRepository
    from database.repositories.template_repo import TemplateRepository
    from services.managed_message_service import ManagedMessageService

    tid = await _template(db)
    repo = ManagedMessageRepository(db)
    await repo.create(GUILD_ID, CHANNEL, 444444444444444444, tid, ACTOR)
    audit = FakeAudit()
    svc = ManagedMessageService(repo, TemplateRepository(db), TemplateRenderer(), MagicMock(), audit=audit)
    assert await svc.untrack(OTHER_GUILD, 444444444444444444, actor_id=ACTOR) is False
    assert await svc.untrack(GUILD_ID, 444444444444444444, actor_id=ACTOR) is True
    assert audit.rows[0][2] == "managed_untrack" and audit.rows[0][4] == 444444444444444444
    assert published[-1][1] == {"section": "managed", "action": "untrack", "message_id": "444444444444444444"}


async def test_autorole_bounds_and_trigger_role_cleanup():
    from services.autorole_service import AutoroleService

    class Repo:
        def __init__(self):
            self.added = []

        async def add(self, **kw):
            self.added.append(kw)
            return 7

    g = NS(id=GUILD_ID, owner_id=ACTOR, me=None, get_role=lambda rid: NS(id=rid) if rid else None)
    repo = Repo()
    svc = AutoroleService(repo, MagicMock(), MagicMock(), audit=FakeAudit())
    # the policy itself is covered by test_selfrole_safety; stub it here
    import services.self_role_policy as policy

    orig = policy.configure_block_reason
    policy.configure_block_reason = AsyncMock(return_value=None)
    try:
        _, errors = await svc.add_rule(GUILD_ID, "join", 5, ACTOR, delay_seconds=-1, guild=g, actor=NS(id=ACTOR))
        assert "Delay" in errors[0]
        rule_id, errors = await svc.add_rule(GUILD_ID, "join", 5, ACTOR, trigger_role_id=9, guild=g, actor=NS(id=ACTOR))
        assert rule_id == 7 and repo.added[-1]["trigger_role_id"] is None  # join rules never keep a trigger role
    finally:
        policy.configure_block_reason = orig


# ---------------------------------------------------------------------------
# internal API (fake bot): autoroles go through self_role_policy with the actor
# ---------------------------------------------------------------------------

def _role(rid, pos, *, perms=None, managed=False, name=None):
    return NS(id=rid, position=pos, managed=managed, name=name or f"r{rid}", is_default=lambda: rid == GUILD_ID,
              permissions=perms or discord.Permissions.none(), color=discord.Colour(0))


class FakeGrants:
    async def list_grants(self, guild_id):
        return [{"role_id": GRANTED_ROLE, "permission": "staff_base"}]


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(config, "STAFF_ROLE_IDS", {STAFF_ROLE})
    monkeypatch.setattr(config, "OWNER_ID", 0)


@pytest_asyncio.fixture
async def api(db):
    from database.repositories.advanced_repo import AutoroleRepository
    from internal_api.app import create_app
    from services.autorole_service import AutoroleService

    roles = {r.id: r for r in (_role(GUILD_ID, 0), _role(100, 1, name="Colour"), _role(GRANTED_ROLE, 4, name="Helper"),
                               _role(103, 6, name="Mod"), _role(150, 20, name="Bot", managed=True))}
    g = NS(id=GUILD_ID, owner_id=1, roles=list(roles.values()), get_role=roles.get, text_channels=[],
           get_channel=lambda cid: None)
    g.me = NS(id=1, top_role=roles[150])
    members = {ACTOR: NS(id=ACTOR, top_role=roles[103], roles=[roles[103]])}
    g.get_member = members.get
    g.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "unknown member"))
    bot = MagicMock()
    bot.db = db
    bot.get_guild = lambda gid: g if gid == GUILD_ID else None
    bot.autorole_service = AutoroleService(AutoroleRepository(db), bot, FakeGrants(), audit=FakeAudit())
    bot.get_cog = lambda name: None
    app = create_app()
    app.state.bot = bot
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=AUTH) as c:
        c.bot = bot
        yield c


AUTOROLES = f"/guilds/{GUILD_ID}/automation/autoroles"


async def test_api_autorole_blocks_granted_role_for_actor(api):
    r = await api.post(AUTOROLES, json={"trigger": "join", "role_id": GRANTED_ROLE, "actor_id": ACTOR})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "role_not_allowed"
    r = await api.post(AUTOROLES, json={"trigger": "join", "role_id": 100, "actor_id": 4242})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "actor_not_found"
    r = await api.post(AUTOROLES, json={"trigger": "join", "role_id": 100, "actor_id": ACTOR})
    assert r.status_code == 200 and r.json()["rule_id"]
    body = (await api.get(AUTOROLES)).json()
    assert body["rules"][0]["role_id"] == "100" and body["rules"][0]["blocked_now"] is None
    rule_id = body["rules"][0]["id"]
    assert (await api.delete(f"{AUTOROLES}/{rule_id}", params={"actor_id": ACTOR})).status_code == 200
    assert (await api.delete(f"{AUTOROLES}/{rule_id}", params={"actor_id": ACTOR})).status_code == 404
    actions = [row[2] for row in api.bot.autorole_service._audit.rows]
    assert actions == ["autorole_add", "autorole_remove"]


async def test_api_autorole_picker_marks_blocked_roles(api):
    roles = {r["id"]: r for r in (await api.get(f"{AUTOROLES}/roles", params={"actor_id": ACTOR})).json()["roles"]}
    assert roles["100"]["blocked_reason"] is None
    assert roles[str(GRANTED_ROLE)]["blocked_reason"]
    assert str(GUILD_ID) not in roles


async def test_api_cron_preview_and_unloaded_cogs(api):
    r = await api.post(f"/guilds/{GUILD_ID}/automation/schedules/preview", json={"cron": "*/15 * * * *"})
    assert r.json()["valid"] is True and len(r.json()["next_runs"]) == 5
    r = await api.post(f"/guilds/{GUILD_ID}/automation/schedules/preview", json={"cron": "every monday"})
    assert r.json()["valid"] is False and r.json()["error"]
    api.bot.event_service = None
    r = await api.get(f"/guilds/{GUILD_ID}/automation/events")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "feature_unavailable"
    greet = (await api.get(f"/guilds/{GUILD_ID}/automation/greetings")).json()
    assert greet["welcome"]["available"] is False


# ---------------------------------------------------------------------------
# dashboard routes: per-tab permission, actor from the session
# ---------------------------------------------------------------------------

@pytest.fixture
def dash(monkeypatch):
    from server import config as sconfig
    from server.app import app
    from server.auth.dependencies import get_current_session, require_session
    from server.db.models import Session
    from server.services import internal_client

    state = {"perms": set(), "staff": False, "calls": []}

    async def check_permission(guild_id, user_id, perm):
        return perm in state["perms"]

    async def check_permissions_bulk(guild_id, user_id, perms):
        return {p: p in state["perms"] for p in perms}

    async def check_is_staff(guild_id, user_id):
        return state["staff"]

    def recorder(name, result):
        async def fn(*args, **kwargs):
            state["calls"].append((name, args, kwargs))
            return result
        return fn

    monkeypatch.setattr(internal_client, "check_permission", check_permission)
    monkeypatch.setattr(internal_client, "check_permissions_bulk", check_permissions_bulk)
    monkeypatch.setattr(internal_client, "check_is_staff", check_is_staff)
    for name in ("list_event_bindings", "set_event_binding", "test_event_binding", "get_greetings", "update_greeting",
                 "create_autorole", "list_schedules", "get_automation_options", "untrack_managed_message"):
        monkeypatch.setattr(internal_client, name, recorder(name, {"ok": True}))

    s = Session()
    s.session_id, s.discord_user_id, s.discord_username = "sid", ACTOR, "u"
    s.discord_avatar, s.is_guild_member, s.cached_role_ids = None, True, []
    app.dependency_overrides[get_current_session] = lambda: s
    app.dependency_overrides[require_session] = lambda: s
    c = TestClient(app)
    c.state = state  # type: ignore[attr-defined]
    c.headers.update({"Origin": sconfig.FRONTEND_BASE_URL})
    yield c
    app.dependency_overrides.clear()


def test_dashboard_tabs_use_the_command_permissions(dash):
    assert dash.get("/api/automation/events").status_code == 403
    dash.state["perms"] = {"manage_events"}
    assert dash.get("/api/automation/events").status_code == 200
    assert dash.get("/api/automation/schedules").status_code == 403  # manage_schedules
    assert dash.get("/api/automation/greetings").status_code == 403  # staff_base, like /welcome and /boost
    assert dash.get("/api/automation/options").status_code == 200  # any automation grant
    dash.state["perms"] = {"staff_base"}
    assert dash.get("/api/automation/greetings").status_code == 200  # same gate as the cogs (is_any_staff)
    assert dash.get("/api/automation/options").status_code == 200  # the tab needs the channel picker
    assert dash.get("/api/automation/events").status_code == 403


def test_dashboard_actor_is_the_session_user(dash):
    dash.state["perms"] = {"manage_events", "manage_autorole", "manage_templates"}
    r = dash.put("/api/automation/events/member_join", json={"channel_id": str(CHANNEL), "template_id": 1, "actor_id": 1})
    assert r.status_code == 200
    name, args, _ = dash.state["calls"][-1]
    assert name == "set_event_binding" and args[2]["actor_id"] == ACTOR and args[2]["channel_id"] == CHANNEL
    assert dash.post("/api/automation/events/member_join/test").status_code == 200
    assert dash.state["calls"][-1][1][2] == ACTOR
    assert dash.put("/api/automation/events/bogus", json={"channel_id": str(CHANNEL), "template_id": 1}).status_code == 400
    r = dash.post("/api/automation/autoroles", json={"trigger": "role_add", "role_id": "100100100100100100"})
    assert r.status_code == 400  # role_add needs a trigger role
    r = dash.post("/api/automation/autoroles", json={"trigger": "join", "role_id": "100100100100100100",
                                                      "trigger_role_id": "200200200200200200"})
    assert r.status_code == 200
    payload = dash.state["calls"][-1][1][1]
    assert payload["actor_id"] == ACTOR and payload["trigger_role_id"] is None
    assert dash.delete("/api/automation/managed/not-an-id").status_code == 400
