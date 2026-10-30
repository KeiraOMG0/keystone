"""
GET  /guilds/{guild_id}/tickets?status=open|closed|archived|all
GET  /guilds/{guild_id}/tickets/{ticket_id}
POST /guilds/{guild_id}/tickets/{ticket_id}/close
POST /guilds/{guild_id}/tickets/{ticket_id}/claim
POST /guilds/{guild_id}/tickets/{ticket_id}/archive
GET  /guilds/{guild_id}/ticket-panels

Wraps services.ticket_service.TicketService for the dashboard. The
close/claim/archive actions all need a LIVE discord.Member for the acting
staff member (not a bare ID) — the service methods mutate Discord state
(channel deletes, permission overwrites) and post lifecycle messages that
reference the actor. Resolved here via get_member, exactly like
permissions.py resolves the subject member.

close = lock the opener out and delete the channel 30 s later (same as the
Discord Close button); archive = delete the channel now. The dashboard UI
only offers close; archive stays for API callers.

Ticket rows are plain dicts straight from database/repositories/ticket_repo.py
(get_ticket / list_open_tickets / list_by_status / list_all) — including the
joined `panel_purpose` column. Returned as-is, no reshaping.
"""

from __future__ import annotations

import discord
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from discord.ext import commands

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild, get_member, get_ticket_service
from internal_api.snowflakes import stringify_snowflakes

router = APIRouter(dependencies=[Depends(require_internal_secret)])

_VALID_STATUSES = {"open", "closed", "archived"}


@router.get("/guilds/{guild_id}/tickets")
async def list_tickets(
    status_filter: str = "open",
    guild: discord.Guild = Depends(get_guild),
    ticket_service=Depends(get_ticket_service),
) -> list[dict]:
    if status_filter == "all":
        return stringify_snowflakes(await ticket_service.list_all_tickets(guild.id))
    if status_filter not in _VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status '{status_filter}'. Must be one of: open, closed, archived, all.",
        )
    if status_filter == "open":
        return stringify_snowflakes(await ticket_service.list_open_tickets(guild.id))
    return stringify_snowflakes(await ticket_service.list_tickets_by_status(guild.id, status_filter))


@router.get("/guilds/{guild_id}/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: int,
    guild: discord.Guild = Depends(get_guild),
    ticket_service=Depends(get_ticket_service),
) -> dict:
    ticket = await ticket_service.get_ticket(ticket_id)
    if not ticket or ticket["guild_id"] != guild.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "ticket_not_found", "message": "Ticket not found."})
    return stringify_snowflakes(ticket)


class TicketActionRequest(BaseModel):
    actor_user_id: int


def _raise_for_errors(errors: list[str]) -> None:
    """Ticket service action methods return a list[str] of human-readable
    errors instead of raising. "... not found" maps to 404 (code
    "ticket_not_found"); any other error (e.g. "already closed") maps to 400
    with code "ticket_action_refused". The body follows the dashboard error
    contract, {"error": code, "message": text}, so server/routes/tickets.py
    can pass it through instead of turning it into a 502."""
    if not errors:
        return
    message = "; ".join(errors)
    if any("not found" in e.lower() for e in errors):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "ticket_not_found", "message": message},
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "ticket_action_refused", "message": message},
    )


async def _resolve_actor(user_id: int, guild: discord.Guild) -> discord.Member:
    """get_member, but a missing actor is a 404 with a readable dict body
    (not the ticket's 404) so the dashboard can tell the two apart."""
    try:
        return await get_member(user_id, guild)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "actor_not_found", "message": "You are not a member of the server (or Discord could not find you)."},
            ) from exc
        raise


@router.post("/guilds/{guild_id}/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: int,
    body: TicketActionRequest,
    guild: discord.Guild = Depends(get_guild),
    ticket_service=Depends(get_ticket_service),
) -> dict:
    actor = await _resolve_actor(body.actor_user_id, guild)
    errors = await ticket_service.close_ticket(ticket_id, guild, actor)
    _raise_for_errors(errors)
    return {"success": True}


