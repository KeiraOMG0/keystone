"""!musicstatus: open to everyone, staff-only detail, and output sanitisation."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("INTERNAL_API_SECRET", "test-secret")

from cogs import music  # noqa: E402
from cogs.music import MusicCog, build_music_status, sanitize_status_text  # noqa: E402

SECRET_PATH = r"C:\Users\Keira\Desktop\python\kick-bot\cookies.txt"
STATUS = {
    "enabled": True,
    "state": "running",
    "responding": True,
    "pid": 4242,
    "adopted_external": False,
    "url": "http://127.0.0.1:4416",
    "uptime_seconds": 3725.4,
    "restart_count": 2,
    "watchdog": True,
    "watchdog_active": True,
    "last_error": rf"script not found at {SECRET_PATH} token=abc123 /home/bot/pot_server/main.js",
    "log_path": r"C:\Users\Keira\Desktop\python\kick-bot\logs\pot_server.log",
    "log_tail": [
        rf"error reading {SECRET_PATH}",
        "Authorization: Bearer supersecretvalue",
        "line with ``` fence and @everyone",
    ],
    "cookies": {"present": True, "path": SECRET_PATH, "age_hours": 5.5},
    "node_version": "v22.1.0",
    "ytdlp_version": "2026.09.01",
    "bgutil_plugin_version": "1.2.3",
}


def _no_leaks(text: str) -> None:
    for bad in ("C:\\", "Users", "Keira", "/home/", "abc123", "supersecretvalue", "4242", "127.0.0.1", "4416"):
        assert bad not in text, f"leaked {bad!r}"


# -- pure formatting ---------------------------------------------------------


def test_public_view_has_basics_and_no_sensitive_detail():
    text = build_music_status(STATUS, staff=False, ping_ms=42, now_playing="Song", queue_count=3, bot_uptime=90061)
    assert "running" in text and "Restarts: 2" in text
    assert "1h 2m 5s" in text  # pot server uptime
    assert "1d 1h 1m 1s" in text  # bot uptime
    assert "present (cookies.txt), 5.5h old" in text
    assert "v22.1.0" in text and "2026.09.01" in text and "1.2.3" in text
    assert "Staff details" not in text and "Log tail" not in text and "Last error" not in text
    _no_leaks(text)


def test_public_view_cookies_missing():
    st = {**STATUS, "cookies": {"present": False, "path": SECRET_PATH, "age_hours": None}}
    text = build_music_status(st, staff=False)
    assert "Cookies: MISSING" in text
    _no_leaks(text)


def test_staff_view_is_still_sanitised():
    text = build_music_status(STATUS, staff=True, log_lines=5)
    assert "Staff details" in text and "Log tail (pot_server.log)" in text
    assert "pid: `4242`" in text and "127.0.0.1:4416" in text  # staff may see these
    for bad in ("C:\\", "Users", "Keira", "/home/", "abc123", "supersecretvalue"):
        assert bad not in text
    assert "cookies.txt" in text and "[redacted]" in text
    assert "@everyone" not in text
    assert text.count("```") == 2


def test_staff_view_fits_discord_limit_and_keeps_fence_balanced():
    st = {**STATUS, "log_tail": ["x" * 400] * 20}
    text = build_music_status(st, staff=True, log_lines=20)
    assert len(text) <= 2000
    assert text.count("```") % 2 == 0


def test_log_tail_limited_to_a_few_lines():
    st = {**STATUS, "log_tail": [f"line {i}" for i in range(30)]}
    text = build_music_status(st, staff=True, log_lines=5)
    assert "line 29" in text and "line 24" not in text


@pytest.mark.parametrize(
    "raw, must_not_contain",
    [
        (r"open C:\Program Files\nodejs\node.exe failed", "Program Files"),
        ("cannot read /var/lib/bot/cookies.txt now", "/var/lib"),
        ("cookie=SID123 rest", "SID123"),
        ("password: hunter2", "hunter2"),
        ("@everyone hi", "@everyone"),
        ("a `code` b", "`"),
    ],
)
def test_sanitize_status_text(raw, must_not_contain):
    assert must_not_contain not in sanitize_status_text(raw)


def test_sanitize_truncates_and_flattens_newlines():
    out = sanitize_status_text("a\nb" * 200, limit=50)
    assert "\n" not in out and len(out) <= 50


# -- command permission behaviour ----------------------------------------------


class FakeCtx:
    def __init__(self, author, guild=object()):
        self.author = author
        self.guild = guild
        self.reply = AsyncMock()


def _member():
    m = MagicMock(spec=discord.Member)
    m.id = 1
    m.roles = []
    return m


@pytest.fixture
async def cog():
    perms = SimpleNamespace(has_permission=AsyncMock(return_value=False))
    svc = SimpleNamespace(status=AsyncMock(return_value=dict(STATUS)))
    bot = SimpleNamespace(permission_service=perms, pot_server_service=svc, latency=0.05, voice_clients=[])
    c = MusicCog(bot)
    yield c
    c.inactivity_check.cancel()


async def _run(cog, ctx):
    await MusicCog.musicstatus_cmd.callback(cog, ctx)
    return ctx.reply.await_args.args[0]


async def test_non_staff_member_gets_public_status(cog):
    ctx = FakeCtx(_member())
    text = await _run(cog, ctx)
    assert "**Music status**" in text and "Staff details" not in text
    _no_leaks(text)
    cog.bot.pot_server_service.status.assert_awaited_once_with(log_lines=0)
    assert ctx.reply.await_args.kwargs["allowed_mentions"].everyone is False


async def test_staff_gets_detail_section(cog):
    cog.bot.permission_service.has_permission.return_value = True
    text = await _run(cog, FakeCtx(_member()))
    cog.bot.permission_service.has_permission.assert_awaited_once()
    assert cog.bot.permission_service.has_permission.await_args.args[1] == "staff_base"
    assert "Staff details" in text and "Log tail" in text


async def test_dm_user_gets_public_status_without_permission_lookup(cog):
    text = await _run(cog, FakeCtx(MagicMock(spec=discord.User), guild=None))
    assert "Staff details" not in text
    cog.bot.permission_service.has_permission.assert_not_awaited()
    _no_leaks(text)


async def test_permission_service_error_degrades_to_public(cog):
    cog.bot.permission_service.has_permission.side_effect = RuntimeError("db down")
    text = await _run(cog, FakeCtx(_member()))
    assert "Staff details" not in text


async def test_command_is_public_and_has_status_alias():
    assert MusicCog.musicstatus_cmd.name == "musicstatus"
    assert "status" in MusicCog.musicstatus_cmd.aliases
    assert not MusicCog.musicstatus_cmd.hidden
    assert not MusicCog.musicstatus_cmd.checks


async def test_missing_pot_service_message(cog):
    cog.bot.pot_server_service = None
    ctx = FakeCtx(_member())
    await MusicCog.musicstatus_cmd.callback(cog, ctx)
    assert "not available" in ctx.reply.await_args.args[0]


def test_module_exports():
    assert music.fmt_uptime(None) == "n/a" and music.fmt_uptime(5) == "5s"
