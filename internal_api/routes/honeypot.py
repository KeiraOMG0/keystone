"""
/guilds/{guild_id}/honeypot/...  — honeypot config, catches, stats and appeals.

Thin adapters over services.honeypot_service.HoneypotService (resolved via
internal_api.deps.get_honeypot_service); the /honeypot slash commands call the
very same service methods. Shapes are specified in docs/honeypot_contract.md.

HoneypotError carries its own HTTP status and wire detail (e.g. 409
"already_configured", 404 "invalid_token"), so routes just translate it.
Snowflakes leave as strings via internal_api.snowflakes.

The appeal lookup/submit routes are called (through the dashboard backend) on
behalf of banned people who cannot log in, so their failures are deliberately
generic (not_eligible / already_submitted) to avoid user enumeration.
"""

from __future__ import annotations

import discord
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, StrictBool, StrictInt

from internal_api.auth import require_internal_secret
from internal_api.deps import get_guild, get_honeypot_service
from internal_api.snowflakes import stringify_snowflakes
from services.honeypot_service import SCRAMBLE_MANUAL_TIMEOUT, HoneypotError

router = APIRouter(dependencies=[Depends(require_internal_secret)])


def _http(exc: HoneypotError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


class UpdateConfigRequest(BaseModel):
    enabled: StrictBool | None = None
    action: str | None = None
    dm_before: StrictBool | None = None
    appeal_enabled: StrictBool | None = None
    delete_message_days: StrictInt | None = None
    log_channel_id: int | None = None
    scramble_enabled: StrictBool | None = None
    scramble_interval_hours: StrictInt | None = None
    rejoin_invite_url: str | None = None
    invite_in_removal_dm: StrictBool | None = None


class ScrambleRequest(BaseModel):
    actor_id: int


class InviteRequest(BaseModel):
    actor_id: int
    channel_id: int


class VerifiedAppealRequest(BaseModel):
    """Only sent by the dashboard after Discord login proved user_id."""
    user_id: int
    username: str | None = None
    name: str
    message: str = ""


class RejoinRequest(BaseModel):
    """access_token: the person's OAuth token (scope guilds.join), used once, never stored or logged."""
    user_id: int
    access_token: str


class SetupRequest(BaseModel):
    actor_id: int
    category_name: str | None = None
    channel_name: str | None = None
    position_top: bool = True
    action: str | None = None
    dm_before: bool | None = None
    appeal_enabled: bool | None = None


class ReviewRequest(BaseModel):
    reviewer_id: int
    note: str | None = None


@router.get("/guilds/{guild_id}/honeypot/config")
async def get_config(guild_id: int, service=Depends(get_honeypot_service)) -> dict:
    return stringify_snowflakes(await service.get_config(guild_id))


@router.put("/guilds/{guild_id}/honeypot/config")
async def update_config(
    body: UpdateConfigRequest,
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_honeypot_service),
) -> dict:
    try:
        cfg = await service.update_config(guild, body.model_dump(exclude_unset=True))
    except HoneypotError as exc:
        raise _http(exc)
    return stringify_snowflakes(cfg)


@router.post("/guilds/{guild_id}/honeypot/setup")
async def setup_honeypot(
    body: SetupRequest,
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_honeypot_service),
) -> dict:
    try:
        cfg, warnings = await service.setup(
            guild, body.actor_id,
            category_name=body.category_name, channel_name=body.channel_name,
            position_top=body.position_top, action=body.action,
            dm_before=body.dm_before, appeal_enabled=body.appeal_enabled,
        )
    except HoneypotError as exc:
        raise _http(exc)
    return stringify_snowflakes({"config": cfg, "warnings": warnings})


