"""Birthdays: shared service logic (services/birthday_service.py), the bot's
internal routes (internal_api/routes/birthdays.py) against a fake guild +
in-memory DB, and the dashboard routes (server/routes/birthdays.py) with a
monkeypatched internal_client. Covers: manage_birthdays gating, Feb 29 and
timezone validation, the birthday-role self-role policy, the %e staff-only
rule, import preview/limits, export formats and live publishing."""

from __future__ import annotations

import pathlib
import types
from datetime import date

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import config
from database.connection import Database
from database.repositories.birthday_repo import BirthdayRepository
from services import birthday_service as bs

ROOT = pathlib.Path(__file__).resolve().parents[2]
SQL_DIR = ROOT / "database" / "migrations" / "sql"
GUILD_ID = 100000000000000001
USER = 1530190000000000001
OTHER_USER = 1530190000000000002
ACTOR = 1530190000000000003
STAFF_ROLE = 1530190000000000100
BDAY_ROLE = 1530190000000000101
MOD_ROLE = 1530190000000000102
HIGH_ROLE = 1530190000000000103
CHANNEL = 1530190000000000200


@pytest_asyncio.fixture
async def db():
    d = Database(":memory:")
    await d.connect()
    for name in ("012_birthdays.sql", "013_birthday_settings_v2.sql", "014_birthday_ping_user.sql",
                 "015_birthday_nullable_tz.sql"):
        await d.executescript((SQL_DIR / name).read_text(encoding="utf-8"))
    yield d
    await d.close()


