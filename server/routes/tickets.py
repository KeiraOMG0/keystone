"""
GET  /api/tickets?status=open|closed|archived|all
GET  /api/tickets/{ticket_id}
POST /api/tickets/{ticket_id}/close
POST /api/tickets/{ticket_id}/claim
POST /api/tickets/{ticket_id}/archive
GET  /api/ticket-panels

Thin, permission-gated client of Step 1's internal API ticket routes
(internal_api/routes/tickets.py), following the exact same shape as
server/routes/moderation.py: require_permission("manage_tickets") first,
then delegate to services.internal_client, translating InternalAPIError
into the same {"error": "internal_api_unavailable"} 502 body used
elsewhere. 404s from the internal API (ticket not found) are translated to
a 404 with {"error": "ticket_not_found"}.

POST actions populate the acting staff member from session.discord_user_id
— never from the request body — so a caller can't attribute a close/claim/
archive to someone else, matching moderation.py's moderator_id handling.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api", tags=["tickets"])


def _internal_api_error_response(exc: InternalAPIError) -> HTTPException:
    """Dashboard error contract: a ticket-service refusal ("already closed",
    "only archived tickets can be deleted") or a Discord refusal is a 4xx
    with the bot's readable {"error", "message"} body; 502
    internal_api_unavailable only means the bot could not be reached."""
    code = exc.status_code
    detail = exc.detail if isinstance(exc.detail, dict) else None
    if code == 404:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail or {"error": "ticket_not_found"})
    if code in (400, 409, 422):
        return HTTPException(status_code=400 if code == 422 else code, detail=detail or {"error": "invalid_request"})
    if code == 403:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail or {"error": "discord_forbidden", "message": "Discord refused the change."},
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"error": "internal_api_unavailable"},
    )


@router.get("/tickets")
async def list_tickets(
    status: str = "open",
    session: Session = Depends(require_permission("manage_tickets")),
) -> list[dict]:
    try:
        return await internal_client.list_tickets(config.DASHBOARD_GUILD_ID, status)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: int,
    session: Session = Depends(require_permission("manage_tickets")),
) -> dict:
    try:
        return await internal_client.get_ticket(config.DASHBOARD_GUILD_ID, ticket_id)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.post("/tickets/{ticket_id}/close")
async def close_ticket(
    ticket_id: int,
    session: Session = Depends(require_permission("manage_tickets")),
) -> dict:
    try:
        return await internal_client.close_ticket(
            config.DASHBOARD_GUILD_ID, ticket_id, session.discord_user_id
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.post("/tickets/{ticket_id}/claim")
async def claim_ticket(
    ticket_id: int,
    session: Session = Depends(require_permission("manage_tickets")),
) -> dict:
    try:
        return await internal_client.claim_ticket(
            config.DASHBOARD_GUILD_ID, ticket_id, session.discord_user_id
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.post("/tickets/{ticket_id}/archive")
async def archive_ticket(
    ticket_id: int,
    session: Session = Depends(require_permission("manage_tickets")),
) -> dict:
    try:
        return await internal_client.archive_ticket(
            config.DASHBOARD_GUILD_ID, ticket_id, session.discord_user_id
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/ticket-panels")
async def list_ticket_panels(
    session: Session = Depends(require_permission("manage_tickets")),
) -> list[dict]:
    try:
        return await internal_client.list_ticket_panels(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


# ---------------------------------------------------------------------------
# Archived-ticket deletion + chat-log retrieval. Appended for the
# claim-permission-fix / auto-purge / chat-log task. Same
# require_permission("manage_tickets") gate as every other route above.
# ---------------------------------------------------------------------------

@router.delete("/tickets/{ticket_id}")
async def delete_ticket(
    ticket_id: int,
    session: Session = Depends(require_permission("manage_tickets")),
) -> dict:
    try:
        return await internal_client.delete_ticket(
            config.DASHBOARD_GUILD_ID, ticket_id, actor_user_id=session.discord_user_id
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/tickets/{ticket_id}/messages")
async def get_ticket_messages(
    ticket_id: int,
    session: Session = Depends(require_permission("manage_tickets")),
) -> dict:
    try:
        return await internal_client.get_ticket_messages(config.DASHBOARD_GUILD_ID, ticket_id)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)
