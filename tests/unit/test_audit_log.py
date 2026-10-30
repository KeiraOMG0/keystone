"""Audit log: repository filters + keyset pagination, row serialisation, the live
event, retention, permission grant/revoke audit rows, and the internal + dashboard
routes (param validation, error mapping). In-memory SQLite, no network."""
from __future__ import annotations

import datetime
import json
import pathlib
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio

import config
from database.connection import Database
from database.repositories.advanced_repo import PermissionRepository
from database.repositories.audit_repo import AuditRepository
from services import audit_service as audit_mod
from services.audit_service import AuditService, serialize_row
from services.permission_service import PermissionService

SQL_DIR = pathlib.Path(__file__).resolve().parents[2] / "database" / "migrations" / "sql"
GUILD = 100000000000000001
OTHER_GUILD = 1278422221613830178
ACTOR = 518624171970789388
ROLE = 987654321098765432
AUTH = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}


@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    for name in ("002_templates.sql", "006_advanced.sql"):
        await database.executescript((SQL_DIR / name).read_text(encoding="utf-8"))
    yield database
    await database.close()


@pytest.fixture(autouse=True)
def no_live(monkeypatch):
    events = []
    monkeypatch.setattr(audit_mod, "live_publish", lambda topic, data: events.append((topic, data)))
    monkeypatch.setattr(audit_mod, "_last_prune", None)
    return events


async def _seed(repo: AuditRepository) -> list[int]:
    ids = []
    rows = [
        (GUILD, ACTOR, "role_added", "role", ROLE, {"member_id": ACTOR}, "2026-10-01 10:00:00"),
        (GUILD, 1, "member_joined", "member", ACTOR, None, "2026-10-02 10:00:00"),
        (GUILD, ACTOR, "role_added", "role", 5, None, "2026-10-03 10:00:00"),
        (OTHER_GUILD, ACTOR, "role_added", "role", ROLE, None, "2026-10-03 11:00:00"),
        (GUILD, ACTOR, "qotd_approved", None, None, {"question": "q?"}, "2026-10-04 10:00:00"),
    ]
    for g, a, action, tt, tid, detail, ts in rows:
        ids.append(await repo.log(g, a, action, tt, tid, detail, created_at=ts))
    return ids


# -- repository ---------------------------------------------------------------

async def test_repo_filters_and_order(db):
    repo = AuditRepository(db)
    ids = await _seed(repo)
    rows = await repo.query(GUILD)
    assert [r["id"] for r in rows] == [ids[4], ids[2], ids[1], ids[0]]  # newest first, guild-scoped
    assert [r["id"] for r in await repo.query(GUILD, action="role_added")] == [ids[2], ids[0]]
    assert [r["id"] for r in await repo.query(GUILD, actor_id=1)] == [ids[1]]
    assert [r["id"] for r in await repo.query(GUILD, target_id=ROLE)] == [ids[0]]
    assert [r["id"] for r in await repo.query(GUILD, target_type="member")] == [ids[1]]
    since = datetime.datetime(2026, 10, 2, 10, 0, tzinfo=datetime.timezone.utc)
    until = datetime.datetime(2026, 10, 4, 10, 0, tzinfo=datetime.timezone.utc)
    assert [r["id"] for r in await repo.query(GUILD, since=since, until=until)] == [ids[2], ids[1]]
    # A non-UTC aware datetime is converted before comparing.
    plus2 = datetime.timezone(datetime.timedelta(hours=2))
    assert [r["id"] for r in await repo.query(GUILD, since=datetime.datetime(2026, 10, 4, 12, 0, tzinfo=plus2))] == [ids[4]]


async def test_repo_keyset_pagination_and_actions(db):
    repo = AuditRepository(db)
    ids = await _seed(repo)
    page1 = await repo.query(GUILD, limit=2)
    page2 = await repo.query(GUILD, limit=2, before_id=page1[-1]["id"])
    page3 = await repo.query(GUILD, limit=2, before_id=page2[-1]["id"])
    assert [r["id"] for r in page1 + page2] == [ids[4], ids[2], ids[1], ids[0]] and page3 == []
    assert await repo.distinct_actions(GUILD) == [
        {"action": "role_added", "count": 2},
        {"action": "member_joined", "count": 1},
        {"action": "qotd_approved", "count": 1},
    ]


async def test_repo_default_timestamp_and_prune(db):
    repo = AuditRepository(db)
    await repo.log(GUILD, ACTOR, "old", created_at="2020-01-01 00:00:00")
    new_id = await repo.log(GUILD, ACTOR, "new")
    row = (await repo.query(GUILD, action="new"))[0]
    assert row["id"] == new_id and len(row["created_at"]) == 19 and row["created_at"][10] == " "
    removed = await repo.prune_older_than(datetime.datetime(2021, 1, 1, tzinfo=datetime.timezone.utc))
    assert removed == 1 and [r["action"] for r in await repo.query(GUILD)] == ["new"]


