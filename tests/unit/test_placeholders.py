"""Template placeholders (core/placeholder_engine.py, 2026-10-30).

Syntax ({name}, {name:arg}, {name|fallback}), unknown names left alone, a
failing placeholder rendering empty, lookups by name/ID (never @everyone),
moderation data ({reason}, {duration}, ...), the non-member target fallback,
the catalogue metadata, sample_context, renderer clipping, and the mod-log /
verify call sites passing their data in.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import core.placeholder_engine as pe
from core.placeholder_engine import RenderContext, human_duration, ordinal, resolve_text
from core.renderer import TemplateRenderer


class Role(SimpleNamespace):
    def is_default(self):
        return self.name == "@everyone"


class Chan(SimpleNamespace):
    pass


def _guild():
    everyone = Role(id=1, name="@everyone", mention="@everyone", colour=discord.Colour(0), members=[])
    mod = Role(id=2, name="Moderator", mention="<@&2>", colour=discord.Colour(0xF2B6CD), members=[1, 2])
    general = Chan(id=10, name="💬┊general", mention="<#10>", topic="Chat here", category=SimpleNamespace(name="Main"))
    rules = Chan(id=11, name="rules", mention="<#11>", topic=None, category=None)
    joined = discord.utils.utcnow() - datetime.timedelta(days=3)
    me = SimpleNamespace(
        id=500, name="sunny.day", display_name="Sunny", global_name="Sunny D", nick=None, mention="<@500>",
        bot=False, joined_at=joined, created_at=joined - datetime.timedelta(days=400),
        display_avatar=SimpleNamespace(url="https://cdn/avatar.png"), roles=[everyone, mod], top_role=mod,
        colour=discord.Colour(0xF2B6CD), premium_since=None, timed_out_until=None,
    )
    older = SimpleNamespace(id=501, name="first", bot=False, joined_at=joined - datetime.timedelta(days=9))
    bot = SimpleNamespace(id=502, name="botty", bot=True, joined_at=joined + datetime.timedelta(days=1))
    g = SimpleNamespace(
        id=900, name="Gamer Girls HQ", member_count=43, members=[me, older, bot], roles=[everyone, mod],
        channels=[general, rules], text_channels=[general, rules], voice_channels=[], categories=[],
        emojis=[SimpleNamespace(id=7, name="heart", __str__=None)], stickers=[], icon=None, banner=None,
        description=None, created_at=joined - datetime.timedelta(days=1000), premium_subscription_count=3,
        premium_tier=1, premium_subscribers=[me], owner=me, rules_channel=rules, system_channel=general,
        verification_level=discord.VerificationLevel.high,
    )
    return g, me


def test_plain_and_unknown_placeholders():
    g, me = _guild()
    ctx = RenderContext(guild=g, member=me)
    assert resolve_text("Hi {mention} welcome to {server}!", ctx) == "Hi <@500> welcome to Gamer Girls HQ!"
    assert resolve_text("{nope} {also:nope} {x|y}", ctx) == "{nope} {also:nope} {x|y}"
    assert resolve_text('{"json": true}', ctx) == '{"json": true}'
    assert resolve_text(None, ctx) == ""


def test_member_placeholders():
    g, me = _guild()
    ctx = RenderContext(guild=g, member=me)
    assert resolve_text("{joindays} {joinposition} {joinpositionordinal}", ctx) == "3 2 2nd"
    assert resolve_text("{roles}|{rolenames}|{rolecount}", ctx) == "<@&2>|Moderator|1"
    assert resolve_text("{usercolor} {isbooster} {isbot} {globalname}", ctx) == "#f2b6cd No No Sunny D"
    assert resolve_text("{joindate:R}", ctx).startswith("<t:") and resolve_text("{joindate:R}", ctx).endswith(":R>")
    assert resolve_text("{accountagedays}", ctx) == "403"


def test_server_placeholders():
    g, me = _guild()
    ctx = RenderContext(guild=g)
    assert resolve_text("{membercountordinal} {humancount} {botcount}", ctx) == "43rd 2 1"
    assert resolve_text("{boostsneeded} {boostercount} {ruleschannel}", ctx) == "4 1 <#11>"
    assert resolve_text("{verificationlevel}", ctx) == "High"
    assert resolve_text("{servericon}", ctx) == ""


def test_fallbacks():
    g, me = _guild()
    ctx = RenderContext(guild=g, member=me)
    assert resolve_text("{nickname|user}", ctx) == "Sunny"  # empty -> another placeholder
    assert resolve_text("{nickname|no nickname}", ctx) == "no nickname"  # empty -> literal text
    assert resolve_text("{user|nobody}", ctx) == "Sunny"  # not empty -> value
    assert resolve_text("{channel:nowhere|#general}", ctx) == "#general"  # lookup miss -> fallback


def test_lookups():
    g, me = _guild()
    ctx = RenderContext(guild=g, member=me)
    assert resolve_text("{channel:general}", ctx) == "<#10>"  # part of a decorated name
    assert resolve_text("{channel:11}", ctx) == "<#11>"  # by ID
    assert resolve_text("{role:moderator}", ctx) == "<@&2>"  # case-insensitive
    assert resolve_text("{role:everyone}", ctx) == ""  # never @everyone
    assert resolve_text("{member:first}", ctx) == ""  # no mention attr on the fake -> fails closed to ""
    assert resolve_text("{random:A;B;C}", ctx) in {"A", "B", "C"}
    assert resolve_text("{time:F}", ctx).endswith(":F>")
    assert resolve_text("{time:zz}", ctx).endswith(":f>")  # unknown style -> f
    assert resolve_text("{date:%Y}", ctx) == str(discord.utils.utcnow().year)


def test_failing_placeholder_is_empty(monkeypatch):
    g, me = _guild()
    monkeypatch.setitem(pe._REGISTRY, "boom", lambda ctx: 1 / 0)
    ctx = RenderContext(guild=g, member=me)
    assert resolve_text("[{boom}]", ctx) == "[]"
    assert resolve_text("[{boom|safe}]", ctx) == "[safe]"


def test_moderation_placeholders_and_target_fallback():
    g, _ = _guild()
    actor = SimpleNamespace(id=1, name="mod", display_name="Mod", mention="<@1>",
                            display_avatar=SimpleNamespace(url="https://cdn/m.png"))
    until = discord.utils.utcnow() + datetime.timedelta(minutes=90)
    ctx = RenderContext(guild=g, member=None, actor=actor, extra={
        "action": "Ban", "reason": None, "case_id": 12, "target_id": 777, "duration_minutes": 90,
        "expires_at": until, "delete_days": 0, "warning_count": 0, "source": "dashboard",
    })
    text = "{action} #{caseid}: {mention} ({userid}) by {moderatormention}. {reason}. {duration}. {source}"
    assert resolve_text(text, ctx) == "Ban #12: <@777> (777) by <@1>. No reason given. 1 hour 30 minutes. Dashboard"
    assert resolve_text("{deletedays}/{warningcount}", ctx) == "0/0"  # zero is a value, not empty
    assert resolve_text("{expires}", ctx).endswith(":R>")
    # Outside a moderation log there is no reason at all.
    assert resolve_text("{reason|none}", RenderContext(guild=g)) == "none"


def test_human_duration_and_ordinal():
    assert human_duration(1) == "1 minute"
    assert human_duration(90) == "1 hour 30 minutes"
    assert human_duration(1440) == "1 day"
    assert human_duration(40320) == "4 weeks"
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101, 111)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st", "111th"]


def test_catalogue_metadata_complete():
    entries = pe.catalogue()
    assert len(entries) >= 100
    syntaxes = [e["syntax"] for e in entries]
    assert len(syntaxes) == len(set(syntaxes))
    for e in entries:
        assert e["category"] in pe.CATEGORIES, e
        assert e["description"] and e["where"], e
        name = e["syntax"][1:-1].split(":")[0]
        assert name in pe._REGISTRY or name in pe._ARG_REGISTRY
    # Every registered placeholder is listed (nothing hidden from the editor).
    listed = {e["name"] for e in entries}
    assert set(pe._REGISTRY) <= listed and set(pe._ARG_REGISTRY) <= listed
    # Every category has something in it.
    assert {e["category"] for e in entries} == set(pe.CATEGORIES)


def test_every_catalogue_example_renders_without_error():
    g, me = _guild()
    ctx = pe.sample_context(g, me)
    for e in pe.catalogue():
        resolve_text(e["syntax"], ctx)  # never raises
    assert ctx.actor is me and ctx.extra["sample"] is True and ctx.channel is g.system_channel
    assert resolve_text("{reason} {duration} {caseid}", ctx) == "Breaking rule 3 (example) 1 hour 30 minutes 128"


def test_renderer_clips_to_discord_limits(monkeypatch):
    g, me = _guild()
    monkeypatch.setitem(pe._REGISTRY, "huge", lambda ctx: "x" * 5000)
    rendered = TemplateRenderer().render_message({
        "content": "{huge}",
        "embeds": [{"title": "{huge}", "description": "{huge}", "footer_text": "{huge}", "author_name": "{huge}",
                    "fields": [{"name": "{huge}", "value": "{huge}"}]}],
    }, RenderContext(guild=g, member=me))
    assert len(rendered["content"]) == 2000
    e = rendered["embeds"][0]
    assert len(e.title) == 256 and len(e.description) == 4096 and len(e.footer.text) == 2048
    assert len(e.author.name) == 256 and len(e.fields[0].name) == 256 and len(e.fields[0].value) == 1024


# --------------------------------------------------------------------------- call sites


def _mod_service(monkeypatch):
    from services.moderation_service import ModerationService

    svc = ModerationService(SimpleNamespace(add_action=AsyncMock(return_value=55)), SimpleNamespace(log=AsyncMock()))
    sent = []

    async def capture(guild, template_name, channel_id, fallback, member=None, actor=None, extra=None):
        sent.append({"template": template_name, "member": member, "extra": extra})

    monkeypatch.setattr(svc, "send_mod_log", capture)
    return svc, sent


def _target():
    t = SimpleNamespace(id=777, top_role=SimpleNamespace(position=1), roles=[], name="t", display_name="T")
    t.kick, t.timeout = AsyncMock(), AsyncMock()
    return t


async def test_mod_logs_get_placeholder_data(monkeypatch):
    svc, sent = _mod_service(monkeypatch)
    g = SimpleNamespace(id=900, ban=AsyncMock(), unban=AsyncMock())
    mod = SimpleNamespace(id=1)
    await svc.timeout(_target(), g, mod, 90, "spam", log_channel_id=5, log_extra={"source": "dashboard"})
    await svc.ban(g, 777, mod, None, log_channel_id=5, delete_message_days=2)
    await svc.unban(g, 777, mod, "appeal", log_channel_id=5)
    await svc.untimeout(_target(), g, mod, None, log_channel_id=5)
    await svc.kick(_target(), g, mod, "bye", log_channel_id=5, log_extra={"kick_number": 9, "source": "command"})
    by = {s["template"]: s["extra"] for s in sent}
    t = by["mod-timeout-log"]
    assert t["action"] == "Timeout" and t["reason"] == "spam" and t["duration_minutes"] == 90
    assert t["case_id"] == 55 and t["source"] == "dashboard" and isinstance(t["expires_at"], datetime.datetime)
    b = by["mod-ban-log"]
    assert b["action"] == "Ban" and b["reason"] is None and b["target_id"] == 777 and b["delete_days"] == 2
    assert by["mod-unban-log"]["target_id"] == 777 and by["mod-unban-log"]["action"] == "Unban"
    assert by["mod-untimeout-log"]["action"] == "Timeout removed"
    k = by["mod-kick-log"]
    assert k["action"] == "Kick" and k["kick_number"] == 9 and k["reason"] == "bye" and k["source"] == "command"


async def test_send_mod_log_renders_extras(monkeypatch):
    from services import moderation_service as ms

    class FakeChannel(SimpleNamespace):
        pass

    monkeypatch.setattr(ms.discord, "TextChannel", FakeChannel)
    channel = FakeChannel(id=5, mention="<#5>", send=AsyncMock())
    templates = SimpleNamespace(get_full=AsyncMock(return_value={
        "content": None, "embeds": [{"description": "{action}: {mention} - {reason|No reason}", "fields": []}],
    }))
    svc = ms.ModerationService(SimpleNamespace(), None, templates, TemplateRenderer())
    g = SimpleNamespace(id=900, get_channel=lambda cid: channel)
    await svc.send_mod_log(g, "mod-ban-log", 5, discord.Embed(description="fallback"),
                           member=None, actor=None, extra={"action": "Ban", "reason": "raid", "target_id": 777})
    embed = channel.send.call_args.kwargs["embeds"][0]
    assert embed.description == "Ban: <@777> - raid"


async def test_verify_message_has_verifier(monkeypatch):
    from services import verify_service

    captured = {}

    class Renderer:
        def render_message(self, template, ctx):
            captured["ctx"] = ctx
            return {"content": "hi", "embeds": []}

    templates = SimpleNamespace(get_full=AsyncMock(return_value={"content": "x", "embeds": []}))
    member = SimpleNamespace(guild=SimpleNamespace(id=900))
    channel = SimpleNamespace(send=AsyncMock())
    actor = SimpleNamespace(id=1)
    await verify_service._send_verify_message(member, channel, templates, Renderer(), actor=actor, source="button")
    assert captured["ctx"].actor is actor and captured["ctx"].extra == {"source": "button"}


async def test_internal_placeholders_route_renders_examples():
    from internal_api.routes.templates import list_placeholders

    g, me = _guild()
    g.get_member = lambda uid: me if uid == 500 else None
    out = await list_placeholders(user_id=500, guild=g)
    assert out["sample_member"] is True and out["categories"] == list(pe.CATEGORIES)
    by = {p["syntax"]: p for p in out["placeholders"]}
    assert by["{mention}"]["example"] == "<@500>"
    assert by["{reason}"]["example"] == "Breaking rule 3 (example)"
    assert by["{channel:general}"]["example"] == "<#10>"
    out = await list_placeholders(user_id=None, guild=g)
    assert out["sample_member"] is False


def test_dashboard_placeholders_route(monkeypatch):
    from fastapi.testclient import TestClient

    from server import config
    from server.app import app
    from server.auth.dependencies import get_current_session, require_session
    from server.db.models import Session
    from server.services import internal_client

    sess = Session()
    sess.session_id, sess.discord_user_id, sess.discord_username = "sid", 4242, "u"
    sess.is_guild_member, sess.cached_role_ids = True, []
    calls = {}

    async def check_permission(guild_id, user_id, perm):
        calls["perm"] = perm
        return calls.get("allowed", True)

    async def list_placeholders(guild_id, *, user_id=None):
        calls["user_id"] = user_id
        return {"categories": [], "placeholders": [], "sample_member": True}

    monkeypatch.setattr(internal_client, "check_permission", check_permission)
    monkeypatch.setattr(internal_client, "list_placeholders", list_placeholders)
    app.dependency_overrides[get_current_session] = lambda: sess
    app.dependency_overrides[require_session] = lambda: sess
    try:
        client = TestClient(app)
        r = client.get("/api/templates/placeholders")
        assert r.status_code == 200 and calls == {"perm": "manage_templates", "user_id": 4242}
        calls["allowed"] = False
        assert client.get("/api/templates/placeholders").status_code == 403
    finally:
        app.dependency_overrides.clear()
