"""
Music player control for the dashboard (/dashboard/music).

GET    /guilds/{guild_id}/music/state                       player state (now playing, queue, volume, ...)
GET    /guilds/{guild_id}/music/voice-channels?actor_id=    voice channels the actor can see
GET    /guilds/{guild_id}/music/status?staff=               !musicstatus as data (staff=true adds technical details)
POST   /guilds/{guild_id}/music/control   {actor_id, action, value?, index?, to_index?, channel_id?} -> {message, state}
POST   /guilds/{guild_id}/music/play      {actor_id, query, channel_id?}  -> 202 job (resolve a URL + enqueue)
POST   /guilds/{guild_id}/music/search    {actor_id, query}               -> 202 job (10 results)
POST   /guilds/{guild_id}/music/search/{job_id}/pick {actor_id, index, channel_id?} -> {message, state}
POST   /guilds/{guild_id}/music/debugurl  {actor_id, query?}              -> 202 job (masked report; staff-only on the dashboard)
GET    /guilds/{guild_id}/music/jobs/{job_id}                             -> {job_id, kind, status, result, error, message}
GET    /guilds/{guild_id}/music/playlists                                 -> {"playlists": [{name, tracks, created_by}]}
POST   /guilds/{guild_id}/music/playlists           {actor_id, name}      -> save current + queue
POST   /guilds/{guild_id}/music/playlists/{name}/load {actor_id, channel_id?}
DELETE /guilds/{guild_id}/music/playlists/{name}?actor_id=

Thin wrapper over the LIVE MusicCog (bot.get_cog("Music")): the same player
state and core methods the prefix commands use (cogs/music.py "Dashboard
control"). Authorization happens in the dashboard backend
(server/routes/music.py: `control_music`, staff for status details/debugurl).
Refusals are 4xx {"detail": {"error", "message"}}; cogs.music is never
imported here (internal_api must not import cogs).
"""

from __future__ import annotations

from typing import Optional

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from internal_api.snowflakes import stringify_snowflakes
from services.music_control import CONTROL_ACTIONS, MusicActionError

router = APIRouter(dependencies=[Depends(require_internal_secret)])

_JOB_ID = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
_PLAYLIST = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def get_music_cog(bot: commands.Bot = Depends(get_bot)):
    cog = bot.get_cog("Music")
    if cog is None or not hasattr(cog, "dashboard_state"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "music_unavailable", "message": "The music cog is not loaded."},
        )
    return cog


async def _actor(guild: discord.Guild, actor_id: int):
    """The acting member (cache, then API). The dashboard only lets guild
    members through, so a miss is rare; fall back to a bare author object."""
    member = guild.get_member(actor_id)
    if member is None:
        try:
            member = await guild.fetch_member(actor_id)
        except discord.HTTPException:
            member = None
    if member is not None:
        return member
    from types import SimpleNamespace

    return SimpleNamespace(id=actor_id, voice=None, display_name="a dashboard user", bot=False)


def _refusal(exc: MusicActionError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.as_detail())


def _out(data: dict) -> dict:
    return stringify_snowflakes(data)


@router.get("/guilds/{guild_id}/music/state")
async def music_state(guild: discord.Guild = Depends(get_guild), cog=Depends(get_music_cog)) -> dict:
    return _out(cog.dashboard_state(guild))


@router.get("/guilds/{guild_id}/music/voice-channels")
async def music_voice_channels(
    actor_id: int = Query(..., ge=1),
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_music_cog),
) -> dict:
    actor = await _actor(guild, actor_id)
    return _out({"channels": cog.dashboard_voice_channels(guild, actor)})


@router.get("/guilds/{guild_id}/music/status")
async def music_status(
    staff: bool = False, guild: discord.Guild = Depends(get_guild), cog=Depends(get_music_cog)
) -> dict:
    return await cog.dashboard_status(guild, staff=staff)


class ControlBody(BaseModel):
    actor_id: int
    action: str
    value: Optional[int | bool] = None
    index: Optional[int] = None
    to_index: Optional[int] = None
    channel_id: Optional[int] = None


@router.post("/guilds/{guild_id}/music/control")
async def music_control(body: ControlBody, guild: discord.Guild = Depends(get_guild), cog=Depends(get_music_cog)) -> dict:
    if body.action not in CONTROL_ACTIONS:
        raise HTTPException(status_code=400, detail={"error": "unknown_action", "message": "Unknown action."})
    actor = await _actor(guild, body.actor_id)
    try:
        result = await cog.dashboard_control(
            guild, actor, body.action, value=body.value, index=body.index, to_index=body.to_index,
            channel_id=body.channel_id,
        )
    except MusicActionError as exc:
        raise _refusal(exc)
    return _out(result)