# -- service ------------------------------------------------------------------

def test_serialize_row_strings_ids_and_parses_detail():
    out = serialize_row({
        "id": 7, "guild_id": GUILD, "actor_id": ACTOR, "action": "x", "target_type": "role", "target_id": 12,
        "detail_json": json.dumps({"member_id": ACTOR, "n": 3, "nested": [ACTOR, True]}),
        "created_at": "2026-10-14 20:06:41",
    })
    assert out["id"] == 7 and out["guild_id"] == str(GUILD) and out["actor_id"] == str(ACTOR)
    assert out["target_id"] == "12" and out["created_at"] == "2026-10-14T20:06:41Z"
    assert out["detail"] == {"member_id": str(ACTOR), "n": 3, "nested": [str(ACTOR), True]}
    assert out["detail_raw"] is None
    bad = serialize_row({"id": 1, "actor_id": 0, "action": "x", "detail_json": "{not json", "created_at": None})
    assert bad["detail"] is None and bad["detail_raw"] == "{not json" and bad["target_id"] is None


async def test_service_query_pages_and_live_event(db, no_live):
    svc = AuditService(AuditRepository(db))
    ids = await _seed(AuditRepository(db))
    page = await svc.query(GUILD, limit=3)
    assert [e["id"] for e in page["entries"]] == [ids[4], ids[2], ids[1]] and page["next_before_id"] == ids[1]
    last = await svc.query(GUILD, limit=3, before_id=page["next_before_id"])
    assert [e["id"] for e in last["entries"]] == [ids[0]] and last["next_before_id"] is None

    row_id = await svc.log(GUILD, ACTOR, "permission_grant", "role", ROLE, {"permission": "view_audit"})
    topic, data = no_live[-1]
    assert topic == "audit" and data["kind"] == "audit"
    live = data["row"]
    assert live["id"] == row_id and live["actor_id"] == str(ACTOR) and live["target_id"] == str(ROLE)
    assert live["detail"] == {"permission": "view_audit"} and live["created_at"].endswith("Z")
    # The live row is exactly what the list endpoint returns for it.
    assert (await svc.query(GUILD, limit=1))["entries"][0] == live


async def test_service_retention_runs_at_most_daily(db, monkeypatch):
    repo = AuditRepository(db)
    await repo.log(GUILD, ACTOR, "ancient", created_at="2000-01-01 00:00:00")
    svc = AuditService(repo)
    await svc.log(GUILD, ACTOR, "fresh")
    assert [r["action"] for r in await repo.query(GUILD)] == ["fresh"]
    await repo.log(GUILD, ACTOR, "ancient2", created_at="2000-01-01 00:00:00")
    await svc.log(GUILD, ACTOR, "fresh2")  # pruned less than a day ago: skipped
    assert "ancient2" in [r["action"] for r in await repo.query(GUILD)]
    monkeypatch.setattr(audit_mod, "AUDIT_RETENTION_DAYS", None)
    monkeypatch.setattr(audit_mod, "_last_prune", None)
    await svc.log(GUILD, ACTOR, "fresh3")
    assert "ancient2" in [r["action"] for r in await repo.query(GUILD)]


# -- permission grant / revoke audit rows --------------------------------------

async def test_permission_grant_and_revoke_write_audit_rows(db):
    audit_repo = AuditRepository(db)
    perms = PermissionService(PermissionRepository(db), audit=AuditService(audit_repo))
    assert await perms.grant(GUILD, ROLE, "view_audit", ACTOR) == []
    assert await perms.grant(GUILD, ROLE, "view_audit", 42) == []  # re-grant: flagged unchanged
    assert await perms.revoke(GUILD, ROLE, "view_audit", actor_id=ACTOR) is True
    assert await perms.revoke(GUILD, ROLE, "view_audit", actor_id=ACTOR) is False  # nothing to revoke: no row
    assert await perms.revoke(GUILD, ROLE, "staff_base") is False
    assert (await perms.grant(GUILD, ROLE, "nope", ACTOR))[0].startswith("Unknown permission")  # invalid: no row
    rows = list(reversed(await audit_repo.query(GUILD)))
    assert [(r["action"], r["actor_id"], r["target_type"], r["target_id"]) for r in rows] == [
        ("permission_grant", ACTOR, "role", ROLE),
        ("permission_grant", 42, "role", ROLE),
        ("permission_revoke", ACTOR, "role", ROLE),
    ]
    assert [json.loads(r["detail_json"]) for r in rows] == [
        {"permission": "view_audit"},
        {"permission": "view_audit", "unchanged": True},
        {"permission": "view_audit"},
    ]


