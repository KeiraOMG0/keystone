"""
Birthdays page (/dashboard/birthdays). Every route requires the bot permission
`manage_birthdays` (staff bypass), the same grant cogs/birthday_config.py,
cogs/birthday_override.py and cogs/birthday_io.py check. Thin client of
internal_api/routes/birthdays.py; the acting user is always the session user.

GET    /api/birthdays                    settings + status + entries + recent/upcoming
GET    /api/birthdays/timezones          IANA zone names the bot accepts
GET    /api/birthdays/roles              role picker with blocked_reason (self-role policy, for you)
GET    /api/birthdays/channels           text-channel picker
GET    /api/birthdays/members/search?q=  member typeahead (add a birthday / test announcement)
PUT    /api/birthdays/config             partial config
PUT    /api/birthdays/entries/{user_id}  add / edit a member's birthday (+ timezone)
DELETE /api/birthdays/entries/{user_id}  remove a member's birthday
POST   /api/birthdays/test               send a test announcement (1-5 members)
POST   /api/birthdays/reset-timers       re-trigger today's birthdays
GET    /api/birthdays/export?format=     {"filename", "content", "count"} (plaintext | csv | ics)
POST   /api/birthdays/import             {"filename", "content", "dry_run"} preview / import (.txt, <= 2 MB)

Errors: bot refusals pass through as {"error", "message"[, "fields"]} with the
bot's 4xx status; 502 internal_api_unavailable only when the bot can't answer;
503 feature_unavailable when the birthday cogs are not loaded.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/birthdays", tags=["birthdays"])

PERMISSION = "manage_birthdays"
ANNOUNCE_MESSAGE_MAX = 1500  # services/birthday_service.py ANNOUNCE_MESSAGE_MAX
IMPORT_MAX_BYTES = 2_000_000  # services/birthday_service.py IMPORT_MAX_BYTES
TEST_MEMBERS_MAX = 5
_FORMATS = ("plaintext", "csv", "ics")

_guard = Depends(require_permission(PERMISSION))


def _error(exc: InternalAPIError) -> HTTPException:
    detail = exc.detail
    if exc.status_code in (400, 403, 404, 409, 422) and isinstance(detail, dict) and "error" in detail:
        body = {"error": str(detail["error"]), "message": str(detail.get("message") or "")}
        if isinstance(detail.get("fields"), dict):
            body["fields"] = {str(k): str(v) for k, v in detail["fields"].items()}
        return HTTPException(status_code=exc.status_code, detail=body)
    if exc.status_code == 422:
        return HTTPException(status_code=422, detail={"error": "invalid_request", "message": "Invalid request."})
    if exc.status_code == 503:
        return HTTPException(status_code=503, detail={"error": "feature_unavailable", "message": "The birthday feature is not loaded on the bot."})
    return HTTPException(status_code=502, detail={"error": "internal_api_unavailable"})


def _snowflake(value: str | None, field: str = "id") -> int | None:
    if value in (None, ""):
        return None
    if not value.isdigit() or len(value) > 20:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": f"Invalid {field}."})
    return int(value)


async def _call(coro):
    try:
        return await coro
    except InternalAPIError as exc:
        raise _error(exc) from exc


@router.get("")
async def overview(session: Session = _guard) -> dict:
    return await _call(internal_client.get_birthdays(config.DASHBOARD_GUILD_ID))


@router.get("/timezones")
async def timezones(session: Session = _guard) -> dict:
    return await _call(internal_client.get_birthday_timezones(config.DASHBOARD_GUILD_ID))


@router.get("/roles")
async def roles(session: Session = _guard) -> dict:
    return await _call(internal_client.get_birthday_role_options(config.DASHBOARD_GUILD_ID, session.discord_user_id))


@router.get("/channels")
async def channels(session: Session = _guard) -> list[dict]:
    return await _call(internal_client.list_guild_channels(config.DASHBOARD_GUILD_ID))


@router.get("/members/search")
async def member_search(
    q: str = Query("", max_length=100),
    limit: int = Query(10, ge=1, le=25),
    session: Session = _guard,
) -> dict:
    return await _call(internal_client.search_members(config.DASHBOARD_GUILD_ID, q, limit))


class ConfigBody(BaseModel):
    # Snowflakes arrive as strings (never Number() in the browser).
    birthday_role_id: str | None = None
    clear_birthday_role: bool = False
    announce_channel_id: str | None = None
    clear_announce_channel: bool = False
    guild_timezone: str | None = Field(default=None, max_length=64)
    clear_guild_timezone: bool = False
    announce_message: str | None = Field(default=None, max_length=ANNOUNCE_MESSAGE_MAX)
    announce_message_pl: str | None = Field(default=None, max_length=ANNOUNCE_MESSAGE_MAX)
    set_messages: bool = False
    announce_ping: bool | None = None
    ping_user: bool | None = None
    add_only: bool | None = None
    private_confirms: bool | None = None


@router.put("/config")
async def put_config(body: ConfigBody, session: Session = _guard) -> dict:
    values = body.model_dump()
    values["birthday_role_id"] = _snowflake(body.birthday_role_id, "role")
    values["announce_channel_id"] = _snowflake(body.announce_channel_id, "channel")
    values["actor_id"] = session.discord_user_id
    return await _call(internal_client.update_birthday_config(config.DASHBOARD_GUILD_ID, values))


class EntryBody(BaseModel):
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    timezone: str | None = Field(default=None, max_length=64)
    clear_timezone: bool = False


@router.put("/entries/{user_id}")
async def put_entry(user_id: str, body: EntryBody, session: Session = _guard) -> dict:
    uid = _snowflake(user_id, "user id")
    values = {**body.model_dump(), "actor_id": session.discord_user_id}
    return await _call(internal_client.set_birthday_entry(config.DASHBOARD_GUILD_ID, uid, values))


@router.delete("/entries/{user_id}")
async def delete_entry(user_id: str, session: Session = _guard) -> dict:
    uid = _snowflake(user_id, "user id")
    return await _call(internal_client.remove_birthday_entry(config.DASHBOARD_GUILD_ID, uid, session.discord_user_id))


class TestBody(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=TEST_MEMBERS_MAX)


@router.post("/test")
async def test_announcement(body: TestBody, session: Session = _guard) -> dict:
    ids = [_snowflake(u, "user id") for u in body.user_ids]
    return await _call(
        internal_client.birthday_test_announcement(config.DASHBOARD_GUILD_ID, session.discord_user_id, ids)
    )


@router.post("/reset-timers")
async def reset_timers(session: Session = _guard) -> dict:
    return await _call(internal_client.birthday_reset_timers(config.DASHBOARD_GUILD_ID, session.discord_user_id))


@router.get("/export")
async def export(format: str = Query("plaintext"), session: Session = _guard) -> dict:
    if format not in _FORMATS:
        raise HTTPException(status_code=422, detail={"error": "invalid_format", "message": "Format must be plaintext, csv or ics."})
    return await _call(internal_client.export_birthdays(config.DASHBOARD_GUILD_ID, format, session.discord_user_id))


class ImportBody(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=IMPORT_MAX_BYTES)
    dry_run: bool = True


@router.post("/import")
async def import_birthdays(body: ImportBody, session: Session = _guard) -> dict:
    if not body.filename.lower().endswith(".txt"):
        raise HTTPException(status_code=422, detail={"error": "invalid_file", "message": "Please upload a .txt file."})
    if len(body.content.encode("utf-8")) > IMPORT_MAX_BYTES:
        raise HTTPException(status_code=422, detail={"error": "file_too_large", "message": "File too large (max 2 MB)."})
    return await _call(
        internal_client.import_birthdays(
            config.DASHBOARD_GUILD_ID, actor_id=session.discord_user_id,
            filename=body.filename, content=body.content, dry_run=body.dry_run,
        )
    )
