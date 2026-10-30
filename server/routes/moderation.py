"""
GET    /api/moderation/warnings/recent?limit=1..200
GET    /api/moderation/warnings/{user_id}
POST   /api/moderation/warnings
DELETE /api/moderation/warnings/{warning_id}?user_id=
GET    /api/moderation/history/{user_id}
GET    /api/moderation/members/search?q=&limit=1..25
POST   /api/moderation/kick
POST   /api/moderation/ban
POST   /api/moderation/unban
POST   /api/moderation/timeout
POST   /api/moderation/untimeout
GET    /api/moderation/purge-channels                 manage_messages_bulk
POST   /api/moderation/purge          -> 202 job      manage_messages_bulk
GET    /api/moderation/purge/{job_id}                 manage_messages_bulk
GET    /api/moderation/message-log/status             require_staff
POST   /api/moderation/message-log/export             require_staff (DMs the export, never returns it)

Permission-gated proxy of the internal API moderation routes
(internal_api/routes/moderation.py, internal_api/routes/member_tools.py).
Gates match the Discord commands: kick "kick_members", ban/unban
"ban_members", purge "manage_messages_bulk" (`!purge`), the message-log
export staff only (`!msglog`), warnings and timeouts "moderate_members"
(`!warn`/`!warnings`/`!delwarn`/`!timeout`). Member search and a member's
mod-action history accept ANY of moderate_members / kick_members /
ban_members (require_any_permission), so kick-only or ban-only roles can use
the Moderation page to find and act on a member.

The acting moderator (`moderator_id`, also used for the rank check and for
"who removed this warning") ALWAYS comes from `session.discord_user_id`, never
from the request body.

Input bounds (pydantic, 422 before anything reaches the bot): reason at most
512 characters, timeout 1..40320 minutes (Discord's 28-day maximum), ban
message deletion 0..7 days, recent-warnings limit 1..200, search q at most 100
characters. These mirror services/moderation_service.py's MAX_* constants
(the bot re-validates; keep both in sync).

Error mapping (_translate): the bot's {"error", "message"} details pass through
with their status: 403 hierarchy_blocked / actor_not_member (rank or identity
refusal; never "forbidden", so the page does not swap to the 403 screen),
404 member_not_found / warning_not_found / not_banned, 409 discord_forbidden,
400 discord_rejected, 422 invalid_input, 503 discord_unavailable. Anything
else (bot unreachable, not ready, guild not cached) is 502
{"error": "internal_api_unavailable"}.
"""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from server import config
from server.db.models import Session
from server.permissions.guards import require_any_permission, require_permission, require_staff
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/moderation", tags=["moderation"])

# Member search + mod-action history: any role that can act on a member from
# the Moderation page may find them and see their past actions.
MEMBER_READ_PERMISSIONS = ("moderate_members", "kick_members", "ban_members")

# Keep in sync with services/moderation_service.py (bot side).
MAX_REASON_LENGTH = 512
MAX_TIMEOUT_MINUTES = 28 * 24 * 60
MAX_DELETE_MESSAGE_DAYS = 7
RECENT_WARNINGS_MAX = 200
# `!purge` bounds (services/purge_service.py; keep in sync): a count of
# PURGE_MIN..PURGE_MAX, or a window "<n>s|m|h|d" up to PURGE_MAX_WINDOW_SECONDS;
# window purges and counts over PURGE_CONFIRM_ABOVE need confirm=true.
PURGE_MIN = 1
PURGE_MAX = 1000
PURGE_CONFIRM_ABOVE = 100
PURGE_MAX_WINDOW_SECONDS = 14 * 24 * 3600

_SNOWFLAKE = dict(ge=1, lt=2**63)


def _guild() -> int:
    return config.DASHBOARD_GUILD_ID


def _translate(exc: InternalAPIError) -> HTTPException:
    code = exc.status_code
    detail = exc.detail if isinstance(exc.detail, dict) else None
    if detail is not None and code in (400, 403, 404, 409, 422, 503):
        return HTTPException(status_code=code, detail=detail)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"error": "internal_api_unavailable"},
    )


class AddWarningBody(BaseModel):
    user_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.get("/warnings/recent")
async def get_recent_warnings(
    limit: int = Query(20, ge=1, le=RECENT_WARNINGS_MAX),
    session: Session = Depends(require_permission("moderate_members")),
) -> dict:
    """Most recent warnings across the guild. MUST stay registered before
    GET /warnings/{user_id} ({user_id} is an int and would 422 on "recent")."""
    try:
        return await internal_client.get_recent_warnings(_guild(), limit)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.get("/warnings/{user_id}")