async def test_permission_revoke_without_actor_records_zero(db):
    audit_repo = AuditRepository(db)
    perms = PermissionService(PermissionRepository(db), audit=AuditService(audit_repo))
    await perms.grant(GUILD, ROLE, "staff_base", ACTOR)
    assert await perms.revoke(GUILD, ROLE, "staff_base") is True
    assert (await audit_repo.query(GUILD, action="permission_revoke"))[0]["actor_id"] == 0


async def test_permission_change_survives_audit_failure(db):
    class Broken:
        async def log(self, *a, **k):
            raise RuntimeError("db locked")

    perms = PermissionService(PermissionRepository(db), audit=Broken())
    assert await perms.grant(GUILD, ROLE, "staff_base", ACTOR) == []
    assert await perms.revoke(GUILD, ROLE, "staff_base", actor_id=ACTOR) is True


# -- internal API ----------------------------------------------------------------

@pytest_asyncio.fixture
async def internal(db):
    from internal_api.app import create_app

    bot = MagicMock()
    bot.db = db
    guild = MagicMock()
    guild.get_member = lambda uid: MagicMock(display_name="Keira") if uid == ACTOR else None
    _role = MagicMock()
    _role.name = "Mods"
    guild.get_role = lambda rid: _role if rid == ROLE else None
    guild.get_channel = lambda cid: None
    bot.get_guild = lambda gid: guild if gid == GUILD else None
    bot.get_user = lambda uid: None
    bot.permission_service = PermissionService(PermissionRepository(db), audit=AuditService(AuditRepository(db)))
    app = create_app()
    app.state.bot = bot
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=AUTH) as c:
        yield c


