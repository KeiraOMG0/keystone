"""SettingsService (services/settings_service.py): strict parsing, per-field
validation, Discord-backed checks against fake guild objects, audit rows,
startup apply onto the config module, and the internal API route. No network."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
import httpx
import pytest
import pytest_asyncio

import config
import services.settings_service as ss
from database.connection import Database
from database.repositories.audit_repo import AuditRepository
from database.repositories.settings_repo import SettingsRepository
from database.repositories.template_repo import TemplateRepository
from services.audit_service import AuditService
from services.settings_service import (
    SETTINGS_SCHEMA,
    GuildUnavailableError,
    SettingsService,
    SettingsValidationError,
    parse_bool,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]
SQL_DIR = ROOT / "database" / "migrations" / "sql"
GUILD_ID = 100000000000000001
ACTOR = 424242424242424242

ROLE_LOW = 1100000000000000001
ROLE_LOW2 = 1100000000000000002
ROLE_HIGH = 1100000000000000003
ROLE_MANAGED = 1100000000000000004
CATEGORY = 1200000000000000001
TEXT = 1200000000000000002
TEXT_NO_PERMS = 1200000000000000003
VOICE = 1200000000000000004



# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRole:
    def __init__(self, rid, name, position, managed=False, default=False):
        self.id, self.name, self.position, self.managed, self._default = rid, name, position, managed, default

    def is_default(self):
        return self._default


class FakeChannel:
    def __init__(self, cid, name, ctype, can_post=True, category=None):
        self.id, self.name, self.type, self.category = cid, name, ctype, category
        self._can_post = can_post

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=True, send_messages=self._can_post, embed_links=self._can_post)


def make_guild(manage_roles=True):
    everyone = FakeRole(GUILD_ID, "@everyone", 0, default=True)
    bot_top = FakeRole(1100000000000000009, "Bot", 10)
    roles = [everyone, FakeRole(ROLE_LOW, "Unverified", 2), FakeRole(ROLE_LOW2, "Verified", 3),
             FakeRole(ROLE_HIGH, "Admin", 20), FakeRole(ROLE_MANAGED, "Integration", 5, managed=True), bot_top]
    cat = FakeChannel(CATEGORY, "unverified", discord.ChannelType.category)
    channels = [cat, FakeChannel(TEXT, "mod-log", discord.ChannelType.text, category=cat),
                FakeChannel(TEXT_NO_PERMS, "locked", discord.ChannelType.text, can_post=False),
                FakeChannel(VOICE, "voice", discord.ChannelType.voice)]
    by_role = {r.id: r for r in roles}
    by_ch = {c.id: c for c in channels}
    guild = SimpleNamespace(
        id=GUILD_ID, roles=roles,
        categories=[cat], text_channels=[c for c in channels if c.type == discord.ChannelType.text],
        me=SimpleNamespace(top_role=bot_top, guild_permissions=discord.Permissions(manage_roles=manage_roles)),
    )
    guild.get_role = by_role.get
    guild.get_channel = by_ch.get
    return guild


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    for name in ("002_templates.sql", "024_guild_settings.sql"):
        await database.executescript((SQL_DIR / name).read_text(encoding="utf-8"))
    await database.execute(
        "INSERT INTO message_templates (guild_id, name, content, created_by, updated_by) VALUES (?, ?, ?, ?, ?)",
        (GUILD_ID, "verify", "hi", ACTOR, ACTOR),
    )
    yield database
    await database.close()


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch):
    """Restore every config attribute the service may touch, and the module's
    env-default snapshot, after each test."""
    for field in SETTINGS_SCHEMA.values():
        monkeypatch.setattr(config, field.config_attr, getattr(config, field.config_attr))
    monkeypatch.setattr(config, "OWNER_ID", 555555555555555555)
    monkeypatch.setattr(config, "TICKET_ARCHIVE_PURGE_DAYS", 30)
    monkeypatch.setattr(config, "CHECK_INTERVAL_MINUTES", 10)
    monkeypatch.setattr(config, "AUTO_KICK", True)
    monkeypatch.setattr(ss, "_env_defaults", {})
    monkeypatch.setattr(ss, "_startup_overrides", {})
    ss.invalidate_cache()


@pytest.fixture
def svc(db):
    return SettingsService(SettingsRepository(db), TemplateRepository(db), AuditService(AuditRepository(db)))


async def _audit_rows(db):
    rows = await db.fetchall("SELECT action, actor_id, detail_json FROM audit_log")
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Strict parsing
# ---------------------------------------------------------------------------

async def test_parse_bool_is_strict():
    for v in (True, 1, "true", "TRUE", " 1 ", "True"):
        assert parse_bool(v) is True
    for v in (False, 0, "false", "0", "False"):
        assert parse_bool(v) is False
    for v in ("yes", "no", "on", "", "2", 2, None, 1.0, "falsey"):
        with pytest.raises(ValueError):
            parse_bool(v)


async def test_stored_false_reads_back_false(svc, db):
    repo = SettingsRepository(db)
    await repo.set(GUILD_ID, "auto_kick", "false", ACTOR)
    await repo.set(GUILD_ID, "ticket_chat_logging_enabled", "0", ACTOR)
    eff = await svc.get_effective(GUILD_ID)
    assert eff["auto_kick"] is False and eff["ticket_chat_logging_enabled"] is False


async def test_bad_stored_values_fall_back_and_are_flagged(svc, db):
    repo = SettingsRepository(db)
    await repo.set(GUILD_ID, "auto_kick", "yes please", ACTOR)
    await repo.set(GUILD_ID, "ticket_archive_purge_days", "0", ACTOR)  # out of range: never purge everything
    await repo.set(GUILD_ID, "music_default_volume", "loud", ACTOR)
    eff = await svc.get_effective(GUILD_ID)
    assert eff["auto_kick"] is True  # env default
    assert eff["ticket_archive_purge_days"] == 30
    report = await svc.get_report(GUILD_ID, make_guild())
    for key in ("auto_kick", "ticket_archive_purge_days", "music_default_volume"):
        assert report["fields"][key]["invalid_stored"], key
        assert report["fields"][key]["source"] == "default"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,value", [
    ("check_interval_minutes", 0), ("check_interval_minutes", 1441), ("check_interval_minutes", "ten"),
    ("check_interval_minutes", True), ("ticket_archive_purge_days", 0), ("ticket_archive_purge_days", -3),
    ("ticket_archive_purge_days", 3651), ("music_default_volume", 101), ("music_default_volume", -1),
    ("music_inactivity_timeout", 5), ("music_inactivity_timeout", 86401), ("music_default_search", "ytsearch"),
    ("music_default_search", "ytsearch100"), ("music_default_search", "gvsearch5"), ("music_default_search", 7),
    ("auto_kick", "yes"), ("auto_kick", 2), ("verify_template_name", ""), ("verify_template_name", "x" * 101),
    ("log_channel_id", "abc"), ("log_channel_id", 12), ("unverified_role_id", True),
])
async def test_type_and_range_rejected(svc, key, value):
    with pytest.raises(SettingsValidationError) as exc:
        await svc.validate(GUILD_ID, {key: value}, make_guild())
    assert key in exc.value.fields


@pytest.mark.parametrize("key,value,expected", [
    ("check_interval_minutes", 1, 1), ("check_interval_minutes", "1440", 1440),
    ("ticket_archive_purge_days", 3650, 3650), ("music_default_volume", 0, 0),
    ("music_default_search", "scsearch5", "scsearch5"), ("music_default_search", "ytsearch99", "ytsearch99"),
    ("auto_kick", "false", False), ("music_allow_playlists", 1, True), ("verify_template_name", " verify ", "verify"),
])
async def test_valid_values_parse(svc, key, value, expected):
    assert (await svc.validate(GUILD_ID, {key: value}, make_guild()))[key] == expected


async def test_unknown_and_read_only_keys_rejected_and_nothing_written(svc, db):
    with pytest.raises(SettingsValidationError) as exc:
        await svc.update(GUILD_ID, {"discord_token": "x", "owner_id": "123456789012345678", "auto_kick": False},
                         ACTOR, make_guild())
    assert exc.value.fields["discord_token"] == "Unknown setting."
    assert "read-only" in exc.value.fields["owner_id"]
    assert await SettingsRepository(db).get_all(GUILD_ID) == {}
    assert await _audit_rows(db) == []


@pytest.mark.parametrize("key,value,needle", [
    ("verified_role_id", 1100000000000000999, "No role"),
    ("verified_role_id", TEXT, "No role"),
    ("verified_role_id", ROLE_HIGH, "above the bot"),
    ("verified_role_id", ROLE_MANAGED, "managed"),
    ("verified_role_id", GUILD_ID, "@everyone"),
    ("unverified_category_id", TEXT, "not a category"),
    ("unverified_category_id", 1200000000000000999, "No channel"),
    ("log_channel_id", CATEGORY, "not a text channel"),
    ("join_leave_channel_id", VOICE, "not a text channel"),
    ("log_channel_id", TEXT_NO_PERMS, "can't post"),
])
async def test_discord_checks(svc, key, value, needle):
    with pytest.raises(SettingsValidationError) as exc:
        await svc.validate(GUILD_ID, {key: str(value)}, make_guild())
    assert needle in exc.value.fields[key]


async def test_role_needs_manage_roles(svc):
    with pytest.raises(SettingsValidationError) as exc:
        await svc.validate(GUILD_ID, {"verified_role_id": str(ROLE_LOW2)}, make_guild(manage_roles=False))
    assert "Manage Roles" in exc.value.fields["verified_role_id"]


async def test_valid_ids_and_cross_field(svc):
    guild = make_guild()
    parsed = await svc.validate(GUILD_ID, {
        "verified_role_id": str(ROLE_LOW2), "unverified_role_id": str(ROLE_LOW),
        "unverified_category_id": str(CATEGORY), "log_channel_id": str(TEXT),
    }, guild)
    assert parsed["verified_role_id"] == ROLE_LOW2 and parsed["log_channel_id"] == TEXT
    with pytest.raises(SettingsValidationError) as exc:
        await svc.validate(GUILD_ID, {"verified_role_id": str(ROLE_LOW), "unverified_role_id": str(ROLE_LOW)}, guild)
    assert "different" in exc.value.fields["verified_role_id"]


async def test_ids_need_the_guild(svc):
    with pytest.raises(GuildUnavailableError):
        await svc.validate(GUILD_ID, {"log_channel_id": str(TEXT)}, None)
    # Non-ID fields don't need it.
    assert (await svc.validate(GUILD_ID, {"auto_kick": False}, None)) == {"auto_kick": False}


async def test_template_must_exist(svc):
    assert (await svc.validate(GUILD_ID, {"verify_template_name": "verify"}, None))["verify_template_name"] == "verify"
    with pytest.raises(SettingsValidationError) as exc:
        await svc.validate(GUILD_ID, {"verify_ticket_welcome_template_name": "nope"}, None)
    assert "No template" in exc.value.fields["verify_ticket_welcome_template_name"]


# ---------------------------------------------------------------------------
# Writes, audit, report
# ---------------------------------------------------------------------------

async def test_update_writes_audits_and_reports_pending(svc, db):
    guild = make_guild()
    changed = await svc.update(GUILD_ID, {"auto_kick": "false", "log_channel_id": str(TEXT),
                                          "ticket_archive_purge_days": 45}, ACTOR, guild)
    assert changed == ["auto_kick", "log_channel_id", "ticket_archive_purge_days"]
    stored = await SettingsRepository(db).get_all(GUILD_ID)
    assert stored["auto_kick"] == "false" and stored["log_channel_id"] == str(TEXT)

    rows = await _audit_rows(db)
    assert len(rows) == 1 and rows[0]["action"] == "settings_update" and rows[0]["actor_id"] == ACTOR
    detail = json.loads(rows[0]["detail_json"])
    assert detail["auto_kick"] == {"old": True, "new": False}
    assert detail["log_channel_id"]["new"] == str(TEXT)  # snowflakes as strings
    assert detail["ticket_archive_purge_days"] == {"old": 30, "new": 45}

    report = await svc.get_report(GUILD_ID, guild)
    f = report["fields"]
    assert f["auto_kick"]["saved"] is False and f["auto_kick"]["running"] is True and f["auto_kick"]["pending_restart"]
    assert not f["ticket_archive_purge_days"]["pending_restart"]  # live field
    assert f["ticket_archive_purge_days"]["running"] == 45
    assert set(report["pending_restart"]) == {"auto_kick", "log_channel_id"}
    assert report["values"]["log_channel_id"] == str(TEXT)
    assert report["fields"]["owner_id"]["saved"] == "555555555555555555"
    assert any(r["id"] == str(ROLE_LOW) and r["assignable"] for r in report["options"]["roles"])
    assert not any(r["id"] == str(ROLE_HIGH) and r["assignable"] for r in report["options"]["roles"])
    assert report["options"]["templates"] == ["verify"]

    # Unchanged values are not re-written or re-audited.
    assert await svc.update(GUILD_ID, {"auto_kick": False}, ACTOR, guild) == []
    assert len(await _audit_rows(db)) == 1


async def test_null_resets_to_default(svc, db):
    await svc.update(GUILD_ID, {"check_interval_minutes": 5}, ACTOR, None)
    assert (await svc.get_effective(GUILD_ID))["check_interval_minutes"] == 5
    assert await svc.update(GUILD_ID, {"check_interval_minutes": None}, ACTOR, None) == ["check_interval_minutes"]
    assert "check_interval_minutes" not in await SettingsRepository(db).get_all(GUILD_ID)
    assert (await svc.get_effective(GUILD_ID))["check_interval_minutes"] == 10
    detail = json.loads((await _audit_rows(db))[-1]["detail_json"])
    assert detail["check_interval_minutes"] == {"old": 5, "new": 10, "reset": True}


async def test_update_invalidates_live_cache(svc):
    ss.set_cached("ticket_archive_purge_days", 30)
    await svc.update(GUILD_ID, {"ticket_archive_purge_days": 60}, ACTOR, None)
    assert ss.get_cached("ticket_archive_purge_days") is None


# ---------------------------------------------------------------------------
# Startup apply
# ---------------------------------------------------------------------------

async def test_apply_to_config_sets_attrs_and_never_owner(svc, db):
    repo = SettingsRepository(db)
    await repo.set(GUILD_ID, "auto_kick", "false", ACTOR)
    await repo.set(GUILD_ID, "check_interval_minutes", "15", ACTOR)
    await repo.set(GUILD_ID, "log_channel_id", str(TEXT), ACTOR)
    await repo.set(GUILD_ID, "owner_id", "123456789012345678", ACTOR)  # must be ignored
    await repo.set(GUILD_ID, "music_default_volume", "999", ACTOR)    # invalid: skipped
    await repo.set(GUILD_ID, "discord_token", "nope", ACTOR)            # unknown: skipped
    volume_before = config.MUSIC_DEFAULT_VOLUME

    applied = await svc.apply_to_config(GUILD_ID)

    assert config.AUTO_KICK is False
    assert config.CHECK_INTERVAL_MINUTES == 15
    assert config.LOG_CHANNEL_ID == TEXT
    assert config.OWNER_ID == 555555555555555555
    assert config.MUSIC_DEFAULT_VOLUME == volume_before
    assert set(applied) == {"auto_kick", "check_interval_minutes", "log_channel_id"}
    assert applied["auto_kick"] == (True, False)

    # After the apply the running value matches the saved one (nothing pending),
    # and the env default is still the pre-apply value.
    report = await svc.get_report(GUILD_ID, make_guild())
    assert report["pending_restart"] == []
    assert report["fields"]["check_interval_minutes"]["default"] == 10
    assert report["fields"]["check_interval_minutes"]["running"] == 15
    # A reset now is pending until the next restart.
    await svc.update(GUILD_ID, {"check_interval_minutes": None}, ACTOR, None)
    report = await svc.get_report(GUILD_ID, None)
    assert report["fields"]["check_interval_minutes"]["saved"] == 10
    assert report["pending_restart"] == ["check_interval_minutes"]


async def test_apply_with_no_rows_keeps_env(svc):
    before = {f.config_attr: getattr(config, f.config_attr) for f in SETTINGS_SCHEMA.values()}
    assert await svc.apply_to_config(GUILD_ID) == {}
    assert {a: getattr(config, a) for a in before} == before


def test_main_imports_do_not_preload_import_time_copies():
    """The startup apply only works if no module that copies config.X at import
    is imported before it. Run main.py's module-level imports in a fresh
    interpreter and check none of IMPORT_TIME_COPIES is loaded."""
    import ast

    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    lines = []
    for node in tree.body:
        nodes = [node]
        if isinstance(node, ast.Try):
            nodes = node.body
        for n in nodes:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                lines.append(ast.unparse(n))
    # Imports inside main() that run before the apply step.
    main_fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")
    for n in ast.walk(main_fn):
        if isinstance(n, ast.ImportFrom) and n.module in ("database.repositories.advanced_repo",
                                                          "services.permission_service"):
            lines.append(ast.unparse(n))
    code = "\n".join(lines) + (
        "\nimport sys\nfrom services.settings_service import IMPORT_TIME_COPIES\n"
        "print(','.join(m for m in IMPORT_TIME_COPIES if m in sys.modules))\n"
    )
    env = dict(os.environ, DISCORD_TOKEN=os.environ.get("DISCORD_TOKEN") or "x",
               INTERNAL_API_SECRET=os.environ.get("INTERNAL_API_SECRET") or "x")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1:] in ([""], []), out.stdout


# ---------------------------------------------------------------------------
# Internal API route
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def api(db):
    from internal_api.app import create_app

    guild = make_guild()
    bot = MagicMock()
    bot.get_guild = lambda gid: guild if gid == GUILD_ID else None
    bot.db = db
    app = create_app()
    app.state.bot = bot
    transport = httpx.ASGITransport(app=app)
    auth = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=auth) as c:
        yield c


async def test_internal_route_422_and_success(api, db):
    base = f"/guilds/{GUILD_ID}/settings"
    r = await api.put(base, json={"updated_by": ACTOR, "values": {"ticket_archive_purge_days": 0, "bogus": 1}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "validation_failed"
    assert set(detail["fields"]) == {"ticket_archive_purge_days", "bogus"}

    r = await api.put(base, json={"updated_by": ACTOR, "values": {"log_channel_id": str(TEXT), "auto_kick": False}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == ["auto_kick", "log_channel_id"]
    assert body["values"]["log_channel_id"] == str(TEXT)
    assert body["fields"]["auto_kick"]["pending_restart"] is True

    r = await api.get(base)
    assert r.status_code == 200 and r.json()["guild_available"] is True


async def test_internal_route_guild_unavailable(api):
    r = await api.put("/guilds/1/settings", json={"updated_by": ACTOR, "values": {"log_channel_id": str(TEXT)}})
    assert r.status_code == 503 and r.json()["detail"]["error"] == "guild_unavailable"