@router.post("/guilds/{guild_id}/tickets/{ticket_id}/claim")
async def claim_ticket(
    ticket_id: int,
    body: TicketActionRequest,
    guild: discord.Guild = Depends(get_guild),
    ticket_service=Depends(get_ticket_service),
) -> dict:
    actor = await _resolve_actor(body.actor_user_id, guild)
    errors = await ticket_service.claim_ticket(ticket_id, guild, actor)
    _raise_for_errors(errors)
    return {"success": True}


@router.post("/guilds/{guild_id}/tickets/{ticket_id}/archive")
async def archive_ticket(
    ticket_id: int,
    body: TicketActionRequest,
    guild: discord.Guild = Depends(get_guild),
    ticket_service=Depends(get_ticket_service),
) -> dict:
    actor = await _resolve_actor(body.actor_user_id, guild)
    errors = await ticket_service.archive_ticket(ticket_id, guild, actor)
    _raise_for_errors(errors)
    return {"success": True}


@router.get("/guilds/{guild_id}/ticket-panels")
async def list_ticket_panels(
    guild: discord.Guild = Depends(get_guild),
    ticket_service=Depends(get_ticket_service),
) -> list[dict]:
    return stringify_snowflakes(await ticket_service.list_panels(guild.id))


# ---------------------------------------------------------------------------
# Archived-ticket deletion (dashboard) + chat-log retrieval. Appended for the
# claim-permission-fix / auto-purge / chat-log task — see
# services/ticket_service.py's delete_ticket / get_ticket_messages for the
# actual logic; these routes are thin wraps, matching the shape above.
# ---------------------------------------------------------------------------

@router.delete("/guilds/{guild_id}/tickets/{ticket_id}")
async def delete_ticket(
    ticket_id: int,
    actor_user_id: int | None = None,
    guild: discord.Guild = Depends(get_guild),
    ticket_service=Depends(get_ticket_service),
) -> dict:
    """`actor_user_id` (query param) is only used for the audit_log row;
    the dashboard always sends the session user."""
    errors = await ticket_service.delete_ticket(ticket_id, guild, actor_id=actor_user_id)
    _raise_for_errors(errors)
    return {"success": True}


@router.get("/guilds/{guild_id}/tickets/{ticket_id}/messages")
async def get_ticket_messages(
    ticket_id: int,
    guild: discord.Guild = Depends(get_guild),
    ticket_service=Depends(get_ticket_service),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    """Returns {"enabled": bool, "messages": [...]}. `enabled` reflects the
    "Enable ticket chat logging" dashboard setting (services/
    settings_service.py's "ticket_chat_logging_enabled", live-reloadable —
    see that module's docstring; config.TICKET_CHAT_LOGGING_ENABLED remains
    the .env-seeded fallback for a first-run/empty settings table) at
    request time — when False, the ticket may still legitimately have zero
    messages (logging was always off), so this flag lets the dashboard show
    "chat logging is disabled" instead of an empty-but-misleading list."""
    ticket = await ticket_service.get_ticket(ticket_id)
    if not ticket or ticket["guild_id"] != guild.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "ticket_not_found", "message": "Ticket not found."})

    enabled = await _ticket_chat_logging_enabled(bot, guild.id)
    if not enabled:
        return {"enabled": False, "messages": []}

    messages, errors = await ticket_service.get_ticket_messages(ticket_id, guild)
    _raise_for_errors(errors)
    return stringify_snowflakes({"enabled": True, "messages": messages})


async def _ticket_chat_logging_enabled(bot: commands.Bot, guild_id: int) -> bool:
    """Same TTL-cache bridge cogs/tickets.py's TicketCog uses (services/
    settings_service.py's get_cached/set_cached), so a dashboard toggle and
    this read-only status check stay consistent without each maintaining
    their own cache. Falls back to config.TICKET_CHAT_LOGGING_ENABLED if
    bot.db isn't available for some reason."""
    import config as _config
    import services.settings_service as settings_module

    db = getattr(bot, "db", None)
    if db is None:
        return _config.TICKET_CHAT_LOGGING_ENABLED

    cached = settings_module.get_cached("ticket_chat_logging_enabled")
    if cached is not None:
        return cached

    from database.repositories.settings_repo import SettingsRepository
    from services.settings_service import SettingsService

    effective = await SettingsService(SettingsRepository(db)).get_effective(guild_id)
    value = effective["ticket_chat_logging_enabled"]
    settings_module.set_cached("ticket_chat_logging_enabled", value)
    return value
