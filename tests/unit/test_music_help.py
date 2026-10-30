"""!musichelp and /music help are generated from the cog's registered commands."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("INTERNAL_API_SECRET", "test-secret")

from cogs.music import (  # noqa: E402
    MUSIC_HELP_CATEGORIES,
    MusicCog,
    render_command_help,
    render_music_help,
)


@pytest.fixture
async def cog():
    c = MusicCog(SimpleNamespace(command_prefix="!", voice_clients=[]))
    yield c
    c.inactivity_check.cancel()


class Ctx:
    def __init__(self):
        self.send = AsyncMock()

    def texts(self):
        return [c.args[0] for c in self.send.await_args_list]


def test_every_categorised_name_is_a_real_command():
    real = {c.name for c in MusicCog.__cog_commands__}
    for _, names in MUSIC_HELP_CATEGORIES:
        for n in names:
            assert n in real, n


async def test_overview_lists_categories_aliases_usage_and_staff_marks(cog):
    text = cog.help_text()
    for heading in ("Voice & Playback", "Queue", "Autoplay & Loop", "Playlists & History", "Status & Debug"):
        assert f"**{heading}**" in text
    assert "`!play <search>`" in text
    assert "`!autoplay [on|off]`" in text
    assert "(also `!status`)" in text and "(also `!mhelp`)" in text
    lines = text.splitlines()
    assert any(l.startswith("`!debugurl") and "**(staff)**" in l for l in lines)
    assert not any(l.startswith("`!play") and "(staff)" in l for l in lines)
    assert "Cookie auto-refresh: off" in text
    assert "/music help" in text and "!musichelp" in text
    assert "Alias for" not in text and "removequeue" not in text


async def test_new_registered_command_appears_automatically(cog):
    async def fake(ctx):
        """Do a fake thing. Usage: !fakething [x]"""

    cmd = commands.Command(fake, name="fakething", aliases=["ft"], extras={"staff_only": True})
    orig = cog.get_commands()
    cog.get_commands = lambda: [*orig, cmd]  # what registering a new command does
    text = cog.help_text()
    assert "fakething" in cog.help_text("ft") and "(staff only)" in cog.help_text("fakething")
    assert "**Other**" in text
    assert any(l.startswith("`!fakething [x]`") and "(also `!ft`)" in l and "(staff)" in l for l in text.splitlines())
    assert "Do a fake thing." in text


async def test_per_command_detail_by_name_alias_and_prefix(cog):
    for q in ("debugurl", "!debugurl", "DEBUGURL"):
        out = cog.help_text(q)
        assert "**!debugurl**" in out and "(staff only)" in out and "--full" in out
    out = cog.help_text("status")
    assert "**!musicstatus**" in out and "Aliases: `!status`" in out
    assert "Usage: `!play <search>`" in cog.help_text("play")


async def test_unknown_command_message_is_sanitised(cog):
    out = cog.help_text("nope @everyone")
    assert out.startswith("Unknown music command") and "@everyone" not in out and "!musichelp" in out


async def test_prefix_command_chunks_within_limit_and_no_mentions(cog):
    ctx = Ctx()
    await MusicCog.musichelp_cmd.callback(cog, ctx)
    assert ctx.texts() and all(len(t) <= 2000 for t in ctx.texts())
    for call in ctx.send.await_args_list:
        assert isinstance(call.kwargs["allowed_mentions"], discord.AllowedMentions)
    await MusicCog.musichelp_cmd.callback(cog, ctx, command="skip")
    assert "**!skip**" in ctx.texts()[-1]


async def test_long_help_is_split_not_truncated(cog, monkeypatch):
    monkeypatch.setattr(MusicCog, "_help_notes", lambda self: ["x" * 900, "y" * 900, "z" * 900])
    ctx = Ctx()
    await MusicCog.musichelp_cmd.callback(cog, ctx)
    assert len(ctx.texts()) > 1 and all(len(t) <= 2000 for t in ctx.texts())
    assert "z" * 900 in "".join(ctx.texts())


async def test_cookie_note_reflects_listener_state(cog):
    cog.cookie_listener = SimpleNamespace(running=True)
    assert "active" in cog.help_text()
    cog.cookie_listener = SimpleNamespace(running=False)
    assert "waiting" in cog.help_text()


def test_slash_group_registered_with_help_and_autocomplete():
    group = MusicCog.music_group
    assert isinstance(group, app_commands.Group) and group.name == "music"
    sub = {c.name: c for c in group.commands}
    assert "help" in sub
    assert "command" in sub["help"]._params and sub["help"]._params["command"].autocomplete


async def test_slash_help_is_ephemeral_and_matches_prefix_output(cog):
    inter = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    await MusicCog.music_help_slash.callback(cog, inter, "debugurl")
    kw = inter.response.send_message.await_args
    assert kw.kwargs["ephemeral"] is True and "**!debugurl**" in kw.args[0]
    inter2 = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    await MusicCog.music_help_slash.callback(cog, inter2, None)
    assert inter2.response.send_message.await_args.args[0].startswith("**Music help**")


async def test_autocomplete_lists_registered_names_and_aliases(cog):
    inter = SimpleNamespace()
    got = await MusicCog.music_help_autocomplete(cog, inter, "sta")
    assert {c.value for c in got} >= {"status"}
    got = await MusicCog.music_help_autocomplete(cog, inter, "")
    assert 0 < len(got) <= 25
    assert {c.value for c in await MusicCog.music_help_autocomplete(cog, inter, "!mh")} == {"mhelp"}


def test_no_help_alias_collision_with_discordpys_default_help():
    assert MusicCog.musichelp_cmd.aliases == ["mhelp"]
    assert "help" not in {c.name for c in MusicCog.__cog_commands__}
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    assert bot.get_command("help") is not None  # discord.py's default help stays untouched


def test_render_command_help_handles_no_usage_line():
    async def f(ctx, a):
        """Plain description."""
    cmd = commands.Command(f, name="plain")
    out = render_command_help(cmd, "!")
    assert "Usage: `!plain <a>`" in out and "Plain description." in out