@router.post("/guilds/{guild_id}/honeypot/scramble")
async def scramble_names(
    body: ScrambleRequest,
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_honeypot_service),
) -> dict:
    """Rename the trap channel + category now. 409 too_soon / rate_limited / channel_missing,
    403 when Discord refuses (Manage Channels). Bounded so the dashboard's 10 s client never times out."""
    try:
        res = await service.scramble_names(
            guild, actor_id=body.actor_id, trigger="manual", timeout=SCRAMBLE_MANUAL_TIMEOUT,
        )
    except HoneypotError as exc:
        raise _http(exc)
    return stringify_snowflakes(res)


@router.post("/guilds/{guild_id}/honeypot/invite")
async def create_rejoin_invite(
    body: InviteRequest,
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_honeypot_service),
) -> dict:
    """Create a permanent invite in channel_id and store it as the rejoin link -> config."""
    try:
        return stringify_snowflakes(await service.create_rejoin_invite(guild, body.channel_id, body.actor_id))
    except HoneypotError as exc:
        raise _http(exc)


@router.get("/guilds/{guild_id}/honeypot/catches")
async def list_catches(
    guild_id: int,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = None,
    service=Depends(get_honeypot_service),
) -> dict:
    return stringify_snowflakes({"catches": await service.get_catches(guild_id, limit, before_id)})


@router.get("/guilds/{guild_id}/honeypot/stats")
async def get_stats(guild_id: int, service=Depends(get_honeypot_service)) -> dict:
    return stringify_snowflakes(await service.get_stats(guild_id))


@router.get("/guilds/{guild_id}/honeypot/appeals")
async def list_appeals(
    guild_id: int, status: str = "all", service=Depends(get_honeypot_service),
) -> dict:
    try:
        appeals = await service.list_appeals(guild_id, status)
    except HoneypotError as exc:
        raise _http(exc)
    return stringify_snowflakes({"appeals": appeals})


@router.get("/guilds/{guild_id}/honeypot/appeals/lookup/{token}")
async def lookup_appeal(guild_id: int, token: str, service=Depends(get_honeypot_service)) -> dict:
    try:
        return stringify_snowflakes(await service.lookup_appeal(guild_id, token))
    except HoneypotError as exc:
        raise _http(exc)


@router.get("/guilds/{guild_id}/honeypot/appeals/for-user/{user_id}")
async def appeal_status_for_user(guild_id: int, user_id: int, service=Depends(get_honeypot_service)) -> dict:
    """The dashboard calls this only for the user its Discord login just proved."""
    return stringify_snowflakes(await service.appeal_status_for_user(guild_id, user_id))


@router.post("/guilds/{guild_id}/honeypot/appeals/submit-verified")
async def submit_verified_appeal(
    guild_id: int, body: VerifiedAppealRequest, service=Depends(get_honeypot_service),
) -> dict:
    try:
        return await service.submit_verified_appeal(guild_id, body.user_id, body.username, body.name, body.message)
    except HoneypotError as exc:
        raise _http(exc)


@router.post("/guilds/{guild_id}/honeypot/appeals/rejoin")
async def rejoin(
    body: RejoinRequest,
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_honeypot_service),
) -> dict:
    """-> {"result": "joined" | "already_member"}; 409 not_approved / still_banned /
    too_many_guilds / login_expired; 403 missing Create Invite."""
    try:
        return await service.rejoin(guild, body.user_id, body.access_token)
    except HoneypotError as exc:
        raise _http(exc)


@router.post("/guilds/{guild_id}/honeypot/appeals/{appeal_id}/approve")
async def approve_appeal(
    appeal_id: int,
    body: ReviewRequest,
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_honeypot_service),
) -> dict:
    try:
        return await service.approve_appeal(guild, appeal_id, body.reviewer_id, body.note)
    except HoneypotError as exc:
        raise _http(exc)


@router.post("/guilds/{guild_id}/honeypot/appeals/{appeal_id}/deny")
async def deny_appeal(
    guild_id: int, appeal_id: int, body: ReviewRequest, service=Depends(get_honeypot_service),
) -> dict:
    try:
        return await service.deny_appeal(guild_id, appeal_id, body.reviewer_id, body.note)
    except HoneypotError as exc:
        raise _http(exc)