async def get_warnings(
    user_id: int,
    session: Session = Depends(require_permission("moderate_members")),
) -> dict:
    try:
        return await internal_client.get_warnings(_guild(), user_id)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.post("/warnings", status_code=status.HTTP_201_CREATED)
async def add_warning(
    body: AddWarningBody,
    session: Session = Depends(require_permission("moderate_members")),
) -> dict:
    try:
        return await internal_client.add_warning(_guild(), body.user_id, session.discord_user_id, body.reason)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.delete("/warnings/{warning_id}")
async def remove_warning(
    warning_id: int,
    user_id: int | None = Query(None, **_SNOWFLAKE),
    session: Session = Depends(require_permission("moderate_members")),
) -> dict:
    """The session user is recorded as who removed it. `user_id` (sent by the
    page) restricts the delete to a warning of that member."""
    try:
        await internal_client.remove_warning(_guild(), warning_id, session.discord_user_id, user_id)
    except InternalAPIError as exc:
        raise _translate(exc)
    return {"removed": True}


@router.get("/history/{user_id}")
async def get_history(
    user_id: int,
    session: Session = Depends(require_any_permission(*MEMBER_READ_PERMISSIONS)),
) -> dict:
    try:
        return await internal_client.get_mod_history(_guild(), user_id)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.get("/members/search")
async def search_members(
    q: str = Query("", max_length=100),
    limit: int = Query(20, ge=1, le=25),
    session: Session = Depends(require_any_permission(*MEMBER_READ_PERMISSIONS)),
) -> dict:
    try:
        return await internal_client.search_members(_guild(), q, limit)
    except InternalAPIError as exc:
        raise _translate(exc)


class KickBody(BaseModel):
    user_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.post("/kick", status_code=status.HTTP_201_CREATED)
async def kick_member(
    body: KickBody,
    session: Session = Depends(require_permission("kick_members")),
) -> dict:
    try:
        return await internal_client.kick_member(_guild(), body.user_id, session.discord_user_id, body.reason)
    except InternalAPIError as exc:
        raise _translate(exc)


class BanBody(BaseModel):
    user_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    delete_message_days: int = Field(default=0, ge=0, le=MAX_DELETE_MESSAGE_DAYS)


@router.post("/ban", status_code=status.HTTP_201_CREATED)
async def ban_user(
    body: BanBody,
    session: Session = Depends(require_permission("ban_members")),
) -> dict:
    try:
        return await internal_client.ban_user(
            _guild(), body.user_id, session.discord_user_id, body.reason, body.delete_message_days
        )
    except InternalAPIError as exc:
        raise _translate(exc)


class UnbanBody(BaseModel):
    user_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.post("/unban", status_code=status.HTTP_201_CREATED)
async def unban_user(
    body: UnbanBody,
    session: Session = Depends(require_permission("ban_members")),
) -> dict:
    try:
        return await internal_client.unban_user(_guild(), body.user_id, session.discord_user_id, body.reason)
    except InternalAPIError as exc:
        raise _translate(exc)


class TimeoutBody(BaseModel):
    user_id: int = Field(**_SNOWFLAKE)
    duration_minutes: int = Field(ge=1, le=MAX_TIMEOUT_MINUTES)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.post("/timeout", status_code=status.HTTP_201_CREATED)
async def timeout_member(
    body: TimeoutBody,
    session: Session = Depends(require_permission("moderate_members")),
) -> dict:
    try:
        return await internal_client.timeout_member(
            _guild(), body.user_id, session.discord_user_id, body.duration_minutes, body.reason
        )
    except InternalAPIError as exc:
        raise _translate(exc)


class UntimeoutBody(BaseModel):
    user_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.post("/untimeout", status_code=status.HTTP_201_CREATED)
async def untimeout_member(
    body: UntimeoutBody,
    session: Session = Depends(require_permission("moderate_members")),
) -> dict:
    try:
        return await internal_client.untimeout_member(_guild(), body.user_id, session.discord_user_id, body.reason)
    except InternalAPIError as exc:
        raise _translate(exc)


# ---------------------------------------------------------------------------
# `!purge` (202 job + poll) and `!msglog` (DM export)
# ---------------------------------------------------------------------------