@pytest.fixture
def published(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(bs, "live_publish", lambda topic, data: events.append((topic, data)))
    return events


@pytest_asyncio.fixture
async def svc(db, published):
    return bs.BirthdayService(BirthdayRepository(db))


# ------------------------------------------------------------------ pure logic


def test_valid_month_day_allows_feb_29_only():
    assert bs.valid_month_day(2, 29)
    assert not bs.valid_month_day(2, 30)
    assert not bs.valid_month_day(4, 31)
    assert not bs.valid_month_day(13, 1)


def test_days_until_next_feb_29_in_non_leap_year():
    d, n = bs.days_until_next(2, 29, date(2027, 2, 20))
    assert d == date(2027, 2, 28) and n == 8
    d, n = bs.days_until_next(1, 5, date(2026, 12, 30))
    assert d == date(2027, 1, 5) and n == 6


def test_nearest_birthdays_window_and_order():
    rows = [
        {"user_id": 1, "birth_month": 10, "birth_day": 25},  # 3 days ago
        {"user_id": 2, "birth_month": 11, "birth_day": 1},   # in 4 days
        {"user_id": 3, "birth_month": 10, "birth_day": 28},  # today
        {"user_id": 4, "birth_month": 12, "birth_day": 25},  # outside
    ]
    groups = bs.nearest_birthdays(rows, date(2026, 10, 28))
    assert [g["offset"] for g in groups] == [-3, 0, 4]
    assert [r["user_id"] for r in groups[1]["rows"]] == [3]


def test_import_parser_rejects_impossible_dates():
    text = "Birthdays in X\n\n● Feb-29: 111 a\n● Feb-30: 222 b\n● Foo-01: 333\ngarbage\n● Mar-14: 444 | Time zone: Europe/London"
    parsed = bs.parse_import_text(text)
    assert [e["user_id"] for e in parsed.entries] == [111, 444]
    assert parsed.entries[1]["user_timezone"] == "Europe/London"
    reasons = {n: r for n, _t, r in parsed.invalid}
    assert "not valid for Feb" in reasons[4] and "Unknown month" in reasons[5] and reasons[6] == "Not a birthday line."


def test_exports_handle_departed_members_and_valid_ics():
    rows = [{"user_id": 5, "birth_month": 3, "birth_day": 4, "user_timezone": None},
            {"user_id": 6, "birth_month": 2, "birth_day": 29, "user_timezone": "Europe/London"}]
    member = types.SimpleNamespace(name="ann", global_name=None, nick=None, display_name="Ann, the; one")
    name, csv_text = bs.build_export("csv", GUILD_ID, "G", rows, {6: member})
    assert name.endswith(".csv") and '"5","","","","Mar-04"' in csv_text
    _, ics = bs.build_export("ics", GUILD_ID, "G", rows, {6: member})
    assert "DTSTART;VALUE=DATE:20000304\r\n" in ics and "DTSTART;VALUE=DATE:20000229\r\n" in ics
    assert "Ann\\, the\\; one" in ics
    _, txt = bs.build_export("plaintext", GUILD_ID, "G", rows, {6: member})
    # round trip: the plaintext export imports back
    assert [e["user_id"] for e in bs.parse_import_text(txt).entries] == [5, 6]
    with pytest.raises(ValueError):
        bs.build_export("xml", GUILD_ID, "G", rows, {})


def test_ics_fold_is_octet_safe():
    folded = bs.ics_fold("SUMMARY:" + "é" * 80)
    assert all(len(line.encode("utf-8")) <= 75 for line in folded.split("\r\n"))


def test_everyone_placeholder_is_staff_only():
    assert bs.everyone_block_reason(None, None, "hi %e", None, actor_is_staff=False) == bs.EVERYONE_STAFF_ONLY
    assert bs.everyone_block_reason(None, None, "hi %e", None, actor_is_staff=True) is None
    # unchanged staff-written %e message: a non-staff edit of the OTHER field is fine
    assert bs.everyone_block_reason("hi %e", None, "hi %e", "plural", actor_is_staff=False) is None
    # changing the %e message is not
    assert bs.everyone_block_reason("hi %e", None, "hey %e", None, actor_is_staff=False) == bs.EVERYONE_STAFF_ONLY
    assert bs.everyone_block_reason(None, None, "no ping", None, actor_is_staff=False) is None
    assert bs.hardcoded_mentions("hi <@123>", None) == ["<@123>"]


# ------------------------------------------------------------------ service + repo


@pytest.mark.asyncio
async def test_set_clear_timezone_remove_publish(svc, published):
    ok, _ = await svc.set_birthday(GUILD_ID, USER, "29", "2", tz_str="Europe/London", override=True)
    assert ok
    assert (await svc.get_birthday(GUILD_ID, USER))["user_timezone"] == "Europe/London"
    assert await svc.clear_timezone(GUILD_ID, USER) is True
    assert (await svc.get_birthday(GUILD_ID, USER))["user_timezone"] is None
    assert await svc.clear_timezone(GUILD_ID, USER) is False
    assert await svc.remove_birthday(GUILD_ID, USER) is True
    assert [d["change"] for t, d in published if t == "birthdays"] == ["entry", "entry", "entry"]


@pytest.mark.asyncio
async def test_preview_then_import(svc):
    await svc.set_birthday(GUILD_ID, 111, "1", "1", override=True)
    await svc.set_birthday(GUILD_ID, 222, "2", "2", override=True)
    text = "● Jan-01: 111\n● Mar-03: 222\n● Apr-04: 333\n● Feb-31: 444"
    preview = await svc.preview_import(GUILD_ID, text)
    assert (preview["new"], preview["updated"], preview["unchanged"]) == (1, 1, 1)
    assert len(preview["invalid"]) == 1
    imported, skipped = await svc.import_from_text(GUILD_ID, text)
    assert (imported, skipped) == (3, 1)
    assert (await svc.get_birthday(GUILD_ID, 222))["birth_month"] == 3


@pytest.mark.asyncio
async def test_reset_today_counts_feb_29_on_feb_28(svc, db):
    await svc.set_birthday(GUILD_ID, 1, "29", "2", override=True)
    await svc.set_birthday(GUILD_ID, 2, "28", "2", override=True)
    await svc.mark_celebrated(GUILD_ID, 1, 2027)
    await svc.mark_celebrated(GUILD_ID, 2, 2027)
    assert await svc.reset_today(GUILD_ID, date(2027, 2, 28)) == 2
    assert (await svc.get_birthday(GUILD_ID, 1))["last_celebrated"] == 0


# ------------------------------------------------------------------ internal API


class FakeRole:
    def __init__(self, rid, name, position, *, managed=False, default=False, perms=None):
        self.id, self.name, self.position, self.managed = rid, name, position, managed
        self._default = default
        self.permissions = types.SimpleNamespace(**(perms or {}))
        self.color = types.SimpleNamespace(value=0)

    def is_default(self):
        return self._default

    def __gt__(self, other):
        return self.position > other.position

    def __le__(self, other):
        return self.position <= other.position


class FakePerms:
    def __init__(self, staff_ids):
        self.staff_ids = staff_ids

    def is_staff(self, member):
        return member.id in self.staff_ids

    async def list_grants(self, guild_id):
        return []


class FakeCore:
    def __init__(self, svc):
        self.svc = svc
        self.audits: list = []
        self.tests: list = []
        self.scheduled: dict = {}

    def scheduled_count(self, guild_id):
        return 0

    def cancel_scheduled(self, guild_id, user_id):
        pass

    async def schedule_guild(self, guild_id):
        pass

    async def audit(self, guild_id, actor_id, action, target_type=None, target_id=None, detail=None):
        self.audits.append((actor_id, action, target_id, detail))

    async def send_test_announcement(self, guild, members):
        self.tests.append([m.id for m in members])
        return None

    async def reset_timers(self, guild_id):
        return 0


def _guild():
    everyone = FakeRole(GUILD_ID, "@everyone", 0, default=True)
    roles = {
        r.id: r for r in (
            everyone,
            FakeRole(BDAY_ROLE, "Birthday", 2),
            FakeRole(MOD_ROLE, "Mods", 3, perms={"kick_members": True}),
            FakeRole(STAFF_ROLE, "Staff", 4),
            FakeRole(HIGH_ROLE, "High", 9),
        )
    }
    bot_member = types.SimpleNamespace(id=999, top_role=FakeRole(1, "bot", 8))
    actor = types.SimpleNamespace(id=ACTOR, top_role=FakeRole(2, "actor", 6), roles=[], display_name="act", name="act")
    member = types.SimpleNamespace(id=USER, display_name="Ann", name="ann", global_name=None, nick=None)
    members = {ACTOR: actor, USER: member}

    from unittest.mock import MagicMock

    import discord

    channel = MagicMock(spec=discord.TextChannel)  # isinstance(..., TextChannel)
    channel.id, channel.name = CHANNEL, "birthdays"
    channel.permissions_for = lambda m: types.SimpleNamespace(send_messages=True)

    async def fetch_member(uid):
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="nf"), "nf")

    return types.SimpleNamespace(
        id=GUILD_ID, name="G", owner_id=1, me=bot_member, roles=list(roles.values()),
        get_role=roles.get, get_member=members.get, get_channel=lambda cid: channel if cid == CHANNEL else None,
        fetch_member=fetch_member, members=list(members.values()),
    )


