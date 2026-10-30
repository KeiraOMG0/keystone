"""Dashboard music control: the job store, the cog's dashboard_* methods (same
player state + core methods as the prefix commands), playlist ownership, the
internal API routes and the dashboard backend routes (control_music / staff)."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("INTERNAL_API_SECRET", "test-secret")

import config  # noqa: E402
from cogs import music  # noqa: E402
from cogs.music import MusicCog  # noqa: E402
from services import live_events  # noqa: E402
from services.music_control import (  # noqa: E402
    MusicActionError,
    MusicJobs,
    check_volume,
    clean_query,
    safe_playlist_name,
)

GUILD_ID = 100000000000000001
ACTOR = 222222222222222222
OTHER = 333333333333333333


# -- pure helpers ----------------------------------------------------------------


def test_helpers_validate():
    assert safe_playlist_name("my list!") == "mylist"
    with pytest.raises(MusicActionError):
        safe_playlist_name("!!!")
    assert clean_query("  hi ") == "hi"
    for bad in ("", "a\nb", "x" * 501):
        with pytest.raises(MusicActionError):
            clean_query(bad)
    assert check_volume(150) == 150
    for bad in (-1, 201, True, "5"):
        with pytest.raises(MusicActionError):
            check_volume(bad)


async def test_jobs_done_failed_busy_and_private_by_reference():
    jobs = MusicJobs()
    private: dict = {}

    async def ok():
        private["x"] = 1
        return {"v": 1}

    async def refused():
        raise MusicActionError("load_failed", "nope", 422)

    async def boom():
        raise RuntimeError("secret path C:/x/cookies.txt")

    a = jobs.start(1, "search", ok, actor_id=5, private=private)
    b = jobs.start(1, "play", refused)
    c = jobs.start(1, "play", boom)
    await asyncio.sleep(0.01)
    assert jobs.get(1, a["job_id"])["result"] == {"v": 1}
    assert jobs.raw(1, a["job_id"])["_private"]["x"] == 1
    assert "_private" not in jobs.get(1, a["job_id"])
    assert jobs.get(1, b["job_id"])["error"] == "load_failed"
    failed = jobs.get(1, c["job_id"])
    assert failed["error"] == "internal_error" and "cookies" not in failed["message"]
    assert jobs.get(2, a["job_id"]) is None  # other guild

    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return {}

    for _ in range(MusicJobs.MAX_RUNNING):
        jobs.start(1, "play", slow)
    with pytest.raises(MusicActionError) as ei:
        jobs.start(1, "play", slow)
    assert ei.value.status == 429
    gate.set()
    await asyncio.sleep(0.01)


# -- cog --------------------------------------------------------------------------


def make_vc(playing=True, paused=False):
    vc = MagicMock()
    vc.guild = SimpleNamespace(id=GUILD_ID)
    vc.channel = SimpleNamespace(id=777, name="Music VC", members=[SimpleNamespace(bot=False)])
    vc.is_connected.return_value = True
    vc.is_playing.return_value = playing
    vc.is_paused.return_value = paused
    vc.source = SimpleNamespace(volume=0.5)
    return vc


class FakeChannel:
    def __init__(self, cid=555, name="music"):
        self.id = cid
        self.name = name
        self.send = AsyncMock()

    def permissions_for(self, _):
        return SimpleNamespace(view_channel=True, send_messages=True)


@pytest.fixture
async def cog(monkeypatch, tmp_path):
    published: list = []
    monkeypatch.setattr(live_events, "publish", lambda topic, data: published.append((topic, data)))
    monkeypatch.setattr(MusicCog, "_PLAYLISTS_DIR", str(tmp_path / "playlists"))
    text = FakeChannel()
    guild = SimpleNamespace(id=GUILD_ID, me=SimpleNamespace(id=1), get_channel=lambda cid: text if cid == text.id else None)
    vc = make_vc()
    bot = SimpleNamespace(voice_clients=[vc], latency=0.05)
    c = MusicCog(bot)
    c._announce_channel_id = text.id
    c.published, c.text, c.guild, c.vc = published, text, guild, vc
    c.actor = SimpleNamespace(id=ACTOR, voice=None, display_name="Kei @everyone", bot=False)
    yield c
    c.inactivity_check.cancel()


def _last_announcement(cog) -> tuple[str, dict]:
    call = cog.text.send.await_args
    return call.args[0], call.kwargs


async def test_pause_resume_announce_audit_and_publish(cog):
    cog._set_current_song("https://www.youtube.com/watch?v=AAAAAAAAAAA", "Song", 200, ACTOR)
    cog._audit_action = AsyncMock()
    r = await cog.dashboard_control(cog.guild, cog.actor, "pause")
    cog.vc.pause.assert_called_once()
    assert cog.current_song["paused_at"] is not None
    text, kwargs = _last_announcement(cog)
    assert text.startswith("⏸️ Paused (via dashboard by **Kei")
    assert "@everyone" not in text.replace("@\u200beveryone", "")  # escaped
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert ("music", {"event": "paused"}) in cog.published
    cog._audit_action.assert_awaited_with(GUILD_ID, ACTOR, "pause")
    assert r["state"]["current"]["requester_id"] == ACTOR

    cog.vc.is_playing.return_value = False
    cog.vc.is_paused.return_value = True
    await cog.dashboard_control(cog.guild, cog.actor, "resume")
    cog.vc.resume.assert_called_once()
    assert cog.current_song["paused_at"] is None and cog.current_song["paused_total"] >= 0


async def test_refusals_and_validation(cog):
    cog.vc.is_playing.return_value = False
    with pytest.raises(MusicActionError) as ei:
        await cog.dashboard_control(cog.guild, cog.actor, "pause")
    assert ei.value.code == "not_playing" and ei.value.status == 409
    with pytest.raises(MusicActionError):
        await cog.dashboard_control(cog.guild, cog.actor, "volume", value=500)
    with pytest.raises(MusicActionError):
        await cog.dashboard_control(cog.guild, cog.actor, "remove", index=1)  # empty queue
    with pytest.raises(MusicActionError):
        await cog.dashboard_control(cog.guild, cog.actor, "loop", value=None)
    cog.text.send.assert_not_awaited()


async def test_queue_ops_volume_loop(cog):
    cog._audit_action = AsyncMock()
    cog.song_queue.extend([("u1", "one"), ("u2", "two"), ("u3", "three")])
    await cog.dashboard_control(cog.guild, cog.actor, "move", index=3, to_index=1)
    assert [t for _, t in cog.song_queue] == ["three", "one", "two"]
    await cog.dashboard_control(cog.guild, cog.actor, "remove", index=2)
    assert [t for _, t in cog.song_queue] == ["three", "two"]
    await cog.dashboard_control(cog.guild, cog.actor, "volume", value=120)
    assert cog.current_volume == 1.2 and cog.vc.source.volume == 1.2
    await cog.dashboard_control(cog.guild, cog.actor, "loop", value=True)
    assert cog.loop_enabled is True
    r = await cog.dashboard_control(cog.guild, cog.actor, "clear")
    assert r["state"]["queue_total"] == 0


async def test_stop_clears_and_marks_stop(cog):
    cog._audit_action = AsyncMock()
    cog.song_queue.append(("u", "t"))
    await cog.dashboard_control(cog.guild, cog.actor, "stop")
    assert not cog.song_queue
    assert cog._stop_requested is True and cog._intentional_stop is True
    cog.vc.stop.assert_called_once()


async def test_state_hides_non_youtube_query(cog):
    cog.song_queue.append(("https://example.com/a.mp3?token=SECRET#x", "a"))
    cog.song_queue.append(("https://www.youtube.com/watch?v=AAAAAAAAAAA", "b"))
    st = cog.dashboard_state(cog.guild)
    assert st["queue"][0]["url"] == "https://example.com/a.mp3"
    assert st["queue"][1]["url"].endswith("v=AAAAAAAAAAA")
    assert st["announce_channel"]["channel_id"] == cog.text.id
    assert st["voice_channel"]["channel_id"] == 777


async def test_pick_only_by_searcher(cog):
    cog._search = AsyncMock(return_value=[("https://www.youtube.com/watch?v=BBBBBBBBBBB", "B")])
    cog._audit_action = AsyncMock()
    job = await cog.dashboard_start_search(cog.guild, cog.actor, "some song")
    await asyncio.sleep(0.01)
    other = SimpleNamespace(id=OTHER, voice=None, display_name="x", bot=False)
    with pytest.raises(MusicActionError) as ei:
        await cog.dashboard_pick(cog.guild, other, job["job_id"], 0, None)
    assert ei.value.status == 403
    cog.vc.is_playing.return_value = True
    r = await cog.dashboard_pick(cog.guild, cog.actor, job["job_id"], 0, None)
    assert r["state"]["queue"][0]["title"] == "B"


async def test_play_requires_url_and_voice(cog):
    with pytest.raises(MusicActionError) as ei:
        await cog.dashboard_start_play(cog.guild, cog.actor, "not a url", None)
    assert ei.value.code == "not_a_url"
    cog.bot.voice_clients = []
    with pytest.raises(MusicActionError) as ei:
        await cog.dashboard_start_play(cog.guild, cog.actor, "https://youtu.be/AAAAAAAAAAA", None)
    assert ei.value.code == "no_voice_channel"


async def test_prefix_commands_keep_their_replies(cog):
    ctx = SimpleNamespace(send=AsyncMock(), author=SimpleNamespace(id=1), guild=cog.guild)
    await MusicCog.pause.callback(cog, ctx)
    ctx.send.assert_awaited_with("⏸️ Paused")
    await MusicCog.volume.callback(cog, ctx, 300)
    ctx.send.assert_awaited_with("🔊 Volume set to 300%")
    assert cog.current_volume == 2.0
    await MusicCog.loop_cmd.callback(cog, ctx)
    ctx.send.assert_awaited_with("🔁 Loop enabled.")
    cog.song_queue.extend([("u1", "a"), ("u2", "b")])
    await MusicCog.move.callback(cog, ctx, 2, 1)
    assert [t for _, t in cog.song_queue] == ["b", "a"]


# -- playlist ownership -------------------------------------------------------------


async def test_playlist_owner_rules_prefix_and_dashboard(cog):
    cog._has_staff_base = AsyncMock(return_value=False)
    cog._audit_action = AsyncMock()
    cog.song_queue.append(("https://www.youtube.com/watch?v=AAAAAAAAAAA", "a"))
    owner_ctx = SimpleNamespace(send=AsyncMock(), author=SimpleNamespace(id=ACTOR), guild=cog.guild)
    other_ctx = SimpleNamespace(send=AsyncMock(), author=SimpleNamespace(id=OTHER), guild=cog.guild)

    await MusicCog.save_cmd.callback(cog, owner_ctx, name="mix")
    data = json.load(open(cog._playlist_path("mix"), encoding="utf-8"))
    assert data["created_by"] == ACTOR and data["tracks"][0]["title"] == "a"
    assert cog._load_playlist("mix") == [("https://www.youtube.com/watch?v=AAAAAAAAAAA", "a")]

    await MusicCog.save_cmd.callback(cog, other_ctx, name="mix")
    assert "belongs to someone else" in other_ctx.send.await_args.args[0]
    await MusicCog.delete_playlist_cmd.callback(cog, other_ctx, name="mix")
    assert os.path.exists(cog._playlist_path("mix"))
    other = SimpleNamespace(id=OTHER, voice=None, display_name="o", bot=False)
    with pytest.raises(MusicActionError) as ei:
        await cog.dashboard_delete_playlist(cog.guild, other, "mix")
    assert ei.value.code == "playlist_not_yours"

    # staff_base may overwrite; the original owner is kept.
    cog._has_staff_base = AsyncMock(return_value=True)
    await cog.dashboard_save_playlist(cog.guild, other, "mix")
    assert json.load(open(cog._playlist_path("mix"), encoding="utf-8"))["created_by"] == ACTOR
    cog._has_staff_base = AsyncMock(return_value=False)
    await MusicCog.delete_playlist_cmd.callback(cog, owner_ctx, name="mix")
    assert not os.path.exists(cog._playlist_path("mix"))


async def test_legacy_playlist_readable_but_staff_only_to_change(cog):
    cog._has_staff_base = AsyncMock(return_value=False)
    path = cog._playlist_path("old")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{"url": "https://youtu.be/AAAAAAAAAAA", "title": "x"}], f)
    assert cog._load_playlist("old") == [("https://youtu.be/AAAAAAAAAAA", "x")]
    summaries = await cog.dashboard_playlists()
    assert summaries == [{"name": "old", "tracks": 1, "created_by": None}]
    ctx = SimpleNamespace(send=AsyncMock(), author=SimpleNamespace(id=ACTOR), guild=cog.guild)
    await MusicCog.delete_playlist_cmd.callback(cog, ctx, name="old")
    assert os.path.exists(path)
    cog._has_staff_base = AsyncMock(return_value=True)
    await MusicCog.delete_playlist_cmd.callback(cog, ctx, name="old")
    assert not os.path.exists(path)


# -- internal API -------------------------------------------------------------------


@pytest.fixture
async def internal(cog):
    from internal_api.app import create_app

    member = SimpleNamespace(id=ACTOR, voice=None, display_name="Kei", bot=False)
    cog.guild.get_member = lambda uid: member if uid == ACTOR else None
    cog.guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "x"))
    cog._audit_action = AsyncMock()
    bot = cog.bot
    bot.get_guild = lambda gid: cog.guild if gid == GUILD_ID else None
    bot.get_cog = lambda name: cog if name == "Music" else None
    app = create_app()
    app.state.bot = bot
    auth = {"Authorization": f"Bearer {config.INTERNAL_API_SECRET}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=auth) as c:
        c.cog = cog
        yield c


async def test_internal_state_and_control(internal):
    cog = internal.cog
    cog._set_current_song("https://www.youtube.com/watch?v=AAAAAAAAAAA", "Song", 100, ACTOR)
    r = await internal.get(f"/guilds/{GUILD_ID}/music/state")
    assert r.status_code == 200
    assert r.json()["current"]["requester_id"] == str(ACTOR)
    assert r.json()["voice_channel"]["channel_id"] == "777"

    r = await internal.post(f"/guilds/{GUILD_ID}/music/control", json={"actor_id": ACTOR, "action": "pause"})
    assert r.status_code == 200 and r.json()["state"]["paused"] in (True, False)
    cog.vc.is_playing.return_value = False
    r = await internal.post(f"/guilds/{GUILD_ID}/music/control", json={"actor_id": ACTOR, "action": "pause"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_playing"
    r = await internal.post(f"/guilds/{GUILD_ID}/music/control", json={"actor_id": ACTOR, "action": "explode"})
    assert r.status_code == 400


async def test_internal_503_without_cog(internal):
    internal.cog.bot.get_cog = lambda name: None
    r = await internal.get(f"/guilds/{GUILD_ID}/music/state")
    assert r.status_code == 503


async def test_internal_job_poll(internal):
    internal.cog._search = AsyncMock(return_value=[("https://www.youtube.com/watch?v=BBBBBBBBBBB", "B")])
    r = await internal.post(f"/guilds/{GUILD_ID}/music/search", json={"actor_id": ACTOR, "query": "abc"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    await asyncio.sleep(0.01)
    r = await internal.get(f"/guilds/{GUILD_ID}/music/jobs/{job_id}")
    assert r.json()["status"] == "done" and r.json()["result"]["results"][0]["title"] == "B"
    assert (await internal.get(f"/guilds/{GUILD_ID}/music/jobs/nope")).status_code == 404


# -- dashboard backend --------------------------------------------------------------

from server import config as server_config  # noqa: E402
from server.app import app as dashboard_app  # noqa: E402
from server.auth.dependencies import get_current_session, require_session  # noqa: E402
from server.db.models import Session  # noqa: E402
from server.services import internal_client  # noqa: E402
from server.services.internal_client import InternalAPIError  # noqa: E402

ORIGIN = server_config.FRONTEND_BASE_URL


@pytest.fixture
def dash(monkeypatch):
    st = {"allowed": True, "staff": False, "calls": []}

    async def check_permission(guild_id, user_id, perm):
        st["calls"].append(("perm", perm))
        return st["allowed"] and perm == "control_music"

    async def check_is_staff(guild_id, user_id):
        return st["staff"]

    async def music_control(guild_id, payload):
        st["calls"].append(("control", payload))
        if payload["action"] == "skip":
            raise InternalAPIError("x", status_code=409, detail={"error": "not_playing", "message": "Nothing is playing."})
        return {"message": "ok", "state": {}}

    async def get_music_status(guild_id, staff):
        st["calls"].append(("status", staff))
        return {"available": True, "staff": {} if staff else None}

    async def get_music_job(guild_id, job_id):
        return {"job_id": job_id, "kind": "debugurl", "status": "done", "result": {"report": "masked"}}

    async def get_music_state(guild_id):
        raise InternalAPIError("down")

    for name, fn in [("check_permission", check_permission), ("check_is_staff", check_is_staff),
                     ("music_control", music_control), ("get_music_status", get_music_status),
                     ("get_music_job", get_music_job), ("get_music_state", get_music_state)]:
        monkeypatch.setattr(internal_client, name, fn)

    s = Session()
    s.session_id = "sid"
    s.discord_user_id = ACTOR
    s.discord_username = "u"
    s.discord_avatar = None
    s.is_guild_member = True
    s.cached_role_ids = []
    dashboard_app.dependency_overrides[get_current_session] = lambda: s
    dashboard_app.dependency_overrides[require_session] = lambda: s
    c = TestClient(dashboard_app)
    c.st = st  # type: ignore[attr-defined]
    yield c
    dashboard_app.dependency_overrides.clear()


def test_dashboard_control_uses_session_actor_and_permission(dash):
    r = dash.post("/api/music/control", json={"action": "volume", "value": 80, "actor_id": OTHER}, headers={"Origin": ORIGIN})
    assert r.status_code == 200
    payload = [c for c in dash.st["calls"] if c[0] == "control"][-1][1]
    assert payload["actor_id"] == ACTOR and payload["value"] == 80
    assert ("perm", "control_music") in dash.st["calls"]


def test_dashboard_refusal_passthrough_and_502(dash):
    r = dash.post("/api/music/control", json={"action": "skip"}, headers={"Origin": ORIGIN})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_playing"
    assert dash.get("/api/music/state").status_code == 502


def test_dashboard_forbidden_without_permission(dash):
    dash.st["allowed"] = False
    assert dash.get("/api/music/status").status_code == 403
    r = dash.post("/api/music/control", json={"action": "pause"}, headers={"Origin": ORIGIN})
    assert r.status_code == 403 and not [c for c in dash.st["calls"] if c[0] == "control"]


def test_staff_status_and_debugurl_need_staff(dash):
    assert dash.get("/api/music/status/staff").status_code == 403
    assert dash.get("/api/music/debugurl/abc").status_code == 403
    assert dash.get("/api/music/status").json()["staff"] is None
    dash.st["staff"] = True
    assert dash.get("/api/music/status/staff").json()["staff"] == {}
    assert ("status", True) in dash.st["calls"]
    assert dash.get("/api/music/debugurl/abc").json()["result"]["report"] == "masked"


def test_control_music_cannot_read_debugurl_job_by_id(dash):
    r = dash.get("/api/music/jobs/abc")
    assert r.status_code == 404


def test_bad_channel_id_rejected(dash):
    r = dash.post("/api/music/control", json={"action": "join", "channel_id": "abc"}, headers={"Origin": ORIGIN})
    assert r.status_code == 400
