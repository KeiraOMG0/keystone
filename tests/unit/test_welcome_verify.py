"""`!welcome-verify` / `!wv` (cogs/verify.py): staff-only, only inside an open
verify ticket, pings the ticket opener with the `welcome-verify` template (or a
plain fallback), then deletes the command message. Fake discord objects only."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.verify import WELCOME_VERIFY_TEMPLATE_NAME, VerifyCog

GUILD_ID = 100000000000000001
OPENER = 123456789012345678
STAFF = 123456789012345679
CHANNEL = 100000000000000002


class FakeTickets:
    def __init__(self, ticket=None, purpose="verify"):
        self.ticket = ticket
        self.purpose = purpose

    async def get_ticket_by_channel(self, channel_id):
        return self.ticket if self.ticket and self.ticket["channel_id"] == channel_id else None

    async def get_panel(self, panel_id):
        return {"id": panel_id, "purpose": self.purpose}


class FakeTemplates:
    def __init__(self, full):
        self.full = full
        self.asked = []

    async def get_full(self, guild_id, name):
        self.asked.append(name)
        return self.full


class FakeRenderer:
    def __init__(self, fail=False):
        self.fail = fail
        self.ctx = None

    def render_message(self, template_full, ctx):
        if self.fail:
            raise ValueError("bad template")
        self.ctx = ctx
        return {"content": None, "embeds": [discord.Embed(title="How to verify")]}


def _ticket(status="open"):
    return {"id": 1, "panel_id": 1, "channel_id": CHANNEL, "opened_by": OPENER,
            "status": status, "ticket_number": 7}


def _make(*, allowed=True, ticket=None, purpose="verify", template=object(), fail_render=False, opener_here=True):
    opener = SimpleNamespace(id=OPENER, mention=f"<@{OPENER}>")
    guild = SimpleNamespace(id=GUILD_ID, get_member=lambda uid: opener if opener_here and uid == OPENER else None)
    ctx = SimpleNamespace(
        guild=guild,
        author=SimpleNamespace(id=STAFF),
        channel=SimpleNamespace(id=CHANNEL),
        reply=AsyncMock(),
        send=AsyncMock(),
        message=SimpleNamespace(delete=AsyncMock()),
    )
    perms = SimpleNamespace(has_permission=AsyncMock(return_value=allowed))
    bot = SimpleNamespace(permission_service=perms)
    templates = FakeTemplates(None if template is None else {"name": WELCOME_VERIFY_TEMPLATE_NAME})
    renderer = FakeRenderer(fail=fail_render)
    cog = VerifyCog(bot, templates, renderer, audit=None, ticket_repo=FakeTickets(ticket, purpose))
    return cog, ctx, perms, templates, renderer, opener


async def _run(cog, ctx):
    await type(cog).welcome_verify_cmd.callback(cog, ctx)


async def test_posts_template_pinging_only_the_opener_and_deletes_command():
    cog, ctx, perms, templates, renderer, opener = _make(ticket=_ticket())
    await _run(cog, ctx)
    perms.has_permission.assert_awaited_once_with(ctx.author, "verify_members")
    assert templates.asked == [WELCOME_VERIFY_TEMPLATE_NAME]
    assert renderer.ctx.member is opener and renderer.ctx.actor is ctx.author and renderer.ctx.ticket_number == 7
    kwargs = ctx.send.await_args.kwargs
    assert kwargs["embeds"][0].title == "How to verify"
    am = kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is False and am.users == [opener]
    ctx.message.delete.assert_awaited_once()
    ctx.reply.assert_not_awaited()


@pytest.mark.parametrize("template,fail", [(None, False), (object(), True)])
async def test_falls_back_to_plain_memo(template, fail):
    cog, ctx, *_ = _make(ticket=_ticket(), template=template, fail_render=fail)
    await _run(cog, ctx)
    kwargs = ctx.send.await_args.kwargs
    assert kwargs["content"].startswith(f"Hi <@{OPENER}>!") and "GG HQ" in kwargs["content"]
    assert kwargs["embeds"] == []


async def test_needs_verify_members():
    cog, ctx, *_ = _make(allowed=False, ticket=_ticket())
    await _run(cog, ctx)
    ctx.send.assert_not_awaited()
    assert "permission" in ctx.reply.await_args.args[0]


@pytest.mark.parametrize("ticket,purpose", [(None, "verify"), (_ticket(), "support"), (_ticket("closed"), "verify")])
async def test_only_in_an_open_verify_ticket(ticket, purpose):
    cog, ctx, *_ = _make(ticket=ticket, purpose=purpose)
    await _run(cog, ctx)
    ctx.send.assert_not_awaited()
    assert ctx.reply.await_args.args[0] == "Use this inside an open verify ticket."


async def test_opener_left():
    cog, ctx, *_ = _make(ticket=_ticket(), opener_here=False)
    await _run(cog, ctx)
    ctx.send.assert_not_awaited()
    assert "no longer in the server" in ctx.reply.await_args.args[0]


async def test_registered_as_wv_alias():
    cmd = VerifyCog.welcome_verify_cmd
    assert cmd.name == "welcome-verify" and "wv" in cmd.aliases
