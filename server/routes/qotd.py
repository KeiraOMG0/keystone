"""
GET  /api/qotd/config
PUT  /api/qotd/config
GET  /api/qotd/history
GET  /api/qotd/models
GET  /api/qotd/fallback-questions
POST /api/qotd/generate            -> 202 {job_id, status, ...} (starts a generation job)
GET  /api/qotd/generate/{job_id}   -> {job_id, status: running|done|failed, question, source, errors, error}
POST /api/qotd/post

Thin, permission-gated client of internal_api/routes/qotd.py, following the
exact same shape as server/routes/tickets.py / moderation.py:
require_permission("manage_qotd") first, then delegate to
services.internal_client, translating InternalAPIError into the same
{"error": "internal_api_unavailable"} 502 body used elsewhere. "manage_qotd"
is an existing permission string (services/permission_service.py's
VALID_PERMISSIONS) — the same one cogs/qotd.py's `/qotd` slash-command group
already requires via bot.permission_service.has_permission — reused as-is,
not reinvented.

POST /qotd/post populates the acting staff member (for audit-log attribution
only) from session.discord_user_id — never from the request body — matching
moderation.py's/tickets.py's moderator_id/actor_user_id handling.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/qotd", tags=["qotd"])

_PERMISSION = "manage_qotd"


def _internal_api_error_response(exc: InternalAPIError) -> HTTPException:
    """Dashboard error contract (same shape as server/routes/role_admin.py):
    the bot's {"error", "message"} body is passed through with a 4xx; a
    Discord refusal (bot 403) becomes 409 so it is never mistaken for a
    dashboard permission failure; 502 internal_api_unavailable only means
    the bot could not be reached."""
    code = exc.status_code
    detail = exc.detail if isinstance(exc.detail, dict) else None
    if code == 404:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail or {"error": "not_found"})
    if code in (400, 409, 422):
        return HTTPException(status_code=400 if code == 422 else code, detail=detail or {"error": "invalid_request"})
    if code == 403:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail or {"error": "discord_forbidden", "message": "Discord refused the post."},
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


@router.get("/config")
async def get_config(
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.get_qotd_config(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/channels")
async def list_channels(
    session: Session = Depends(require_permission(_PERMISSION)),
) -> list[dict]:
    """Text-channel picker for the QOTD config form (public channel +
    approval channel). Own endpoint (not server/routes/reaction_roles.py's
    GET /api/channels) because that one is gated on "manage_reaction_roles" —
    a moderator holding only "manage_qotd" must still be able to use this
    page. Hits the same ungated internal_api GET /guilds/{id}/channels data
    (ungated there because internal_api's only gate is the shared secret;
    per-feature permission gating happens at this dashboard-backend layer),
    mirroring GET /api/reaction-roles/roles's identical rationale for role
    picking below."""
    try:
        return await internal_client.list_guild_channels(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/roles")
async def list_roles(
    session: Session = Depends(require_permission(_PERMISSION)),
) -> list[dict]:
    """Role picker for the public/staff ping-role fields. Own endpoint for
    the same reason as list_channels above — GET /api/permissions/roles
    requires full require_staff, GET /api/reaction-roles/roles requires
    "manage_reaction_roles"; this hits the same underlying
    internal_client.list_guild_roles data gated on "manage_qotd" instead."""
    try:
        return await internal_client.list_guild_roles(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


class UpsertConfigBody(BaseModel):
    channel_id: int | None = None
    post_hour: int | None = None
    post_minute: int | None = None
    enabled: bool | None = None
    model: str | None = None
    use_emojis: bool | None = None
    ping_role_id: int | None = None
    clear_ping_role: bool = False
    staff_ping_role_id: int | None = None
    clear_staff_ping_role: bool = False
    approval_channel_id: int | None = None
    clear_approval_channel: bool = False


@router.put("/config")
async def upsert_config(
    body: UpsertConfigBody,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.upsert_qotd_config(config.DASHBOARD_GUILD_ID, **body.model_dump())
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/history")
async def get_history(
    limit: int = 50,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.get_qotd_history(config.DASHBOARD_GUILD_ID, limit)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/models")
async def get_models(
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.get_qotd_models(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/fallback-questions")
async def get_fallback_questions(
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.get_qotd_fallback_questions(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


class GenerateBody(BaseModel):
    model: str | None = None


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate(
    body: GenerateBody,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    """Starts a generation job on the bot and returns it immediately; poll
    GET /api/qotd/generate/{job_id}. Both calls are fast, so nothing here
    can hit Cloudflare's 100 s limit however long the model takes."""
    try:
        return await internal_client.start_qotd_generation(config.DASHBOARD_GUILD_ID, body.model)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/generate/{job_id}")
async def generate_status(
    job_id: str = Path(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.get_qotd_generation(config.DASHBOARD_GUILD_ID, job_id)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


class PostBody(BaseModel):
    question: str
    source: str
    channel_id: int
    use_emojis: bool = False
    ping_role_id: int | None = None


@router.post("/post")
async def post_qotd(
    body: PostBody,
    session: Session = Depends(require_permission(_PERMISSION)),
) -> dict:
    try:
        return await internal_client.post_qotd(
            config.DASHBOARD_GUILD_ID,
            question=body.question,
            source=body.source,
            channel_id=body.channel_id,
            use_emojis=body.use_emojis,
            ping_role_id=body.ping_role_id,
            actor_user_id=session.discord_user_id,
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)
