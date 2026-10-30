"""
GET /guilds/{guild_id}/audit           one page of audit_log rows, newest first
GET /guilds/{guild_id}/audit/actions   distinct action names (+ counts) for the filter dropdown

Read-only view of the bot's `audit_log` table for the dashboard Audit log page
(server/routes/audit.py gates it on view_audit). Filters: action, actor_id,
target_id, target_type, since (inclusive), until (exclusive), all optional and
ANDed. Pagination is keyset on the autoincrement id: pass the previous page's
`next_before_id` as `before_id`; `next_before_id` is null on the last page.

Entries use services.audit_service.serialize_row (the same shape as the live
`audit` event): snowflakes, target_id and oversized ints inside `detail` are
strings, `created_at` is ISO UTC with a trailing Z, a detail_json that does not
parse comes back as `detail_raw` instead of failing the page. List entries also
carry best-effort `actor_name` / `target_name` from the bot's cache (live
events do not).
"""

from __future__ import annotations

import datetime

from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException, Query, status

from database.repositories.audit_repo import AuditRepository
from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot
from services.audit_service import AuditService

router = APIRouter(dependencies=[Depends(require_internal_secret)])

MAX_LIMIT = 200
_SQLITE_MAX_INT = 2**63 - 1


def _service(bot: commands.Bot) -> AuditService:
    db = getattr(bot, "db", None)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "not_ready", "message": "The bot database is not ready yet."},
        )
    return AuditService(AuditRepository(db))


@router.get("/guilds/{guild_id}/audit")
async def list_audit(
    guild_id: int,
    action: str | None = Query(None, min_length=1, max_length=100),
    actor_id: int | None = Query(None, ge=0, le=_SQLITE_MAX_INT),
    target_id: int | None = Query(None, ge=0, le=_SQLITE_MAX_INT),
    target_type: str | None = Query(None, min_length=1, max_length=50),
    since: datetime.datetime | None = Query(None),
    until: datetime.datetime | None = Query(None),
    before_id: int | None = Query(None, ge=1, le=_SQLITE_MAX_INT),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    if since is not None and until is not None and _utc(since) >= _utc(until):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_range", "message": "The start of the date range must be before its end."},
        )
    page = await _service(bot).query(
        guild_id, action=action, actor_id=actor_id, target_id=target_id, target_type=target_type,
        since=since, until=until, before_id=before_id, limit=limit,
    )
    _add_names(bot, guild_id, page["entries"])
    return page


def _add_names(bot, guild_id: int, entries: list[dict]) -> None:
    """Best-effort `actor_name` / `target_name` from the bot's cache (no API
    calls), so viewers without staff_base (who cannot use /api/resolve) still
    see names. None when the id is unknown or gone."""
    guild = bot.get_guild(guild_id) if hasattr(bot, "get_guild") else None

    def user_name(raw: str | None) -> str | None:
        if not raw or not raw.isdigit() or raw == "0":
            return None
        uid = int(raw)
        member = guild.get_member(uid) if guild is not None else None
        if member is not None:
            return _text(getattr(member, "display_name", None))
        user = bot.get_user(uid) if hasattr(bot, "get_user") else None
        return _text(getattr(user, "name", None)) if user is not None else None

    def target_name(kind: str | None, raw: str | None) -> str | None:
        if not raw or not raw.isdigit() or guild is None:
            return None
        if kind in ("member", "user"):
            return user_name(raw)
        if kind == "role":
            role = guild.get_role(int(raw))
            return _text(getattr(role, "name", None)) if role is not None else None
        if kind == "channel":
            channel = guild.get_channel(int(raw))
            return _text(getattr(channel, "name", None)) if channel is not None else None
        return None

    for entry in entries:
        try:
            entry["actor_name"] = user_name(entry.get("actor_id"))
            entry["target_name"] = target_name(entry.get("target_type"), entry.get("target_id"))
        except Exception:  # names are decoration; never fail the page over them
            entry.setdefault("actor_name", None)
            entry.setdefault("target_name", None)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


@router.get("/guilds/{guild_id}/audit/actions")
async def list_audit_actions(
    guild_id: int,
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    return {"actions": await _service(bot).list_actions(guild_id)}


def _utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)