async def test_internal_audit_route(internal, db):
    ids = await _seed(AuditRepository(db))
    base = f"/guilds/{GUILD}/audit"
    assert (await internal.get(base, headers={"Authorization": "Bearer nope"})).status_code == 403
    body = (await internal.get(base, params={"limit": 2})).json()
    assert [e["id"] for e in body["entries"]] == [ids[4], ids[2]] and body["next_before_id"] == ids[2]
    assert body["entries"][1]["actor_id"] == str(ACTOR) and body["entries"][1]["target_id"] == "5"
    body = (await internal.get(base, params={"before_id": body["next_before_id"], "action": "role_added"})).json()
    assert [e["id"] for e in body["entries"]] == [ids[0]] and body["next_before_id"] is None
    assert body["entries"][0]["detail"] == {"member_id": str(ACTOR)}
    # Names from the bot cache: actor (member) and target (role); unknown -> None.
    assert body["entries"][0]["actor_name"] == "Keira" and body["entries"][0]["target_name"] == "Mods"
    first = (await internal.get(base, params={"actor_id": 1})).json()["entries"][0]
    assert first["actor_name"] is None and first["target_name"] == "Keira"  # member target
    body = (await internal.get(base, params={"since": "2026-10-02T00:00:00Z", "until": "2026-10-03T00:00:00Z"})).json()
    assert [e["id"] for e in body["entries"]] == [ids[1]]
    for bad in ({"limit": 0}, {"limit": 201}, {"before_id": 0}, {"actor_id": "abc"}, {"since": "yesterday"}):
        assert (await internal.get(base, params=bad)).status_code == 422, bad
    r = await internal.get(base, params={"since": "2026-10-03T00:00:00Z", "until": "2026-10-02T00:00:00Z"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_range"
    actions = (await internal.get(f"{base}/actions")).json()["actions"]
    assert actions[0] == {"action": "role_added", "count": 2}


async def test_internal_permission_routes_audit_with_actor(internal, db):
    base = f"/guilds/{GUILD}/permission-grants"
    r = await internal.post(base, json={"role_id": ROLE, "permission": "view_audit", "granted_by": ACTOR})
    assert r.status_code == 201
    r = await internal.post(base, json={"role_id": ROLE, "permission": "bogus", "granted_by": ACTOR})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_grant"
    assert "Unknown permission" in r.json()["detail"]["message"]
    r = await internal.request("DELETE", base, json={"role_id": ROLE, "permission": "view_audit", "actor_id": ACTOR})
    assert r.json() == {"revoked": True}
    entries = (await internal.get(f"/guilds/{GUILD}/audit")).json()["entries"]
    assert [(e["action"], e["actor_id"], e["target_id"], e["detail"]) for e in entries] == [
        ("permission_revoke", str(ACTOR), str(ROLE), {"permission": "view_audit"}),
        ("permission_grant", str(ACTOR), str(ROLE), {"permission": "view_audit"}),
    ]


# -- dashboard backend -------------------------------------------------------------

@pytest.fixture
def dash(monkeypatch):
    from fastapi.testclient import TestClient

    from server import config as sconfig
    from server.app import app
    from server.auth.dependencies import require_session
    from server.db.models import Session
    from server.services import internal_client

    state = {"allowed": True, "checked": [], "calls": []}

    async def fake_check(guild_id, user_id, permission):
        state["checked"].append(permission)
        return state["allowed"]

    async def fake_is_staff(guild_id, user_id):
        return True

    async def fake_session():
        s = Session()
        s.session_id = "s"
        s.discord_user_id = ACTOR
        s.discord_username = "keira"
        s.is_guild_member = True
        return s

    async def fake_audit(guild_id, params):
        state["calls"].append(params)
        return {"entries": [], "next_before_id": None}

    monkeypatch.setattr(internal_client, "check_permission", fake_check)
    monkeypatch.setattr(internal_client, "check_is_staff", fake_is_staff)
    monkeypatch.setattr(internal_client, "get_audit_log", fake_audit)
    app.dependency_overrides[require_session] = fake_session
    client = TestClient(app, headers={"Origin": sconfig.FRONTEND_BASE_URL})
    client.state = state
    client.ic = internal_client
    yield client
    app.dependency_overrides.pop(require_session, None)


def test_dashboard_audit_requires_view_audit(dash):
    dash.state["allowed"] = False
    r = dash.get("/api/audit")
    assert r.status_code == 403 and r.json()["detail"]["required_permission"] == "view_audit"
    assert dash.state["checked"] == ["view_audit"] and dash.state["calls"] == []


def test_dashboard_audit_validates_and_forwards(dash):
    r = dash.get("/api/audit", params={
        "action": "permission_grant", "actor_id": str(ACTOR), "since": "2026-10-01T00:00:00+02:00",
        "before_id": 10, "limit": 25,
    })
    assert r.status_code == 200
    sent = dash.state["calls"][-1]
    assert sent["action"] == "permission_grant" and sent["actor_id"] == str(ACTOR)
    assert sent["since"] == "2026-09-30T22:00:00+00:00" and sent["until"] is None
    assert sent["before_id"] == 10 and sent["limit"] == 25
    for bad in (
        {"limit": 0}, {"limit": 500}, {"actor_id": "12ab"}, {"target_id": "-1"}, {"before_id": 0},
        {"action": "a b"}, {"since": "nope"}, {"target_id": "99999999999999999999"},
        {"since": "2026-10-02T00:00:00Z", "until": "2026-10-01T00:00:00Z"},
    ):
        assert dash.get("/api/audit", params=bad).status_code == 422, bad


def test_dashboard_audit_error_mapping(dash, monkeypatch):
    from server.services.internal_client import InternalAPIError

    async def down(*a, **k):
        raise InternalAPIError("x")

    async def not_ready(*a, **k):
        raise InternalAPIError("x", status_code=503, detail={"error": "not_ready", "message": "m"})

    async def bad(*a, **k):
        raise InternalAPIError("x", status_code=400, detail={"error": "invalid_range", "message": "m"})

    monkeypatch.setattr(dash.ic, "get_audit_log", down)
    assert dash.get("/api/audit").json()["detail"]["error"] == "internal_api_unavailable"
    monkeypatch.setattr(dash.ic, "get_audit_log", not_ready)
    assert dash.get("/api/audit").status_code == 502
    monkeypatch.setattr(dash.ic, "get_audit_log", bad)
    r = dash.get("/api/audit")
    assert r.status_code == 400 and r.json()["detail"] == {"error": "invalid_range", "message": "m"}
    monkeypatch.setattr(dash.ic, "get_audit_actions", down)
    assert dash.get("/api/audit/actions").status_code == 502


def test_dashboard_permission_routes_pass_actor_and_message(dash, monkeypatch):
    from server.services.internal_client import InternalAPIError

    seen = {}

    async def fake_revoke(guild_id, role_id, permission, actor_id=None):
        seen["actor_id"] = actor_id
        return {"revoked": True}

    async def fake_grant(*a, **k):
        raise InternalAPIError("x", status_code=400, detail={"error": "invalid_grant", "message": "Unknown permission 'x'."})

    monkeypatch.setattr(dash.ic, "revoke_permission", fake_revoke)
    monkeypatch.setattr(dash.ic, "grant_permission", fake_grant)
    r = dash.request("DELETE", "/api/permissions/grants", json={"role_id": ROLE, "permission": "view_audit"})
    assert r.status_code == 200 and seen["actor_id"] == ACTOR
    r = dash.post("/api/permissions/grants", json={"role_id": ROLE, "permission": "x"})
    assert r.status_code == 400 and r.json()["detail"] == {"error": "invalid_grant", "message": "Unknown permission 'x'."}
