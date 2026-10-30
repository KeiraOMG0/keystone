"""/help Music section and /devhelp extras are derived from the registered commands."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("INTERNAL_API_SECRET", "test-secret")

from cogs.help_commands import build_dev_extra_pages, build_music_pages  # noqa: E402
from cogs.music import MusicCog  # noqa: E402


class FakeTree:
    def walk_commands(self):
        return iter(())


@pytest.fixture
async def bot():
    b = SimpleNamespace(command_prefix="!", voice_clients=[], tree=FakeTree())
    cog = MusicCog(b)
    b.get_cog = lambda name: cog if name == "Music" else None
    yield b
    cog.inactivity_check.cancel()


async def test_music_page_lists_registered_commands_with_staff_marks(bot):
    pages = build_music_pages(bot)
    text = "\n".join(p["description"] for p in pages)
    for cmd in ("play", "skip", "queue", "autoplay", "musicstatus", "status", "debugurl"):
        assert f"`!{cmd}" in text or f"`!{cmd}`" in text
    assert "**(staff)**" in text and "`!musichelp`" not in text.split("**(staff)**")[0].splitlines()[-1]
    assert "!musichelp" in text and "/music help" in text
    assert not any(l.startswith("`!play") and "(staff)" in l for l in text.splitlines())
    assert "Alias for" not in text  # duplicate alias command hidden
    assert all(len(p["description"]) <= 4096 for p in pages)


async def test_music_pages_empty_when_cog_missing():
    b = SimpleNamespace(command_prefix="!", get_cog=lambda n: None, tree=FakeTree())
    assert build_music_pages(b) == []


async def test_dev_pages_cover_diagnostics_honeypot_and_tooling(bot):
    pages = build_dev_extra_pages(bot)
    titles = [p["title"] for p in pages]
    assert titles[:3] == ["Diagnostics & debug commands", "Honeypot admin commands", "Internal tooling"]
    assert "`!debugurl" in pages[0]["description"]
    assert all(len(p["description"]) <= 4096 for p in pages)
