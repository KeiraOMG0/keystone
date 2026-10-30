"""Autoplay: candidate selection, dedupe, consecutive cap, stop conditions, intentional_stop."""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("INTERNAL_API_SECRET", "test-secret")

from cogs import music  # noqa: E402
from cogs.music import (  # noqa: E402
    AutoplayState,
    MusicCog,
    autoplay_block_reason,
    build_music_status,
    extract_video_id,
    select_autoplay_candidates,
)

SEED = "AAAAAAAAAAA"
SEED_URL = f"https://www.youtube.com/watch?v={SEED}"


def vid(n: int) -> str:
    return f"vid{n:08d}"[:11]


def entries(*ids):
    return [{"id": i, "title": f"t-{i}", "url": f"https://www.youtube.com/watch?v={i}"} for i in ids]


# -- pure selection ------------------------------------------------------------


def test_extract_video_id():
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=3") == "dQw4w9WgXcQ"
    assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RD") == "dQw4w9WgXcQ"
    assert extract_video_id("https://example.com/x") is None


def test_select_skips_seed_recent_and_dupes_and_respects_limit():
    ents = entries(SEED, "B" * 11, "B" * 11, "C" * 11, "D" * 11, "E" * 11)
    ents.insert(1, {"id": None, "title": "no id"})
    got = select_autoplay_candidates(ents, {"C" * 11}, SEED, limit=2)
    assert [u.rsplit("=", 1)[1] for u, _ in got] == ["B" * 11, "D" * 11]


def test_select_returns_nothing_when_everything_was_recent():
    assert select_autoplay_candidates(entries("B" * 11, "C" * 11), {"B" * 11, "C" * 11}, SEED, 3) == []


def test_history_is_bounded_and_records_ids():
    st = AutoplayState(True, history=3)
    for i in range(5):
        st.record_played(f"https://www.youtube.com/watch?v={chr(65 + i) * 11}", autoplay=True)
    assert list(st.recent) == ["C" * 11, "D" * 11, "E" * 11]
    assert st.consecutive == 5
    st.record_played(f"https://www.youtube.com/watch?v={'Z' * 11}", autoplay=False)
    assert st.consecutive == 0


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({}, None),
        ({"enabled": False}, "disabled"),
        ({"intentional": True}, "intentional_stop"),
        ({"humans": 0}, "no_listeners"),
        ({"consecutive": 10}, "cap"),
        ({"consecutive": 9}, None),
        ({"has_seed": False}, "no_seed"),
    ],
)
def test_block_reasons(kw, expected):
    base = dict(enabled=True, intentional=False, humans=2, consecutive=0, max_consecutive=10, has_seed=True)
    assert autoplay_block_reason(**{**base, **kw}) == expected


# -- cog integration -------------------------------------------------------------


def make_vc(humans=1, bots=1):
    members = [SimpleNamespace(bot=False) for _ in range(humans)] + [SimpleNamespace(bot=True) for _ in range(bots)]
    vc = MagicMock()
    vc.guild = SimpleNamespace(id=42)
    vc.channel = SimpleNamespace(members=members)
    vc.is_connected.return_value = True
    vc.is_playing.return_value = False
    vc.is_paused.return_value = False
    return vc


@pytest.fixture
async def cog(monkeypatch):
    monkeypatch.setattr(music._config, "MUSIC_AUTOPLAY_MAX_CONSECUTIVE", 4)
    monkeypatch.setattr(music._config, "MUSIC_AUTOPLAY_HISTORY", 5)
    bot = SimpleNamespace(voice_clients=[], latency=0.05)
    c = MusicCog(bot)
    c.previous_song = (SEED_URL, "seed")
    c._autoplay_for(42).enabled = True
    c._extract_info = AsyncMock(return_value={"entries": entries("B" * 11, "C" * 11, "D" * 11, "E" * 11)})
    yield c
    c.inactivity_check.cancel()


def ctx():
    return SimpleNamespace(send=AsyncMock(), author=SimpleNamespace(id=1))


async def test_refill_queues_batch_and_marks_autoplay(cog):
    c = ctx()
    n = await cog._autoplay_refill(c, make_vc())
    assert n == 3 == len(cog.song_queue)
    assert {u for u, _ in cog.song_queue} == cog._autoplay_for(42).auto_urls
    assert "list=RD" + SEED in cog._extract_info.await_args.args[1]


async def test_refill_avoids_recent_history(cog):
    st = cog._autoplay_for(42)
    for i in "BCD":
        st.record_played(f"https://www.youtube.com/watch?v={i * 11}", autoplay=True)
    st.consecutive = 0
    await cog._autoplay_refill(ctx(), make_vc())
    assert [u.rsplit("=", 1)[1] for u, _ in cog.song_queue] == ["E" * 11]


async def test_refill_capped_by_remaining_consecutive_budget(cog):
    cog._autoplay_for(42).consecutive = 3  # max is 4 -> one left
    assert await cog._autoplay_refill(ctx(), make_vc()) == 1