class PlayBody(BaseModel):
    actor_id: int
    query: str = Field(..., max_length=2000)
    channel_id: Optional[int] = None


@router.post("/guilds/{guild_id}/music/play", status_code=status.HTTP_202_ACCEPTED)
async def music_play(body: PlayBody, guild: discord.Guild = Depends(get_guild), cog=Depends(get_music_cog)) -> dict:
    actor = await _actor(guild, body.actor_id)
    try:
        return await cog.dashboard_start_play(guild, actor, body.query, body.channel_id)
    except MusicActionError as exc:
        raise _refusal(exc)


class SearchBody(BaseModel):
    actor_id: int
    query: str = Field(..., max_length=2000)


@router.post("/guilds/{guild_id}/music/search", status_code=status.HTTP_202_ACCEPTED)
async def music_search(body: SearchBody, guild: discord.Guild = Depends(get_guild), cog=Depends(get_music_cog)) -> dict:
    actor = await _actor(guild, body.actor_id)
    try:
        return await cog.dashboard_start_search(guild, actor, body.query)
    except MusicActionError as exc:
        raise _refusal(exc)


class PickBody(BaseModel):
    actor_id: int
    index: int
    channel_id: Optional[int] = None


@router.post("/guilds/{guild_id}/music/search/{job_id}/pick")
async def music_pick(
    body: PickBody,
    job_id: str = _JOB_ID,
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_music_cog),
) -> dict:
    actor = await _actor(guild, body.actor_id)
    try:
        return _out(await cog.dashboard_pick(guild, actor, job_id, body.index, body.channel_id))
    except MusicActionError as exc:
        raise _refusal(exc)


class DebugBody(BaseModel):
    actor_id: int
    query: Optional[str] = Field(None, max_length=2000)


@router.post("/guilds/{guild_id}/music/debugurl", status_code=status.HTTP_202_ACCEPTED)
async def music_debugurl(body: DebugBody, guild: discord.Guild = Depends(get_guild), cog=Depends(get_music_cog)) -> dict:
    actor = await _actor(guild, body.actor_id)
    try:
        return await cog.dashboard_start_debugurl(guild, actor, body.query)
    except MusicActionError as exc:
        raise _refusal(exc)


@router.get("/guilds/{guild_id}/music/jobs/{job_id}")
async def music_job(job_id: str = _JOB_ID, guild: discord.Guild = Depends(get_guild), cog=Depends(get_music_cog)) -> dict:
    job = cog.dashboard_job(guild.id, job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "job_not_found", "message": "That lookup is no longer available (the bot may have restarted)."},
        )
    return _out(job)


@router.get("/guilds/{guild_id}/music/playlists")
async def music_playlists(guild: discord.Guild = Depends(get_guild), cog=Depends(get_music_cog)) -> dict:
    return _out({"playlists": await cog.dashboard_playlists()})


class SavePlaylistBody(BaseModel):
    actor_id: int
    name: str = Field(..., max_length=200)


@router.post("/guilds/{guild_id}/music/playlists")
async def music_save_playlist(
    body: SavePlaylistBody, guild: discord.Guild = Depends(get_guild), cog=Depends(get_music_cog)
) -> dict:
    actor = await _actor(guild, body.actor_id)
    try:
        return _out(await cog.dashboard_save_playlist(guild, actor, body.name))
    except MusicActionError as exc:
        raise _refusal(exc)


class LoadPlaylistBody(BaseModel):
    actor_id: int
    channel_id: Optional[int] = None


@router.post("/guilds/{guild_id}/music/playlists/{name}/load")
async def music_load_playlist(
    body: LoadPlaylistBody,
    name: str = _PLAYLIST,
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_music_cog),
) -> dict:
    actor = await _actor(guild, body.actor_id)
    try:
        return _out(await cog.dashboard_load_playlist(guild, actor, name, body.channel_id))
    except MusicActionError as exc:
        raise _refusal(exc)


@router.delete("/guilds/{guild_id}/music/playlists/{name}")
async def music_delete_playlist(
    name: str = _PLAYLIST,
    actor_id: int = Query(..., ge=1),
    guild: discord.Guild = Depends(get_guild),
    cog=Depends(get_music_cog),
) -> dict:
    actor = await _actor(guild, actor_id)
    try:
        return _out(await cog.dashboard_delete_playlist(guild, actor, name))
    except MusicActionError as exc:
        raise _refusal(exc)
