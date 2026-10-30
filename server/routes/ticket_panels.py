"""
POST /api/ticket-panels
GET  /api/categories
GET  /api/ticket-panel-channels

Panel CREATION only — split from server/routes/tickets.py (read + close/
claim/archive + panel listing) for the same reason its internal_api
counterpart is split: keeps tickets.py's re-read-then-append surface small.

Gated with "manage_tickets", the SAME permission server/routes/tickets.py
already uses for the whole tickets page — panel creation is a ticket-system
management action, not a distinct concept, so reusing that permission
string (rather than inventing a new one) is intentional and matches this
task's own guidance to keep gating consistent with what already gates the
tickets page.

created_by is always populated from session.discord_user_id, never trusted
from the request body, matching every other POST route in this dashboard
backend (tickets.py, moderation.py).

This actually posts a live Discord message via internal_api's
create_ticket_panel (see that route's docstring) — the response's
`message_id` will be null and `post_errors` non-empty if the post step
failed (e.g. missing channel permissions), even though panel_id was
created successfully. The frontend must check post_errors, not just HTTP
status, to detect a "created but not posted" panel.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api", tags=["ticket-panels"])


def _internal_api_error_response(exc: InternalAPIError) -> HTTPException:
    """Dashboard error contract: a bot refusal is a 4xx with a readable body;
    502 internal_api_unavailable only when the bot could not answer (no
    response or a 5xx)."""
    code = exc.status_code
    if code == 400:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "validation_error", "detail": str(exc)},
        )
    if code in (404, 409, 422):
        detail = getattr(exc, "detail", None)
        return HTTPException(
            status_code=400 if code == 422 else code,
            detail=detail if isinstance(detail, dict) else {"error": "invalid_request", "message": str(detail or exc)},
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"error": "internal_api_unavailable"},
    )


class CreateTicketPanelBody(BaseModel):
    channel_id: int
    template_name: str | None = None
    open_category_id: int | None = None
    closed_category_id: int | None = None
    transcript_channel_id: int | None = None
    max_open_per_user: int = 1
    naming_pattern: str = "ticket-{number}"
    button_label: str = "Open Ticket"
    button_emoji: str | None = None
    purpose: str = "support"


@router.post("/ticket-panels", status_code=status.HTTP_201_CREATED)
async def create_ticket_panel(
    body: CreateTicketPanelBody,
    session: Session = Depends(require_permission("manage_tickets")),
) -> dict:
    try:
        return await internal_client.create_ticket_panel(
            config.DASHBOARD_GUILD_ID,
            channel_id=body.channel_id,
            created_by=session.discord_user_id,
            template_name=body.template_name,
            open_category_id=body.open_category_id,
            closed_category_id=body.closed_category_id,
            transcript_channel_id=body.transcript_channel_id,
            max_open_per_user=body.max_open_per_user,
            naming_pattern=body.naming_pattern,
            button_label=body.button_label,
            button_emoji=body.button_emoji,
            purpose=body.purpose,
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/categories")
async def list_categories(
    session: Session = Depends(require_permission("manage_tickets")),
) -> list[dict]:
    try:
        return await internal_client.list_guild_categories(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/ticket-panel-channels")
async def list_channels(
    session: Session = Depends(require_permission("manage_tickets")),
) -> list[dict]:
    """Reuses internal_client.list_guild_channels (already wraps the
    existing GET /guilds/{guild_id}/channels added for the reaction-roles
    panel builder) — same underlying internal_api route, no need to
    duplicate it there. Named distinctly (not /api/channels) because
    server/routes/reaction_roles.py already registers /api/channels gated
    on "manage_reaction_roles" — a ticket-panel-builder-only staff member
    without that permission would get an unexpected 403 reusing that path,
    so this is a separate route on the SAME data gated on "manage_tickets"
    instead."""
    try:
        return await internal_client.list_guild_channels(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)
