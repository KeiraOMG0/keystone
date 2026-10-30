"""
GET  /api/honeypot/config                     (moderate_members)
GET  /api/honeypot/catches?limit=&before_id=  (moderate_members)
GET  /api/honeypot/stats                      (moderate_members)
GET  /api/honeypot/appeals?status=            (moderate_members)
PUT  /api/honeypot/config                     (ban_members)
POST /api/honeypot/setup                      (ban_members)
POST /api/honeypot/scramble                   (ban_members)  rename channel + category now
POST /api/honeypot/invite                     (ban_members)  create the rejoin invite {channel_id}
POST /api/honeypot/appeals/{id}/approve       (ban_members)
POST /api/honeypot/appeals/{id}/deny          (ban_members)

Thin, permission-gated client of internal_api/routes/honeypot.py (contract:
docs/honeypot_contract.md), same shape as server/routes/moderation.py.
Read routes need "moderate_members"; anything that changes configuration or
unbans someone needs "ban_members" (the bar the existing !ban / !unban
commands use). The acting user (setup actor / appeal reviewer) is ALWAYS
taken from `session.discord_user_id`, never from the request body.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/honeypot", tags=["honeypot"])

Action = Literal["ban", "softban", "kick"]
AppealStatus = Literal["pending", "approved", "denied", "invited", "all"]


def _unavailable() -> HTTPException:
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


def _translate(exc: InternalAPIError, *, not_found: str = "not_found") -> HTTPException:
    """Map an internal-API failure to this dashboard's error vocabulary."""
    code = exc.status_code
    if code == 400:
        # The bot's 400 text is a validation message written for staff (e.g. "That invite
        # is for a different server."); pass it on so the dashboard can show it.
        body = {"error": "invalid_request"}
        if isinstance(exc.detail, str) and exc.detail:
            body["message"] = exc.detail[:300]
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=body)
    if code == 404:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": not_found})
    if code == 409:
        err = exc.detail if isinstance(exc.detail, str) and exc.detail else "conflict"
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": err})
    if code == 403:
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "discord_forbidden"})
    return _unavailable()


def _snowflake(value: str | int | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("must be a Discord snowflake")
    if n <= 0:
        raise ValueError("must be a Discord snowflake")
    return n


# ---------------------------------------------------------------------------
# Reads - "moderate_members"
# ---------------------------------------------------------------------------

@router.get("/config")
async def get_config(session: Session = Depends(require_permission("moderate_members"))) -> dict:
    try:
        return await internal_client.get_honeypot_config(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.get("/catches")
async def get_catches(
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, ge=1),
    session: Session = Depends(require_permission("moderate_members")),
) -> dict:
    try:
        return await internal_client.get_honeypot_catches(config.DASHBOARD_GUILD_ID, limit, before_id)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.get("/stats")
async def get_stats(session: Session = Depends(require_permission("moderate_members"))) -> dict:
    try:
        return await internal_client.get_honeypot_stats(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.get("/appeals")
async def get_appeals(
    status_filter: AppealStatus = Query("all", alias="status"),
    session: Session = Depends(require_permission("moderate_members")),
) -> dict:
    try:
        return await internal_client.get_honeypot_appeals(config.DASHBOARD_GUILD_ID, status_filter)
    except InternalAPIError as exc:
        raise _translate(exc)


# ---------------------------------------------------------------------------
# Writes - "ban_members"
# ---------------------------------------------------------------------------

class UpdateConfigBody(BaseModel):
    enabled: bool | None = None
    action: Action | None = None
    dm_before: bool | None = None
    appeal_enabled: bool | None = None
    delete_message_days: int | None = Field(default=None, ge=0, le=7)
    scramble_enabled: bool | None = None
    # Mirrors SCRAMBLE_MIN_HOURS / SCRAMBLE_MAX_HOURS in services/honeypot_service.py.
    scramble_interval_hours: int | None = Field(default=None, ge=1, le=168)
    # Rejoin link (the bot checks it is a permanent invite to this server); null clears it.
    rejoin_invite_url: str | None = Field(default=None, max_length=200)
    invite_in_removal_dm: bool | None = None
    # Snowflake as a string on the wire (JS-safe); explicit null clears it.
    log_channel_id: str | int | None = None

    @field_validator("log_channel_id")
    @classmethod
    def _check_channel(cls, v: str | int | None) -> str | int | None:
        _snowflake(v)
        return v


@router.put("/config")
async def update_config(
    body: UpdateConfigBody,
    session: Session = Depends(require_permission("ban_members")),
) -> dict:
    # Partial update: only fields the client actually sent (an explicit
    # log_channel_id: null is kept so it can clear the override).
    values = body.model_dump(exclude_unset=True)
    if "log_channel_id" in values:
        values["log_channel_id"] = _snowflake(values["log_channel_id"])
    try:
        return await internal_client.update_honeypot_config(config.DASHBOARD_GUILD_ID, values)
    except InternalAPIError as exc:
        raise _translate(exc)


class SetupBody(BaseModel):
    category_name: str | None = Field(default=None, min_length=1, max_length=100)
    channel_name: str | None = Field(default=None, min_length=1, max_length=100)
    position_top: bool = True
    action: Action | None = None
    dm_before: bool | None = None
    appeal_enabled: bool | None = None


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup(
    body: SetupBody,
    session: Session = Depends(require_permission("ban_members")),
) -> dict:
    values = body.model_dump(exclude_none=True)
    try:
        return await internal_client.setup_honeypot(config.DASHBOARD_GUILD_ID, session.discord_user_id, values)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.post("/scramble")
async def scramble(session: Session = Depends(require_permission("ban_members"))) -> dict:
    """409 {"error": "too_soon" | "rate_limited" | "channel_missing"}; 502 discord_forbidden."""
    try:
        return await internal_client.scramble_honeypot_names(config.DASHBOARD_GUILD_ID, session.discord_user_id)
    except InternalAPIError as exc:
        raise _translate(exc)


class InviteBody(BaseModel):
    channel_id: str | int

    @field_validator("channel_id")
    @classmethod
    def _check(cls, v: str | int) -> str | int:
        if _snowflake(v) is None:
            raise ValueError("must be a Discord snowflake")
        return v


@router.post("/invite")
async def create_invite(body: InviteBody, session: Session = Depends(require_permission("ban_members"))) -> dict:
    """Bot creates a permanent invite in channel_id and stores it as the rejoin link -> config."""
    try:
        return await internal_client.create_honeypot_rejoin_invite(
            config.DASHBOARD_GUILD_ID, session.discord_user_id, _snowflake(body.channel_id),
        )
    except InternalAPIError as exc:
        raise _translate(exc)


class ReviewBody(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/appeals/{appeal_id}/approve")
async def approve_appeal(
    appeal_id: int,
    body: ReviewBody,
    session: Session = Depends(require_permission("ban_members")),
) -> dict:
    try:
        return await internal_client.approve_honeypot_appeal(
            config.DASHBOARD_GUILD_ID, appeal_id, session.discord_user_id, body.note
        )
    except InternalAPIError as exc:
        raise _translate(exc, not_found="appeal_not_found")


@router.post("/appeals/{appeal_id}/deny")
async def deny_appeal(
    appeal_id: int,
    body: ReviewBody,
    session: Session = Depends(require_permission("ban_members")),
) -> dict:
    try:
        return await internal_client.deny_honeypot_appeal(
            config.DASHBOARD_GUILD_ID, appeal_id, session.discord_user_id, body.note
        )
    except InternalAPIError as exc:
        raise _translate(exc, not_found="appeal_not_found")
