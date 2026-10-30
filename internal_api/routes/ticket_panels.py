"""
POST /guilds/{guild_id}/ticket-panels
GET  /guilds/{guild_id}/categories

Panel CREATION only (distinct from internal_api/routes/tickets.py, which is
read + close/claim/archive + panel listing for already-existing panels).
Split into its own file rather than appended to tickets.py per the task's
own suggestion, and to keep tickets.py's re-read-then-append surface small
for other concurrent agents.

The create route calls the REAL services.ticket_service.TicketService —
resolved via internal_api.deps.get_ticket_service, the SAME accessor
tickets.py already uses — for BOTH steps:
  1. TicketService.create_panel(...)   -> writes the DB row
  2. TicketService.post_panel(panel_id, guild) -> posts the live Discord
     message with the persistent-custom_id button view and stores
     message_id back onto the row (ticket_repo.set_panel_message)
This is the exact same two-step sequence cogs/tickets.py's `/ticket setup`
slash command performs (see its ticket_setup handler), so a dashboard-
created panel is byte-for-byte indistinguishable from a command-created one
— same DB row shape, same TicketPanelView custom_id pattern, same
bot.add_view registration on next restart via cogs/tickets.py's setup().

post_panel failures (e.g. channel deleted between form load and submit,
missing Send Messages permission) do NOT roll back the DB row — this
matches TicketService.post_panel's own behavior when called from the slash
command (the panel row persists so staff can retry posting, rather than
silently losing the configured panel). The response surfaces
message_id: null plus post_errors so the frontend can show a clear
"created but not posted" state instead of a bare success.

`/categories` mirrors reaction_roles.py's `/channels` helper (same
guild-metadata-only shape, no service call) — added here rather than
editing that shared file, since the ticket panel builder form needs
category pickers (open/closed) and a transcript channel picker that
reaction_roles.py's text-channel-only endpoint doesn't cover.
"""

from __future__ import annotations

import discord
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from internal_api.auth import require_internal_secret
from internal_api.deps import get_guild, get_ticket_service
from internal_api.snowflakes import stringify_snowflakes

router = APIRouter(dependencies=[Depends(require_internal_secret)])


class CreateTicketPanelRequest(BaseModel):
    channel_id: int
    created_by: int
    template_name: str | None = None
    open_category_id: int | None = None
    closed_category_id: int | None = None
    transcript_channel_id: int | None = None
    max_open_per_user: int = 1
    naming_pattern: str = "ticket-{number}"
    button_label: str = "Open Ticket"
    button_emoji: str | None = None
    purpose: str = "support"


@router.post("/guilds/{guild_id}/ticket-panels", status_code=status.HTTP_201_CREATED)
async def create_ticket_panel(
    body: CreateTicketPanelRequest,
    guild: discord.Guild = Depends(get_guild),
    ticket_service=Depends(get_ticket_service),
) -> dict:
    channel = guild.get_channel(body.channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channel_id must be a text channel in this guild.",
        )

    open_category = guild.get_channel(body.open_category_id) if body.open_category_id else None
    closed_category = guild.get_channel(body.closed_category_id) if body.closed_category_id else None
    transcript_channel = guild.get_channel(body.transcript_channel_id) if body.transcript_channel_id else None

    if body.open_category_id and not isinstance(open_category, discord.CategoryChannel):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="open_category_id is not a category in this guild.")
    if body.closed_category_id and not isinstance(closed_category, discord.CategoryChannel):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="closed_category_id is not a category in this guild.")
    if body.transcript_channel_id and not isinstance(transcript_channel, discord.TextChannel):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="transcript_channel_id is not a text channel in this guild.")

    panel_id, errors = await ticket_service.create_panel(
        guild=guild,
        channel=channel,
        actor_id=body.created_by,
        open_category=open_category,
        closed_category=closed_category,
        transcript_channel=transcript_channel,
        template_name=body.template_name,
        max_open_per_user=body.max_open_per_user,
        naming_pattern=body.naming_pattern,
        button_label=body.button_label,
        button_emoji=body.button_emoji,
        purpose=body.purpose,
    )
    if errors:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=errors)

    message_id, post_errors = await ticket_service.post_panel(panel_id, guild)

    panels = await ticket_service.list_panels(guild.id)
    panel_row = next((p for p in panels if p["id"] == panel_id), None)

    return stringify_snowflakes(
        {
            "panel_id": panel_id,
            "message_id": message_id,
            "post_errors": post_errors,
            "panel": panel_row,
        }
    )


@router.get("/guilds/{guild_id}/categories")
async def list_categories(
    guild: discord.Guild = Depends(get_guild),
) -> list[dict]:
    """Category channels only, for the ticket panel builder's
    open-category / closed-category pickers.

    "id" here is the category's own Discord snowflake, not a DB row id —
    stringified explicitly since bare "id" is deliberately excluded from
    the shared SNOWFLAKE_KEYS allowlist (see internal_api/snowflakes.py).
    """
    return [{"id": str(c.id), "name": c.name, "position": c.position} for c in guild.categories]