@pytest_asyncio.fixture
async def internal(svc, monkeypatch):
    from internal_api.app import create_app

    monkeypatch.setattr(config, "STAFF_ROLE_IDS", [STAFF_ROLE], raising=False)
    guild = _guild()
    core = FakeCore(svc)
    bot = types.SimpleNamespace(
        get_guild=lambda gid: guild if gid == GUILD_ID else None,
        birthday_core=core, permission_service=FakePerms(set()),
    )
    app = create_app()
    app.state.bot = bot
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers) as c:
        c.core, c.bot = core, bot
        yield c


BASE = f"/guilds/{GUILD_ID}/birthdays"


@pytest.mark.asyncio
async def test_internal_overview_strings_and_entries(internal):
    await internal.core.svc.set_birthday(GUILD_ID, USER, "29", "2", override=True)
    body = (await internal.get(BASE)).json()
    assert body["entries"][0]["user_id"] == str(USER) and body["entries"][0]["display_name"] == "Ann"
    assert body["status"]["registered"] == 1 and body["limits"]["announce_message_max"] == 1500


@pytest.mark.asyncio
async def test_internal_entry_validation(internal):
    r = await internal.put(f"{BASE}/entries/{USER}", json={"actor_id": ACTOR, "month": 2, "day": 30})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_date"
    r = await internal.put(f"{BASE}/entries/{USER}", json={"actor_id": ACTOR, "month": 2, "day": 29, "timezone": "Mars/Base"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_timezone"
    r = await internal.put(f"{BASE}/entries/{OTHER_USER}", json={"actor_id": ACTOR, "month": 1, "day": 1})
    assert r.status_code == 404  # new entry for someone not in the server
    r = await internal.put(f"{BASE}/entries/{USER}", json={"actor_id": ACTOR, "month": 2, "day": 29, "timezone": "europe/london"})
    assert r.status_code == 200 and r.json()["entry"]["user_timezone"] == "Europe/London"
    assert internal.core.audits[-1][1] == "birthday_set" and internal.core.audits[-1][0] == ACTOR
    r = await internal.put(f"{BASE}/entries/{USER}", json={"actor_id": ACTOR, "month": 2, "day": 29, "clear_timezone": True})
    assert r.json()["entry"]["user_timezone"] is None
    r = await internal.request("DELETE", f"{BASE}/entries/{USER}", params={"actor_id": ACTOR})
    assert r.json() == {"removed": True} and internal.core.audits[-1][1] == "birthday_remove"


@pytest.mark.asyncio
@pytest.mark.parametrize("role_id,ok", [(BDAY_ROLE, True), (STAFF_ROLE, False), (MOD_ROLE, False),
                                        (HIGH_ROLE, False), (GUILD_ID, False)])
async def test_internal_birthday_role_policy(internal, role_id, ok):
    r = await internal.put(f"{BASE}/config", json={"actor_id": ACTOR, "birthday_role_id": role_id})
    assert (r.status_code == 200) is ok, r.text
    if not ok:
        assert "birthday_role_id" in r.json()["detail"]["fields"]


@pytest.mark.asyncio
async def test_internal_everyone_placeholder_needs_staff(internal):
    body = {"actor_id": ACTOR, "set_messages": True, "announce_message": "Happy birthday {mention} %e"}
    r = await internal.put(f"{BASE}/config", json=body)
    assert r.status_code == 422 and "announce_message" in r.json()["detail"]["fields"]
    internal.bot.permission_service.staff_ids.add(ACTOR)
    r = await internal.put(f"{BASE}/config", json=body)
    assert r.status_code == 200 and r.json()["settings"]["announce_message"].endswith("%e")
    assert internal.core.audits[-1][1] == "birthday_config_update"


@pytest.mark.asyncio
async def test_internal_config_channel_timezone_and_warning(internal):
    r = await internal.put(f"{BASE}/config", json={
        "actor_id": ACTOR, "announce_channel_id": CHANNEL, "guild_timezone": "america/new_york",
        "set_messages": True, "announce_message": "hi <@123>", "add_only": True,
    })
    body = r.json()
    assert r.status_code == 200 and body["settings"]["announce_channel_id"] == str(CHANNEL)
    assert body["settings"]["guild_timezone"] == "America/New_York" and body["settings"]["add_only"] is True
    assert body["warnings"] and "<@123>" in body["warnings"][0]
    r = await internal.put(f"{BASE}/config", json={"actor_id": ACTOR, "announce_channel_id": 5})
    assert r.status_code == 422
    r = await internal.put(f"{BASE}/config", json={"actor_id": ACTOR, "guild_timezone": "Nope/Zone"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_internal_test_and_import(internal):
    r = await internal.post(f"{BASE}/test", json={"actor_id": ACTOR, "user_ids": [USER]})
    assert r.status_code == 200 and internal.core.tests == [[USER]]
    r = await internal.post(f"{BASE}/test", json={"actor_id": ACTOR, "user_ids": [OTHER_USER]})
    assert r.status_code == 404
    r = await internal.post(f"{BASE}/test", json={"actor_id": ACTOR, "user_ids": [1, 2, 3, 4, 5, 6]})
    assert r.status_code == 422
    content = f"● Mar-14: {USER}\n● Feb-30: 5"
    r = await internal.post(f"{BASE}/import", json={"actor_id": ACTOR, "filename": "a.csv", "content": content})
    assert r.status_code == 422
    r = await internal.post(f"{BASE}/import", json={"actor_id": ACTOR, "filename": "a.txt", "content": content})
    assert r.json()["dry_run"] and r.json()["new"] == 1 and len(r.json()["invalid"]) == 1
    assert await internal.core.svc.get_birthday(GUILD_ID, USER) is None  # dry run wrote nothing
    r = await internal.post(f"{BASE}/import", json={"actor_id": ACTOR, "filename": "a.txt", "content": content, "dry_run": False})
    assert r.json()["imported"] == 1 and internal.core.audits[-1][1] == "birthday_import"
    r = await internal.get(f"{BASE}/export", params={"format": "ics", "actor_id": ACTOR})
    assert r.json()["filename"].endswith(".ics") and r.json()["count"] == 1


@pytest.mark.asyncio
async def test_internal_unknown_actor_refused(internal):
    r = await internal.post(f"{BASE}/reset-timers", json={"actor_id": OTHER_USER})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "actor_not_member"


# ------------------------------------------------------------------ dashboard routes


@pytest.fixture
def dash(monkeypatch):
    from server import config as sconfig
    from server.app import app
    from server.auth.dependencies import get_current_session, require_session
    from server.db.models import Session
    from server.services import internal_client
    from server.services.internal_client import InternalAPIError

    calls: dict = {}
    allowed = {"value": True}

    async def check_permission(guild_id, user_id, perm):
        calls.setdefault("perm", []).append(perm)
        return allowed["value"]

    async def set_birthday_entry(guild_id, user_id, values):
        calls["entry"] = (user_id, values)
        return {"ok": True}

    async def update_birthday_config(guild_id, values):
        calls["config"] = values
        raise InternalAPIError("x", status_code=422, detail={
            "error": "invalid_config", "message": "bad", "fields": {"birthday_role_id": "bad"}})

    async def import_birthdays(guild_id, **kw):
        calls["import"] = kw
        return {"dry_run": kw["dry_run"]}

    async def get_birthdays(guild_id):
        raise InternalAPIError("down")

    for name, fn in (("check_permission", check_permission), ("set_birthday_entry", set_birthday_entry),
                     ("update_birthday_config", update_birthday_config), ("import_birthdays", import_birthdays),
                     ("get_birthdays", get_birthdays)):
        monkeypatch.setattr(internal_client, name, fn)

    s = Session()
    s.session_id, s.discord_user_id, s.discord_username = "sid", ACTOR, "u"
    s.discord_avatar, s.is_guild_member, s.cached_role_ids = None, True, []
    app.dependency_overrides[get_current_session] = lambda: s
    app.dependency_overrides[require_session] = lambda: s
    c = TestClient(app)
    c.calls, c.allowed, c.origin = calls, allowed, sconfig.FRONTEND_BASE_URL  # type: ignore[attr-defined]
    yield c
    app.dependency_overrides.clear()


def test_dashboard_requires_manage_birthdays(dash):
    dash.allowed["value"] = False
    for method, path in (("GET", "/api/birthdays"), ("PUT", f"/api/birthdays/entries/{USER}"),
                         ("POST", "/api/birthdays/reset-timers"), ("GET", "/api/birthdays/export"),
                         ("POST", "/api/birthdays/import"), ("PUT", "/api/birthdays/config")):
        r = dash.request(method, path, json={"month": 1, "day": 1, "filename": "a.txt", "content": ""},
                         headers={"Origin": dash.origin})
        assert r.status_code == 403, path
        assert r.json()["detail"]["required_permission"] == "manage_birthdays"
    assert set(dash.calls["perm"]) == {"manage_birthdays"}


def test_dashboard_actor_is_session_user_and_ids_are_strings(dash):
    r = dash.put(f"/api/birthdays/entries/{USER}", json={"month": 2, "day": 29, "actor_id": 1},
                 headers={"Origin": dash.origin})
    assert r.status_code == 200
    uid, values = dash.calls["entry"]
    assert uid == USER and values["actor_id"] == ACTOR
    r = dash.put("/api/birthdays/entries/abc", json={"month": 2, "day": 29}, headers={"Origin": dash.origin})
    assert r.status_code == 400
    r = dash.put(f"/api/birthdays/entries/{USER}", json={"month": 13, "day": 1}, headers={"Origin": dash.origin})
    assert r.status_code == 422


def test_dashboard_passes_bot_refusals_and_502(dash):
    r = dash.put("/api/birthdays/config", json={"birthday_role_id": str(BDAY_ROLE)}, headers={"Origin": dash.origin})
    assert r.status_code == 422 and r.json()["detail"]["fields"] == {"birthday_role_id": "bad"}
    assert dash.calls["config"]["birthday_role_id"] == BDAY_ROLE and dash.calls["config"]["actor_id"] == ACTOR
    r = dash.get("/api/birthdays")
    assert r.status_code == 502 and r.json()["detail"]["error"] == "internal_api_unavailable"


def test_dashboard_import_validation(dash):
    r = dash.post("/api/birthdays/import", json={"filename": "a.csv", "content": "x"}, headers={"Origin": dash.origin})
    assert r.status_code == 422
    big = "x" * (2_000_001)
    r = dash.post("/api/birthdays/import", json={"filename": "a.txt", "content": big}, headers={"Origin": dash.origin})
    assert r.status_code == 422
    r = dash.post("/api/birthdays/import", json={"filename": "a.txt", "content": "● Jan-01: 1"}, headers={"Origin": dash.origin})
    assert r.status_code == 200 and dash.calls["import"]["actor_id"] == ACTOR and dash.calls["import"]["dry_run"] is True