_PURGE_WINDOW_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)
_PURGE_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def purge_amount_error(raw: str) -> tuple[str | None, bool]:
    """(error, needs_confirm) for a purge amount; mirrors
    services/purge_service.parse_purge_amount + needs_confirm."""
    text = raw.strip()
    if re.fullmatch(r"-?\d+", text):
        count = int(text)
        if count < PURGE_MIN or count > PURGE_MAX:
            return f"The purge count must be between {PURGE_MIN} and {PURGE_MAX}.", False
        return None, count > PURGE_CONFIRM_ABOVE
    match = _PURGE_WINDOW_RE.match(text)
    if not match:
        return "Enter a count from 1 to 1000, or a window like 30s, 15m, 2h or 1d (up to 14 days).", False
    seconds = int(match.group(1)) * _PURGE_UNIT_SECONDS[match.group(2).lower()]
    if seconds <= 0 or seconds > PURGE_MAX_WINDOW_SECONDS:
        return "The purge window must be between 1 second and 14 days.", False
    return None, True


@router.get("/purge-channels")
async def purge_channels(session: Session = Depends(require_permission("manage_messages_bulk"))) -> dict:
    """Text channels the session user can see (bot_can_purge per channel)."""
    try:
        return await internal_client.get_purge_channels(_guild(), actor_id=session.discord_user_id)
    except InternalAPIError as exc:
        raise _translate(exc)


class PurgeBody(BaseModel):
    channel_id: int = Field(**_SNOWFLAKE)
    amount: str = Field(min_length=1, max_length=16)
    user_id: int | None = Field(default=None, **_SNOWFLAKE)
    confirm: bool = False


@router.post("/purge", status_code=status.HTTP_202_ACCEPTED)
async def start_purge(
    body: PurgeBody,
    session: Session = Depends(require_permission("manage_messages_bulk")),
) -> dict:
    """Starts the purge as a bot-side job and answers at once (202); poll
    GET /purge/{job_id}. A big purge can outlast Cloudflare's 100 s limit."""
    error, needs_confirm = purge_amount_error(body.amount)
    if error:
        raise HTTPException(status_code=422, detail={"error": "invalid_input", "message": error})
    if needs_confirm and not body.confirm:
        raise HTTPException(status_code=409, detail={
            "error": "confirm_required",
            "message": "Purges of more than 100 messages and time-window purges must be confirmed.",
        })
    try:
        return await internal_client.start_purge(
            _guild(), moderator_id=session.discord_user_id, channel_id=body.channel_id,
            amount=body.amount.strip(), user_id=body.user_id, confirm=body.confirm,
        )
    except InternalAPIError as exc:
        raise _translate(exc)


@router.get("/purge/{job_id}")
async def get_purge_job(
    job_id: str = Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    session: Session = Depends(require_permission("manage_messages_bulk")),
) -> dict:
    try:
        return await internal_client.get_purge_job(_guild(), job_id)
    except InternalAPIError as exc:
        raise _translate(exc)


@router.get("/message-log/status")
async def message_log_status(session: Session = Depends(require_staff)) -> dict:
    try:
        return await internal_client.get_message_log_status(_guild())
    except InternalAPIError as exc:
        raise _translate(exc)


class MessageLogExportBody(BaseModel):
    kind: Literal["user", "channel", "deleted"]
    user_id: int | None = Field(default=None, **_SNOWFLAKE)
    channel_id: int | None = Field(default=None, **_SNOWFLAKE)
    window: str | None = Field(default=None, max_length=8, pattern=r"^\s*\d+\s*[mhdMHD]\s*$")


@router.post("/message-log/export")
async def message_log_export(
    body: MessageLogExportBody,
    session: Session = Depends(require_staff),
) -> dict:
    """Sends the export to the SESSION user's Discord DMs (like `!msglog`).
    The file is never returned here: it holds other members' messages."""
    if body.kind == "user" and body.user_id is None:
        raise HTTPException(status_code=422, detail={"error": "invalid_input", "message": "Pick a user."})
    if body.kind == "channel" and body.channel_id is None:
        raise HTTPException(status_code=422, detail={"error": "invalid_input", "message": "Pick a channel."})
    try:
        return await internal_client.export_message_log(
            _guild(), actor_id=session.discord_user_id, kind=body.kind,
            user_id=body.user_id if body.kind == "user" else None,
            channel_id=body.channel_id if body.kind == "channel" else None,
            window=body.window.strip() if body.window else None,
        )
    except InternalAPIError as exc:
        raise _translate(exc)
