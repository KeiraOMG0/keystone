"""Bot-wide safety defaults: global allowed_mentions and the guild-only command
tree (core/bot_factory.py), the prefix-command error handler
(core/command_errors.py via cogs/diagnostics_collector.py), music's no-ping
sends and template posts without @everyone. No network."""

from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord.ext import commands

from core import bot_factory
from core.command_errors import GENERIC_FAILURE, handle_command_error

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# bot construction
# ---------------------------------------------------------------------------

async def test_bot_has_safe_allowed_mentions_and_guild_only_tree():
    bot = bot_factory.create_bot(discord.Intents.none())
    try:
        am = bot.allowed_mentions
        assert am is not None
        assert am.everyone is False
        assert am.roles is True and am.users is True and am.replied_user is True
        assert isinstance(bot.tree, bot_factory.GuildOnlyCommandTree)
        ctx = bot.tree.allowed_contexts
        assert ctx.guild is True and ctx.dm_channel is False and ctx.private_channel is False
        assert bot.command_prefix == "!"
    finally:
        await bot.close()


async def test_main_uses_the_factory():
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2].joinpath("main.py").read_text(encoding="utf-8")
    assert "create_bot(intents)" in src
    assert "commands.Bot(" not in src


def _interaction(guild, itype=discord.InteractionType.application_command):
    return NS(
        guild=guild, guild_id=getattr(guild, "id", None), type=itype,
        response=NS(send_message=AsyncMock(), autocomplete=AsyncMock(), is_done=lambda: False),
    )


async def test_tree_refuses_dm_interactions_ephemerally():
    bot = bot_factory.create_bot(discord.Intents.none())
    try:
        dm = _interaction(None)
        assert await bot.tree.interaction_check(dm) is False
        dm.response.send_message.assert_awaited_once_with(bot_factory.DM_REFUSAL, ephemeral=True)

        auto = _interaction(None, discord.InteractionType.autocomplete)
        assert await bot.tree.interaction_check(auto) is False
        auto.response.autocomplete.assert_awaited_once_with([])
        auto.response.send_message.assert_not_awaited()

        in_guild = _interaction(NS(id=1))
        assert await bot.tree.interaction_check(in_guild) is True
        in_guild.response.send_message.assert_not_awaited()
    finally:
        await bot.close()


# ---------------------------------------------------------------------------
# prefix-command error handler
# ---------------------------------------------------------------------------

def _ctx(command=None, cog=None):
    return NS(command=command, cog=cog, send=AsyncMock(), clean_prefix="!", prefix="!")


def _command(name="kick", signature="<member> [reason]", has_handler=False):
    return NS(qualified_name=name, signature=signature, has_error_handler=lambda: has_handler)


async def test_command_not_found_is_ignored():
    ctx = _ctx()
    assert await handle_command_error(ctx, commands.CommandNotFound("nope")) == "ignored"
    ctx.send.assert_not_awaited()


async def test_user_errors_get_a_short_reply_without_pings():
    param = NS(name="member", displayed_name="member")
    cases = [
        commands.MissingRequiredArgument(param),
        commands.BadArgument("Member \"@everyone\" not found."),
        commands.CheckFailure("nope"),
        commands.CommandOnCooldown(commands.Cooldown(1, 10), 7.2, commands.BucketType.user),
    ]
    for err in cases:
        ctx = _ctx(_command())
        assert await handle_command_error(ctx, err) == "user_error", err
        text = ctx.send.await_args.args[0]
        assert text and len(text) < 300
        assert ctx.send.await_args.kwargs["allowed_mentions"].everyone is False
        assert "@everyone" not in text  # escaped
    ctx = _ctx(_command())
    await handle_command_error(ctx, cases[0])
    assert "`member`" in ctx.send.await_args.args[0] and "!kick <member> [reason]" in ctx.send.await_args.args[0]


async def test_unexpected_errors_log_traceback_and_reply(caplog):
    ctx = _ctx(_command())
    try:
        raise ValueError("boom")
    except ValueError as exc:
        err = commands.CommandInvokeError(exc)
    with caplog.at_level("ERROR", logger="commands.errors"):
        assert await handle_command_error(ctx, err) == "logged"
    assert ctx.send.await_args.args[0] == GENERIC_FAILURE
    rec = next(r for r in caplog.records if r.name == "commands.errors")
    assert rec.exc_info and rec.exc_info[0] is ValueError


async def test_command_with_own_handler_is_left_alone():
    ctx = _ctx(_command(has_handler=True))
    assert await handle_command_error(ctx, commands.CommandOnCooldown(commands.Cooldown(1, 1), 1, commands.BucketType.user)) == "delegated"
    ctx.send.assert_not_awaited()


async def test_diagnostics_listener_counts_and_handles():
    from cogs.diagnostics_collector import DiagnosticsCollector

    collector = DiagnosticsCollector(MagicMock())
    ctx = _ctx(_command())
    await collector.on_command_error(ctx, commands.CheckFailure("x"))
    assert collector.commands_err["kick"] == 1
    assert collector.recent_errors[-1]["error"] == "CheckFailure"
    ctx.send.assert_awaited_once()


# ---------------------------------------------------------------------------
# music: echoed text never pings
# ---------------------------------------------------------------------------

async def test_music_mute_mentions_defaults_to_no_pings():
    from cogs.music import NO_PINGS, mute_mentions, safe_text

    sent = []

    async def send(*args, **kwargs):
        sent.append(kwargs)

    ctx = NS(send=send)
    mute_mentions(ctx)
    mute_mentions(ctx)  # idempotent: not double-wrapped
    await ctx.send("Loaded **@everyone**")
    assert sent[-1]["allowed_mentions"] is NO_PINGS
    explicit = discord.AllowedMentions(users=True)
    await ctx.send("x", allowed_mentions=explicit)
    assert sent[-1]["allowed_mentions"] is explicit
    assert "@everyone" not in safe_text("@everyone") and "@here" not in safe_text("@here")


async def test_music_cog_mutes_every_command_context():
    from cogs.music import MusicCog

    assert "cog_before_invoke" in MusicCog.__dict__


# ---------------------------------------------------------------------------
# templates: never @everyone/@here
# ---------------------------------------------------------------------------

async def test_template_post_suppresses_everyone():
    from services.managed_message_service import ManagedMessageService

    templates = NS(get_by_name=AsyncMock(return_value={"id": 1}), get_full=AsyncMock(return_value={}))
    renderer = NS(render_message=lambda t, ctx: {"content": "@everyone hi", "embeds": []})
    managed = NS(get_components=AsyncMock(return_value=[]), create=AsyncMock())
    svc = ManagedMessageService(managed, templates, renderer, MagicMock())
    channel = NS(id=5, send=AsyncMock(return_value=NS(id=9)))
    msg_id, errors = await svc.post(NS(id=1), channel, "t", 2)
    assert errors == [] and msg_id == 9
    am = channel.send.await_args.kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is True and am.users is True
