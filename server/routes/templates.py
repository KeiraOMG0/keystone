"""
GET    /api/templates?category=
GET    /api/templates/{template_id}
POST   /api/templates
PATCH  /api/templates/{template_id}
PUT    /api/templates/{template_id}/embeds
DELETE /api/templates/{template_id}

Thin, permission-gated client of internal_api/routes/templates.py, following
the exact same shape as server/routes/tickets.py: require_permission
("manage_templates") first, then delegate to services.internal_client,
translating InternalAPIError into the same {"error": "internal_api_unavailable"}
502 body used elsewhere. 404s (template not found) translate to a 404 with
{"error": "template_not_found"}; 400s (validation errors — bad name, dup
name) translate to a 400 with {"error": "validation_error", "detail": [...]}.

"manage_templates" already existed as a registered permission string in
services/permission_service.py's VALID_PERMISSIONS before this file was
written — reused as-is, not invented.

created_by / actor_id are always populated from session.discord_user_id,
never trusted from the request body, matching tickets.py's moderator_id
handling (a caller can't attribute a create/edit to someone else).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server import config
from server.db.models import Session
from server.permissions.guards import require_permission
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api", tags=["templates"])


def _internal_api_error_response(exc: InternalAPIError) -> HTTPException:
    if exc.status_code == 404:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "template_not_found"},
        )
    if exc.status_code == 400:
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "validation_error", "detail": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"error": "internal_api_unavailable"},
    )


@router.get("/templates")
async def list_templates(
    category: str | None = None,
    session: Session = Depends(require_permission("manage_templates")),
) -> list[dict]:
    try:
        return await internal_client.list_templates(config.DASHBOARD_GUILD_ID, category)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/templates/channels")
async def list_template_channels(
    session: Session = Depends(require_permission("manage_templates")),
) -> list[dict]:
    """Text-channel picker for the "send to channel" action, gated on
    "manage_templates" — deliberately separate from reaction_roles.py's own
    /api/channels (gated on "manage_reaction_roles"), since a staff member
    can hold one permission without the other. Same underlying
    internal_client.list_guild_channels call, just re-exposed under this
    file's permission so the templates page doesn't need an unrelated grant.
    Registered ABOVE /templates/{template_id} so "channels" is never treated
    as a template_id path param."""
    try:
        return await internal_client.list_guild_channels(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/templates/placeholders")
async def list_placeholders(
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    """Every template placeholder with a description and an example value
    rendered by the bot for the signed-in user (core/placeholder_engine.py).
    Registered ABOVE /templates/{template_id} like /templates/channels."""
    try:
        return await internal_client.list_placeholders(config.DASHBOARD_GUILD_ID, user_id=session.discord_user_id)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.get("/templates/{template_id}")
async def get_template(
    template_id: int,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.get_template(config.DASHBOARD_GUILD_ID, template_id)
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


class CreateTemplateBody(BaseModel):
    name: str
    description: str | None = None
    category: str | None = None
    content: str | None = None


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    body: CreateTemplateBody,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.create_template(
            config.DASHBOARD_GUILD_ID,
            body.name,
            session.discord_user_id,
            description=body.description,
            category=body.category,
            content=body.content,
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


class UpdateTemplateBody(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    content: str | None = None


@router.patch("/templates/{template_id}")
async def update_template(
    template_id: int,
    body: UpdateTemplateBody,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.update_template(
            config.DASHBOARD_GUILD_ID,
            template_id,
            session.discord_user_id,
            name=body.name,
            description=body.description,
            category=body.category,
            content=body.content,
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


class EmbedFieldBody(BaseModel):
    name: str
    value: str
    inline: bool = False
    position: int | None = None


class EmbedBody(BaseModel):
    title: str | None = None
    description: str | None = None
    color: int | None = None
    url: str | None = None
    footer_text: str | None = None
    footer_icon_url: str | None = None
    thumbnail_url: str | None = None
    image_url: str | None = None
    author_name: str | None = None
    author_icon_url: str | None = None
    author_url: str | None = None
    timestamp_mode: str = "none"
    fields: list[EmbedFieldBody] = []


class ReplaceEmbedsBody(BaseModel):
    embeds: list[EmbedBody]


@router.put("/templates/{template_id}/embeds")
async def replace_template_embeds(
    template_id: int,
    body: ReplaceEmbedsBody,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.replace_template_embeds(
            config.DASHBOARD_GUILD_ID,
            template_id,
            session.discord_user_id,
            [e.model_dump() for e in body.embeds],
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: int,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        await internal_client.delete_template(config.DASHBOARD_GUILD_ID, template_id, session.discord_user_id)
        return {"success": True}
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


class PostTemplateBody(BaseModel):
    channel_id: int


@router.post("/templates/{template_id}/post", status_code=status.HTTP_201_CREATED)
async def post_template(
    template_id: int,
    body: PostTemplateBody,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.post_template(
            config.DASHBOARD_GUILD_ID,
            template_id,
            body.channel_id,
            session.discord_user_id,
        )
    except InternalAPIError as exc:
        raise _internal_api_error_response(exc)


# ---------------------------------------------------------------------------
# History / rollback / export / import / bot-rendered preview: parity with
# /template history|rollback|export|import|preview (manage_templates). Errors
# keep the bot's message (a list of strings for templates) as
# {"error": "validation_error", "message"} so describeApiError can show it.
# ---------------------------------------------------------------------------

def _template_error(exc: InternalAPIError) -> HTTPException:
    if exc.status_code in (400, 404, 422):
        detail = exc.detail
        if isinstance(detail, list) and detail:
            message = str(detail[0])
        elif isinstance(detail, str):
            message = detail
        else:
            message = None
        code = 404 if exc.status_code == 404 else 400
        error = "template_not_found" if code == 404 else "validation_error"
        body = {"error": error}
        if message:
            body["message"] = message
        return HTTPException(status_code=code, detail=body)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


@router.get("/templates/{template_id}/versions")
async def list_template_versions(
    template_id: int,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.list_template_versions(config.DASHBOARD_GUILD_ID, template_id)
    except InternalAPIError as exc:
        raise _template_error(exc)


class RollbackBody(BaseModel):
    version_number: int


@router.post("/templates/{template_id}/rollback")
async def rollback_template(
    template_id: int,
    body: RollbackBody,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.rollback_template(
            config.DASHBOARD_GUILD_ID, template_id, body.version_number, session.discord_user_id
        )
    except InternalAPIError as exc:
        raise _template_error(exc)


@router.get("/templates/{template_id}/export")
async def export_template(
    template_id: int,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.export_template(config.DASHBOARD_GUILD_ID, template_id)
    except InternalAPIError as exc:
        raise _template_error(exc)


class ImportTemplateBody(BaseModel):
    data: dict
    name_override: str | None = None


@router.post("/templates/import", status_code=status.HTTP_201_CREATED)
async def import_template(
    body: ImportTemplateBody,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.import_template(
            config.DASHBOARD_GUILD_ID, body.data, body.name_override, session.discord_user_id
        )
    except InternalAPIError as exc:
        raise _template_error(exc)


@router.get("/templates/{template_id}/preview")
async def preview_template(
    template_id: int,
    session: Session = Depends(require_permission("manage_templates")),
) -> dict:
    try:
        return await internal_client.preview_template(
            config.DASHBOARD_GUILD_ID, template_id, user_id=session.discord_user_id
        )
    except InternalAPIError as exc:
        raise _template_error(exc)
