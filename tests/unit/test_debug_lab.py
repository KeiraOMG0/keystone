"""services/debug_lab_service.py (owner-only Debug > Lab) and the Settings
`applied_at` field: the command-requirement scanner, the permission simulator,
the anti-spam tester, previews captured instead of sent, and SafeChannel sends."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from services import debug_lab_service as lab
from services import permission_service as ps
from services import settings_service as ss
from services.spam_detector import SpamRules



GUILD = 100000000000000001
OWNER = 111222333444555666
STAFF_ROLE = 200000000000000001
MOD_ROLE = 200000000000000002
FAN_ROLE = 200000000000000003


# ---------------------------------------------------------------------------
# Command requirement scanner
# ---------------------------------------------------------------------------

CONST_PERM = "manage_schedules"


class FakeCog:
    async def cmd_direct(self, ctx):
        if not await self.bot.permission_service.has_permission(ctx.author, "kick_members"):
            return

    async def cmd_const(self, interaction):
        await self.bot.permission_service.has_permission(interaction.user, CONST_PERM)

    async def cmd_staff(self, ctx):
        if not self.bot.permission_service.is_staff(ctx.author):
            return

    async def cmd_any_staff(self, interaction):
        await self.permission_service.is_any_staff_interaction(interaction)

    async def cmd_owner(self, ctx):
        if ctx.author.id != OWNER_ID:  # noqa: F821 - only parsed, never run
            return

    async def cmd_via_helper(self, interaction):
        if await self._deny(interaction):
            return

    async def _deny(self, interaction):
        return not await self._allowed(interaction)

    async def _allowed(self, interaction):
        return await self.bot.permission_service.has_permission(interaction.user, "manage_qotd")

    async def cmd_open(self, ctx):
        await ctx.send("hi")

    async def cmd_dynamic(self, ctx, perm):
        await self.bot.permission_service.has_permission(ctx.author, perm)  # unresolvable: ignored


# The scanner only follows helpers defined in cogs.* modules.
for _name in ("_deny", "_allowed"):
    getattr(FakeCog, _name).__module__ = "cogs.fake"


def test_scan_checks_finds_permissions_staff_owner_and_helpers():
    cog = FakeCog()
    assert lab.scan_checks(FakeCog.cmd_direct, cog) == [{"type": "permission", "permission": "kick_members"}]
    assert lab.scan_checks(FakeCog.cmd_const, cog) == [{"type": "permission", "permission": "manage_schedules"}]
    assert lab.scan_checks(FakeCog.cmd_staff, cog) == [{"type": "staff"}]
    assert lab.scan_checks(FakeCog.cmd_any_staff, cog) == [{"type": "permission", "permission": "staff_base"}]
    assert lab.scan_checks(FakeCog.cmd_owner, cog) == [{"type": "owner"}]
    assert lab.scan_checks(FakeCog.cmd_via_helper, cog) == [{"type": "permission", "permission": "manage_qotd"}]
    assert lab.scan_checks(FakeCog.cmd_open, cog) == []
    assert lab.scan_checks(FakeCog.cmd_dynamic, cog) == []


def test_scan_checks_does_not_follow_non_cog_helpers():
    class Other:
        async def cmd(self, i):
            await self._helper(i)

        async def _helper(self, i):
            await self.p.has_permission(i.user, "ban_members")

    # _helper lives in a tests.* module, not cogs.*: not followed.
    assert lab.scan_checks(Other.cmd, Other()) == []


def test_command_requirements_on_real_command_objects():
    from discord import app_commands
    from discord.ext import commands

    class C(commands.Cog):
        @commands.command(name="zap")
        async def zap(self, ctx):
            await self.bot.permission_service.has_permission(ctx.author, "moderate_members")

        @app_commands.command(name="cfg", description="x")
        @app_commands.default_permissions(manage_guild=True)
        async def cfg(self, interaction: discord.Interaction):
            self.bot.permission_service.is_staff(interaction.user)

    cog = C()
    bot = MagicMock()
    bot.walk_commands = lambda: iter(cog.get_commands())
    bot.tree.walk_commands = lambda: iter(cog.get_app_commands())
    out = {c["name"]: c for c in lab.command_requirements(bot)}
    assert out["!zap"]["checks"] == [{"type": "permission", "permission": "moderate_members"}]
    assert out["/cfg"]["checks"] == [{"type": "staff"}]
    assert out["/cfg"]["default_permissions"] == ["manage_guild"]


# ---------------------------------------------------------------------------
# Permission simulator
# ---------------------------------------------------------------------------

def _role(rid, name, position, perms=0):
    return SimpleNamespace(id=rid, name=name, position=position, permissions=discord.Permissions(perms),
                           color=discord.Colour(0), managed=False)


def _guild():
    everyone = _role(GUILD, "@everyone", 0)
    roles = {r.id: r for r in (everyone, _role(STAFF_ROLE, "Staff", 3), _role(MOD_ROLE, "Mod", 2), _role(FAN_ROLE, "Fan", 1))}
    return SimpleNamespace(id=GUILD, default_role=everyone, roles=list(roles.values()), get_role=roles.get,
                           get_channel=lambda _id: None)


def _member(uid, role_ids, guild):
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.name = "someone"
    m.display_name = "Someone"
    m.bot = False
    m.display_avatar.url = "https://cdn/x.png"
    m.roles = [guild.default_role] + [guild.get_role(r) for r in role_ids]
    m.guild_permissions = discord.Permissions.none()
    return m


@pytest.fixture
def perm_bot(monkeypatch):
    monkeypatch.setattr(ps, "OWNER_ID", OWNER)
    monkeypatch.setattr(ps, "DEFAULT_STAFF_ROLE_IDS", {STAFF_ROLE})
    grants = [{"role_id": MOD_ROLE, "permission": "moderate_members"}, {"role_id": MOD_ROLE, "permission": "staff_base"},
              {"role_id": 999999999999999999, "permission": "view_audit"}]
    repo = MagicMock()
    repo.list_grants = AsyncMock(return_value=grants)

    async def has_permission(guild_id, role_ids, perm):
        return any(g["role_id"] in role_ids and g["permission"] == perm for g in grants)

    repo.has_permission = AsyncMock(side_effect=has_permission)
    bot = MagicMock()
    bot.permission_service = ps.PermissionService(repo)
    bot.walk_commands = lambda: iter([])
    bot.tree.walk_commands = lambda: iter([])
    return bot


async def test_simulate_member_with_grant(perm_bot):
    guild = _guild()
    member = _member(400000000000000001, [MOD_ROLE], guild)
    member.guild = guild
    r = await lab.simulate_permissions(perm_bot, guild, member=member)
    by = {p["permission"]: p for p in r["permissions"]}
    assert r["is_staff"] is False and r["is_owner"] is False
    assert by["moderate_members"]["allowed"] and by["moderate_members"]["via"] == "grant"
    assert by["moderate_members"]["grants"] == [{"role_id": str(MOD_ROLE), "role_name": "Mod"}]
    assert by["moderate_members"]["bot_says"] is True
    assert not by["ban_members"]["allowed"] and by["ban_members"]["via"] is None
    assert r["unknown_role_grants"] == ["999999999999999999"]
    assert not any("disagrees" in n or "says" in n for n in r["notes"])


async def test_simulate_owner_and_staff_bypass(perm_bot):
    guild = _guild()
    owner = _member(OWNER, [], guild)
    owner.guild = guild
    r = await lab.simulate_permissions(perm_bot, guild, member=owner)
    assert r["is_owner"] and r["is_staff"] and r["staff_reason"] == "owner"
    assert all(p["allowed"] and p["via"] == "owner" for p in r["permissions"])

    r = await lab.simulate_permissions(perm_bot, guild, role_ids=[STAFF_ROLE])
    assert r["is_staff"] and r["staff_reason"] == "staff_role" and not r["is_owner"]
    assert r["subject"]["mode"] == "roles"
    assert {x["id"] for x in r["subject"]["roles"]} == {str(GUILD), str(STAFF_ROLE)}


async def test_simulate_role_set_commands_and_unknown_role(perm_bot):
    guild = _guild()
    perm_bot.walk_commands = lambda: iter([])
    cmds = [
        {"name": "!warn", "kind": "prefix", "cog": "M", "aliases": [], "custom_checks": 0, "default_permissions": [],
         "checks": [{"type": "permission", "permission": "moderate_members"}]},
        {"name": "!antispam", "kind": "prefix", "cog": "A", "aliases": [], "custom_checks": 0, "default_permissions": [],
         "checks": [{"type": "permission", "permission": "staff_base"}, {"type": "staff"}]},
        {"name": "!ban", "kind": "prefix", "cog": "M", "aliases": [], "custom_checks": 0, "default_permissions": [],
         "checks": [{"type": "permission", "permission": "ban_members"}]},
        {"name": "!play", "kind": "prefix", "cog": "Music", "aliases": [], "custom_checks": 0, "default_permissions": [], "checks": []},
    ]
    orig = lab.command_requirements
    lab.command_requirements = lambda bot: cmds
    try:
        r = await lab.simulate_permissions(perm_bot, guild, role_ids=[MOD_ROLE])
    finally:
        lab.command_requirements = orig
    access = {c["name"]: c["access"] for c in r["commands"]}
    assert access == {"!warn": "yes", "!antispam": "partial", "!ban": "no", "!play": "yes"}
    with pytest.raises(lab.LabError):
        await lab.simulate_permissions(perm_bot, guild, role_ids=[123456789012345678])


# ---------------------------------------------------------------------------
# Anti-spam tester
# ---------------------------------------------------------------------------

RULES = SpamRules(enabled=True, flood_messages=5, flood_seconds=5, duplicate_messages=3, duplicate_seconds=30,
                  mention_limit=4, timeout_minutes=10)


def test_antispam_flood_and_cooldown():
    msgs = [{"at": i * 0.5, "content": f"m{i}"} for i in range(6)]
    r = lab.simulate_antispam(RULES, msgs)
    assert r["results"][4]["verdict"]["rule"] == "flood"
    assert r["results"][4]["verdict"]["deletes"] == [0, 1, 2, 3, 4]
    assert r["results"][5]["verdict"]["rule"] == "cooldown" and r["results"][5]["verdict"]["first"] is False
    assert r["timeouts"] == [{"user": "A", "at": 2.0, "minutes": 10, "rule": "flood"}]
    assert r["deleted"] == [0, 1, 2, 3, 4, 5]


def test_antispam_duplicate_across_channels_and_users_are_separate():
    msgs = [{"at": i * 3, "channel": ch, "content": "FREE nitro"} for i, ch in enumerate(["a", "b", "c"])]
    msgs.append({"at": 1, "user": "B", "content": "FREE nitro"})
    r = lab.simulate_antispam(RULES, msgs)
    hit = r["results"][2]["verdict"]
    assert hit["rule"] == "duplicate" and "across 3 channels" in hit["detail"]
    assert r["results"][3]["verdict"] is None  # user B: own history


def test_antispam_mentions_counted_from_text_and_validation():
    text = " ".join(f"<@{100000000000000000 + i}>" for i in range(3)) + " <@&200000000000000000>"
    assert lab.count_mentions(text) == 4
    r = lab.simulate_antispam(RULES, [{"at": 0, "content": text}])
    assert r["results"][0]["verdict"]["rule"] == "mentions"
    off = lab.simulate_antispam(SpamRules(enabled=False), [{"at": 0, "content": "hi"}])
    assert any("OFF" in n for n in off["notes"])
    with pytest.raises(lab.LabError):
        lab.simulate_antispam(RULES, [])
    with pytest.raises(lab.LabError):
        lab.simulate_antispam(RULES, [{"at": -1}])
    with pytest.raises(lab.LabError):
        lab.simulate_antispam(RULES, [{"at": 0}] * (lab.MAX_SAMPLE_MESSAGES + 1))


async def test_effective_rules_overrides_validated(monkeypatch):
    values = {"antispam_enabled": True, "antispam_flood_messages": 6, "antispam_flood_seconds": 5,
              "antispam_duplicate_messages": 5, "antispam_duplicate_seconds": 30, "antispam_mention_limit": 6,
              "antispam_timeout_minutes": 10}
    monkeypatch.setattr(ss.SettingsService, "get_effective", AsyncMock(return_value=dict(values)))
    bot = SimpleNamespace(db=object())
    rules = await lab.effective_spam_rules(bot, GUILD, {"antispam_flood_messages": 3})
    assert rules.flood_messages == 3 and rules.duplicate_messages == 5
    with pytest.raises(lab.LabError):
        await lab.effective_spam_rules(bot, GUILD, {"antispam_flood_messages": 1})  # 0 or >= 3
    with pytest.raises(lab.LabError):
        await lab.effective_spam_rules(bot, GUILD, {"auto_kick": False})  # not anti-spam


# ---------------------------------------------------------------------------
# Previews: captured, never sent; "send copy" only via SafeChannel
# ---------------------------------------------------------------------------

async def test_welcome_preview_is_captured_not_sent():
    real_channel = MagicMock()
    real_channel.id = 777777777777777777
    real_channel.name = "welcome"
    real_channel.send = AsyncMock()
    guild = SimpleNamespace(id=GUILD, get_channel=lambda cid: real_channel if cid == real_channel.id else None,
                            name="G", member_count=5)
    member = MagicMock(spec=discord.Member)
    member.id = 400000000000000001
    member.display_name = "Someone"
    member.name = "someone"
    member.guild = guild

    async def send_welcome(channel, m):
        await channel.send(content=f"Welcome {m.display_name} in #{channel.name}", embeds=[discord.Embed(title="hi")])

    service = SimpleNamespace(get_config=AsyncMock(return_value={"channel_id": real_channel.id, "enabled": False}),
                              send_welcome=send_welcome)
    bot = MagicMock()
    bot.get_cog = lambda name: SimpleNamespace(_service=service) if name == "Welcome" else None
    preview = await lab.render_preview(bot, guild, member, "welcome")
    real_channel.send.assert_not_called()
    out = lab.to_json(preview)
    assert out["messages"][0]["content"] == "Welcome Someone in #welcome"
    assert out["messages"][0]["embeds"][0]["title"] == "hi"
    assert out["channel_id"] == str(real_channel.id)
    assert any("disabled" in n for n in out["notes"])
    assert "user" in out["placeholders"]


async def test_preview_unknown_kind_and_missing_cog():
    bot = MagicMock()
    bot.get_cog = lambda name: None
    member = MagicMock(spec=discord.Member)
    with pytest.raises(lab.LabError):
        await lab.render_preview(bot, SimpleNamespace(id=GUILD), member, "boost")
    with pytest.raises(lab.LabError):
        await lab.render_preview(bot, SimpleNamespace(id=GUILD), member, "nope")


async def test_send_preview_goes_through_safechannel():
    sent = []

    async def send(content=None, **kwargs):
        msg = MagicMock()
        msg.id = 9000 + len(sent)
        msg.jump_url = "https://discord.com/channels/1/2/3"
        sent.append((content, kwargs))
        return msg

    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 42
    ch.send = AsyncMock(side_effect=send)
    preview = {"kind": "event", "event_type": "member_join",
               "messages": [{"content": "@everyone <@400000000000000001> hi", "embeds": [discord.Embed(title="x")]}]}
    res = await lab.send_preview(preview, ch, OWNER)
    content, kwargs = sent[0]
    assert content.startswith("[TEST] Lab preview: member_join\n")
    am = kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is False and [u.id for u in am.users] == [OWNER]
    assert res["message_ids"] == ["9000"] and res["channel_id"] == "42"


# ---------------------------------------------------------------------------
# Settings: applied_at
# ---------------------------------------------------------------------------

async def test_settings_report_has_applied_at(monkeypatch):
    monkeypatch.setattr(ss, "_applied_at", None)
    monkeypatch.setattr(ss, "_env_defaults", {})
    monkeypatch.setattr(ss, "_startup_overrides", {})
    repo = MagicMock()
    repo.get_all = AsyncMock(return_value={})
    svc = ss.SettingsService(repo)
    assert (await svc.get_report(GUILD))["applied_at"] is None
    await svc.apply_to_config(GUILD)
    first = (await svc.get_report(GUILD))["applied_at"]
    assert isinstance(first, float) and first > 0


def test_scan_checks_keeps_source_order():
    class C:
        async def cmd(self, ctx, arg=None):
            if not await self.p.has_permission(ctx.author, "manage_messages_bulk"):
                return
            if arg:
                if not self.p.is_staff(ctx.author):
                    return

    # ast.walk is breadth-first; the scanner re-sorts so the entry gate comes first.
    assert lab.scan_checks(C.cmd, C()) == [{"type": "permission", "permission": "manage_messages_bulk"}, {"type": "staff"}]