async def test_cap_stops_autoplay_and_announces_once(cog):
    st = cog._autoplay_for(42)
    st.consecutive = 4
    c = ctx()
    assert await cog._autoplay_refill(c, make_vc()) == 0
    assert await cog._autoplay_refill(c, make_vc()) == 0
    cog._extract_info.assert_not_awaited()
    assert c.send.await_count == 1 and "paused" in c.send.await_args.args[0]


async def test_no_humans_in_channel_stops(cog):
    assert await cog._autoplay_refill(ctx(), make_vc(humans=0, bots=2)) == 0
    cog._extract_info.assert_not_awaited()


async def test_disabled_does_nothing(cog):
    cog._autoplay_for(42).enabled = False
    assert await cog._autoplay_refill(ctx(), make_vc()) == 0
    cog._extract_info.assert_not_awaited()


async def test_intentional_stop_flag_blocks_refill(cog):
    cog._intentional_stop = True
    assert await cog._autoplay_refill(ctx(), make_vc()) == 0
    cog._extract_info.assert_not_awaited()


async def test_play_next_only_autoplays_when_allowed(cog):
    """skip/stop/leave/error call play_next without allow_autoplay: never a surprise track."""
    cog._autoplay_refill = AsyncMock(return_value=0)
    await cog.play_next(ctx(), make_vc())  # default: not allowed
    cog._autoplay_refill.assert_not_awaited()
    await cog.play_next(ctx(), make_vc(), allow_autoplay=True)
    cog._autoplay_refill.assert_awaited_once()


async def test_play_next_does_not_autoplay_when_queue_has_tracks(cog):
    cog._autoplay_refill = AsyncMock(return_value=0)
    cog.song_queue.append(("https://www.youtube.com/watch?v=" + "Q" * 11, "q"))
    cog._extract_info = AsyncMock(return_value=None)  # track fails to load -> loop ends
    await cog.play_next(ctx(), make_vc(), allow_autoplay=True)
    cog._autoplay_refill.assert_not_awaited()


async def test_refill_failure_never_raises(cog):
    cog._extract_info = AsyncMock(side_effect=RuntimeError("boom"))
    assert await cog._autoplay_refill(ctx(), make_vc()) == 0


async def test_toggle_command_and_state_per_guild(cog):
    guild_a, guild_b = SimpleNamespace(id=1), SimpleNamespace(id=2)
    a = SimpleNamespace(guild=guild_a, send=AsyncMock())
    b = SimpleNamespace(guild=guild_b, send=AsyncMock())
    await MusicCog.autoplay_cmd.callback(cog, a)
    assert cog._autoplay_for(1).enabled is True and cog._autoplay_for(2).enabled is False
    await MusicCog.autoplay_cmd.callback(cog, a, "off")
    assert cog._autoplay_for(1).enabled is False
    await MusicCog.autoplay_cmd.callback(cog, b, "on")
    assert cog._autoplay_for(2).enabled is True
    await MusicCog.autoplay_cmd.callback(cog, b, "bogus")
    assert "Usage" in b.send.await_args.args[0]


async def test_disabling_drops_pending_autoplay_tracks(cog):
    await cog._autoplay_refill(ctx(), make_vc())
    cog.song_queue.append(("https://www.youtube.com/watch?v=" + "H" * 11, "human"))
    a = SimpleNamespace(guild=SimpleNamespace(id=42), send=AsyncMock())
    await MusicCog.autoplay_cmd.callback(cog, a)  # toggles off
    assert [t for _, t in cog.song_queue] == ["human"]


async def test_default_comes_from_config(monkeypatch):
    monkeypatch.setattr(music._config, "MUSIC_AUTOPLAY_DEFAULT", True)
    c = MusicCog(SimpleNamespace(voice_clients=[]))
    try:
        assert c._autoplay_for(7).enabled is True
    finally:
        c.inactivity_check.cancel()


def test_status_output_shows_autoplay_and_cookie_age_only():
    text = build_music_status(
        {"cookies": {"present": True, "path": "cookies.txt", "age_hours": 1}},
        staff=False,
        autoplay={"enabled": True, "consecutive": 2, "max": 10},
        cookie_refresh={"age_seconds": 300, "poll_age_seconds": 12, "last_error": "secret detail", "updates_total": 9},
    )
    assert "Autoplay: on (2/10 in a row)" in text
    assert "Last cookie refresh: 5m 0s ago" in text and "Extension last seen: 12s ago" in text
    assert "secret detail" not in text and "updates" not in text.lower()
    off = build_music_status({}, staff=False, autoplay={"enabled": False}, cookie_refresh={"age_seconds": None})
    assert "Autoplay: off" in off and "Last cookie refresh: never" in off


@pytest.mark.parametrize("var", ["MUSIC_AUTOPLAY_MAX_CONSECUTIVE", "MUSIC_AUTOPLAY_HISTORY"])
def test_config_parsing_is_safe(var, monkeypatch):
    import config

    monkeypatch.setenv(var, "not-a-number")
    assert config._env_int(var, 10, minimum=1) == 10
    monkeypatch.setenv(var, "0")
    assert config._env_int(var, 10, minimum=1) == 10
    monkeypatch.setenv(var, "7")
    assert config._env_int(var, 10, minimum=1) == 7
