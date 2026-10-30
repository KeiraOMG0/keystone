"""
Music page (/dashboard/music): view and control the bot's music player.

GET    /api/music/state                     player state            control_music
GET    /api/music/voice-channels            voice channel picker    control_music
GET    /api/music/status                    !musicstatus, public view   control_music
GET    /api/music/status/staff              !musicstatus, staff view    require_staff
POST   /api/music/control                   pause/resume/skip/stop/previous/shuffle/clear/
                                            loop/autoplay/volume/remove/move/join/leave   control_music
POST   /api/music/play      {query, channel_id?}   -> 202 job (resolve URL + enqueue)   control_music
POST   /api/music/search    {query}                -> 202 job (results)                 control_music
POST   /api/music/search/{job_id}/pick {index, channel_id?}                              control_music
GET    /api/music/jobs/{job_id}             poll a play/search job                       control_music
POST   /api/music/debugurl  {query?}        -> 202 job (masked report)                   require_staff
GET    /api/music/debugurl/{job_id}         poll a debugurl job                          require_staff
GET    /api/music/playlists                 list                                         control_music
POST   /api/music/playlists {name}          save current + queue                         control_music
POST   /api/music/playlists/{name}/load     {channel_id?}                                control_music
DELETE /api/music/playlists/{name}                                                       control_music

The Discord music commands are open to every member; the dashboard gates
viewing and controlling the player with the grantable `control_music`
permission (staff bypass). `!debugurl` and the staff half of `!musicstatus`
stay staff-only here (require_staff). Saving over / deleting someone else's
playlist is refused bot-side unless the actor holds staff_base (same rule as
`!save` / `!delete_playlist`). The actor is always the session user. yt-dlp
work runs as bot-side jobs (202 + poll), never inside one request (Cloudflare
cuts requests at 100 s).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission, require_staff
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/music", tags=["music"])

_PERMISSION = "control_music"
_JOB_ID = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
_PLAYLIST = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
_PASS_THROUGH = {400, 403, 404, 409, 422, 429}


def _error(exc: InternalAPIError) -> HTTPException:
    detail = exc.detail
    if exc.status_code in _PASS_THROUGH and isinstance(detail, dict) and isinstance(detail.get("error"), str):
        body = {"error": detail["error"]}
        if isinstance(detail.get("message"), str):
            body["message"] = detail["message"][:300]
        return HTTPException(status_code=exc.status_code, detail=body)
    if exc.status_code == 503:
        return HTTPException(status_code=503, detail={"error": "feature_unavailable", "message": "The music player is not loaded on the bot."})
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


def _snowflake(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    if not value.isdigit() or len(value) > 21:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": "Invalid channel."})
    return int(value)


async def _call(coro):
    try:
        return await coro
    except InternalAPIError as exc:
        raise _error(exc)


@router.get("/state")
async def get_state(session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _call(internal_client.get_music_state(config.DASHBOARD_GUILD_ID))


@router.get("/voice-channels")
async def voice_channels(session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _call(internal_client.get_music_voice_channels(config.DASHBOARD_GUILD_ID, int(session.discord_user_id)))


@router.get("/status")
async def get_status(session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _call(internal_client.get_music_status(config.DASHBOARD_GUILD_ID, staff=False))


@router.get("/status/staff")
async def get_staff_status(session: Session = Depends(require_staff)) -> dict:
    return await _call(internal_client.get_music_status(config.DASHBOARD_GUILD_ID, staff=True))


class ControlBody(BaseModel):
    action: Literal[
        "pause", "resume", "skip", "stop", "previous", "shuffle", "clear",
        "loop", "autoplay", "volume", "remove", "move", "join", "leave",
    ]
    value: int | bool | None = None
    index: int | None = Field(None, ge=0, le=100000)
    to_index: int | None = Field(None, ge=0, le=100000)
    channel_id: str | None = None


@router.post("/control")
async def control(body: ControlBody, session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    payload = {
        "actor_id": int(session.discord_user_id),
        "action": body.action,
        "value": body.value,
        "index": body.index,
        "to_index": body.to_index,
        "channel_id": _snowflake(body.channel_id),
    }
    return await _call(internal_client.music_control(config.DASHBOARD_GUILD_ID, payload))


class PlayBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    channel_id: str | None = None


@router.post("/play", status_code=status.HTTP_202_ACCEPTED)
async def play(body: PlayBody, session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _call(
        internal_client.music_start_play(
            config.DASHBOARD_GUILD_ID, actor_id=int(session.discord_user_id), query=body.query,
            channel_id=_snowflake(body.channel_id),
        )
    )


class SearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)


@router.post("/search", status_code=status.HTTP_202_ACCEPTED)
async def search(body: SearchBody, session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _call(
        internal_client.music_start_search(config.DASHBOARD_GUILD_ID, actor_id=int(session.discord_user_id), query=body.query)
    )


class PickBody(BaseModel):
    index: int = Field(..., ge=0, le=50)
    channel_id: str | None = None


@router.post("/search/{job_id}/pick")
async def pick(body: PickBody, job_id: str = _JOB_ID, session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _call(
        internal_client.music_pick(
            config.DASHBOARD_GUILD_ID, job_id, actor_id=int(session.discord_user_id), index=body.index,
            channel_id=_snowflake(body.channel_id),
        )
    )


async def _job(job_id: str, kinds: set[str]) -> dict:
    job = await _call(internal_client.get_music_job(config.DASHBOARD_GUILD_ID, job_id))
    if job.get("kind") not in kinds:
        # A control_music user must not read a staff debugurl report by id.
        raise HTTPException(status_code=404, detail={"error": "job_not_found", "message": "That lookup is no longer available."})
    return job


@router.get("/jobs/{job_id}")
async def job_status(job_id: str = _JOB_ID, session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _job(job_id, {"play", "search"})


class DebugBody(BaseModel):
    query: str | None = Field(None, max_length=500)


@router.post("/debugurl", status_code=status.HTTP_202_ACCEPTED)
async def debugurl(body: DebugBody, session: Session = Depends(require_staff)) -> dict:
    return await _call(
        internal_client.music_start_debugurl(config.DASHBOARD_GUILD_ID, actor_id=int(session.discord_user_id), query=body.query)
    )


@router.get("/debugurl/{job_id}")
async def debugurl_status(job_id: str = _JOB_ID, session: Session = Depends(require_staff)) -> dict:
    return await _job(job_id, {"debugurl"})


@router.get("/playlists")
async def playlists(session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _call(internal_client.get_music_playlists(config.DASHBOARD_GUILD_ID))


class SavePlaylistBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


@router.post("/playlists")
async def save_playlist(body: SavePlaylistBody, session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _call(
        internal_client.music_save_playlist(config.DASHBOARD_GUILD_ID, actor_id=int(session.discord_user_id), name=body.name)
    )


class LoadPlaylistBody(BaseModel):
    channel_id: str | None = None


@router.post("/playlists/{name}/load")
async def load_playlist(
    body: LoadPlaylistBody, name: str = _PLAYLIST, session: Session = Depends(require_permission(_PERMISSION))
) -> dict:
    return await _call(
        internal_client.music_load_playlist(
            config.DASHBOARD_GUILD_ID, name, actor_id=int(session.discord_user_id), channel_id=_snowflake(body.channel_id)
        )
    )


@router.delete("/playlists/{name}")
async def delete_playlist(name: str = _PLAYLIST, session: Session = Depends(require_permission(_PERMISSION))) -> dict:
    return await _call(
        internal_client.music_delete_playlist(config.DASHBOARD_GUILD_ID, name, actor_id=int(session.discord_user_id))
    )
