"""
GET    /guilds/{guild_id}/templates
GET    /guilds/{guild_id}/templates/{template_id}
POST   /guilds/{guild_id}/templates
PATCH  /guilds/{guild_id}/templates/{template_id}
PUT    /guilds/{guild_id}/templates/{template_id}/embeds
DELETE /guilds/{guild_id}/templates/{template_id}

Thin wrapper over services.template_service.TemplateService — the SAME
service cogs/tickets.py constructs on-demand (see its `template_service`
local var built from TemplateRepository/EmbedRepository/VersionRepository/
AuditRepository, all bound to bot.db) for template-editing slash commands,
so dashboard-issued edits and command-issued edits are indistinguishable in
storage, snapshot history, and audit log.

TemplateService itself only exposes get_full(guild_id, name) /
delete(guild_id, name) (name-keyed, matching the slash-command UX where
staff type a template name). The dashboard list/detail/edit views work off
numeric template_id instead (from list_templates), so every route below
that needs an existing template resolves guild_id -> row -> row["name"]
via TemplateRepository.get_by_id first, then calls the name-keyed service
method. This is a deliberate adapter, not a service change, since the
slash-command surface still relies on name-keyed lookups.

PUT .../embeds accepts the FULL embeds+fields array and replaces the
template's entire embed set in one call (TemplateService.replace_embeds) —
matches how the dashboard's embed editor holds the whole array in local
React state and saves once, rather than diffing individual embed/field
edits against the server.

No live-Discord side effects here — templates are just data. The ticket
panel builder (ticket_panels.py) is what actually posts to Discord using a
template.
"""

from __future__ import annotations

import discord
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from database.repositories.audit_repo import AuditRepository
from database.repositories.embed_repo import EmbedRepository
from database.repositories.managed_repo import ManagedMessageRepository
from database.repositories.template_repo import TemplateRepository
from database.repositories.version_repo import VersionRepository
from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from internal_api.snowflakes import stringify_snowflakes
from core.renderer import TemplateRenderer
from services.managed_message_service import ManagedMessageService
from services.template_service import TemplateService

router = APIRouter(dependencies=[Depends(require_internal_secret)])


def get_template_service(bot=Depends(get_bot)) -> TemplateService:
    """Constructed fresh per-request from bot.db, exactly like
    cogs/tickets.py does inline for its template-editing slash commands —
    TemplateService is stateless aside from its repo handles, so there's no
    reason to cache an instance on the bot/cog."""
    if not hasattr(bot, "db") or bot.db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not available yet.",
        )
    return TemplateService(
        TemplateRepository(bot.db),
        EmbedRepository(bot.db),
        VersionRepository(bot.db),
        AuditRepository(bot.db),
    )


def get_managed_message_service(bot=Depends(get_bot)) -> ManagedMessageService:
    """Constructed fresh per-request, same stateless-per-request pattern as
    get_template_service above — mirrors how cogs/template_commands.py's
    `/template post` slash command builds it (see that cog's setup())."""
    if not hasattr(bot, "db") or bot.db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not available yet.",
        )
    return ManagedMessageService(
        ManagedMessageRepository(bot.db),
        TemplateRepository(bot.db),
        TemplateRenderer(),
        bot,
    )


def _raise_for_errors(errors: list[str]) -> None:
    if not errors:
        return
    if any("not found" in e.lower() for e in errors):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=errors)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=errors)


async def _get_template_row_or_404(template_id: int, guild: discord.Guild, template_repo: TemplateRepository) -> dict:
    row = await template_repo.get_by_id(template_id)
    if not row or row["guild_id"] != guild.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    return row


async def _enqueue_sync(bot, template_id: int) -> None:
    """Re-render every managed message of this template (same debounced
    bot.sync_queue the Discord embed editor and /template rollback use), so a
    dashboard save really does update messages posted with "Send to channel"."""
    queue = getattr(bot, "sync_queue", None)
    if queue is not None:
        try:
            await queue.enqueue(template_id)
        except Exception:  # never fail the save over the sync
            pass


@router.get("/guilds/{guild_id}/templates")
async def list_templates(
    category: str | None = None,
    guild: discord.Guild = Depends(get_guild),
    template_service: TemplateService = Depends(get_template_service),
) -> list[dict]:
    return stringify_snowflakes(await template_service.list_templates(guild.id, category))


@router.get("/guilds/{guild_id}/templates/{template_id}")
async def get_template(
    template_id: int,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    template_service: TemplateService = Depends(get_template_service),
) -> dict:
    template_repo = TemplateRepository(bot.db)
    row = await _get_template_row_or_404(template_id, guild, template_repo)
    full = await template_repo.get_full(row["id"])
    return stringify_snowflakes(full)


class CreateTemplateRequest(BaseModel):
    name: str
    created_by: int
    description: str | None = None
    category: str | None = None
    content: str | None = None


@router.post("/guilds/{guild_id}/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    body: CreateTemplateRequest,
    guild: discord.Guild = Depends(get_guild),
    template_service: TemplateService = Depends(get_template_service),
) -> dict:
    template_id, errors = await template_service.create(
        guild.id, body.name, body.created_by,
        description=body.description, category=body.category, content=body.content,
    )
    _raise_for_errors(errors)
    return {"template_id": template_id}


class UpdateTemplateRequest(BaseModel):
    actor_id: int
    name: str | None = None
    description: str | None = None
    category: str | None = None
    content: str | None = None


@router.patch("/guilds/{guild_id}/templates/{template_id}")
async def update_template(
    template_id: int,
    body: UpdateTemplateRequest,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    template_service: TemplateService = Depends(get_template_service),
) -> dict:
    template_repo = TemplateRepository(bot.db)
    row = await _get_template_row_or_404(template_id, guild, template_repo)

    update_kwargs = body.model_dump(exclude={"actor_id"}, exclude_none=True)
    errors = await template_service.update_fields(guild.id, row["name"], body.actor_id, **update_kwargs)
    _raise_for_errors(errors)
    await _enqueue_sync(bot, row["id"])
    return {"success": True}


class PostTemplateRequest(BaseModel):
    channel_id: int
    actor_id: int


@router.post("/guilds/{guild_id}/templates/{template_id}/post", status_code=status.HTTP_201_CREATED)
async def post_template(
    template_id: int,
    body: PostTemplateRequest,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    managed_service: ManagedMessageService = Depends(get_managed_message_service),
) -> dict:
    """Renders a template and sends it as a live message in `channel_id`,
    tracked as a managed message (auto-updates on future template edits) —
    the same ManagedMessageService.post() call cogs/template_commands.py's
    `/template post` slash command makes, so a dashboard-sent template and
    a command-sent one are indistinguishable in storage/auto-sync."""
    template_repo = TemplateRepository(bot.db)
    row = await _get_template_row_or_404(template_id, guild, template_repo)

    channel = guild.get_channel(body.channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="channel_id must be a text channel in this guild.",
        )

    message_id, errors = await managed_service.post(
        guild=guild,
        channel=channel,
        template_name=row["name"],
        actor_id=body.actor_id,
    )
    _raise_for_errors(errors)
    return stringify_snowflakes({"message_id": message_id, "channel_id": body.channel_id})


class EmbedFieldPayload(BaseModel):
    name: str
    value: str
    inline: bool = False
    position: int | None = None


class EmbedPayload(BaseModel):
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
    fields: list[EmbedFieldPayload] = []


class ReplaceEmbedsRequest(BaseModel):
    actor_id: int
    embeds: list[EmbedPayload]


@router.put("/guilds/{guild_id}/templates/{template_id}/embeds")
async def replace_embeds(
    template_id: int,
    body: ReplaceEmbedsRequest,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    template_service: TemplateService = Depends(get_template_service),
) -> dict:
    template_repo = TemplateRepository(bot.db)
    row = await _get_template_row_or_404(template_id, guild, template_repo)

    embeds_data = [e.model_dump(exclude_none=False) for e in body.embeds]
    errors = await template_service.replace_embeds(guild.id, row["name"], body.actor_id, embeds_data)
    _raise_for_errors(errors)
    await _enqueue_sync(bot, row["id"])

    full = await template_repo.get_full(row["id"])
    return stringify_snowflakes(full)


@router.delete("/guilds/{guild_id}/templates/{template_id}")
async def delete_template(
    template_id: int,
    actor_id: int,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    template_service: TemplateService = Depends(get_template_service),
) -> dict:
    template_repo = TemplateRepository(bot.db)
    row = await _get_template_row_or_404(template_id, guild, template_repo)
    deleted = await template_service.delete(guild.id, row["name"], actor_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    return {"success": True}


# ---------------------------------------------------------------------------
# History / rollback / export / import / bot-side preview (parity with
# /template history|rollback|export|import|preview). Import is registered with
# a literal path segment, so it never collides with /templates/{template_id}.
# ---------------------------------------------------------------------------

@router.get("/guilds/{guild_id}/templates/{template_id}/versions")
async def list_versions(
    template_id: int,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    template_service: TemplateService = Depends(get_template_service),
) -> dict:
    row = await _get_template_row_or_404(template_id, guild, TemplateRepository(bot.db))
    versions = await template_service.get_version_history(guild.id, row["name"])
    return stringify_snowflakes({
        "versions": [
            {
                "version_number": v["version_number"],
                "changed_by": v.get("changed_by"),
                "change_summary": v.get("change_summary"),
                "created_at": v.get("created_at"),
            }
            for v in versions
        ]
    })


class RollbackRequest(BaseModel):
    version_number: int
    actor_id: int


@router.post("/guilds/{guild_id}/templates/{template_id}/rollback")
async def rollback_template(
    template_id: int,
    body: RollbackRequest,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    template_service: TemplateService = Depends(get_template_service),
) -> dict:
    row = await _get_template_row_or_404(template_id, guild, TemplateRepository(bot.db))
    errors = await template_service.rollback(guild.id, row["name"], body.version_number, body.actor_id)
    _raise_for_errors(errors)
    await _enqueue_sync(bot, row["id"])  # same as /template rollback
    return {"success": True}


@router.get("/guilds/{guild_id}/templates/{template_id}/export")
async def export_template(
    template_id: int,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    template_service: TemplateService = Depends(get_template_service),
) -> dict:
    row = await _get_template_row_or_404(template_id, guild, TemplateRepository(bot.db))
    export = await template_service.export_template(guild.id, row["name"])
    if export is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found.")
    return export


class ImportTemplateRequest(BaseModel):
    data: dict
    name_override: str | None = None
    actor_id: int


@router.post("/guilds/{guild_id}/templates/import")
async def import_template(
    body: ImportTemplateRequest,
    guild: discord.Guild = Depends(get_guild),
    template_service: TemplateService = Depends(get_template_service),
) -> dict:
    template_id, errors, name = await template_service.import_template(
        guild.id, body.data, body.actor_id, name_override=(body.name_override or "").strip() or None,
    )
    if errors:
        # Validation only (bad/duplicate name, malformed JSON): never a 404.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=errors)
    return {"template_id": template_id, "name": name}


@router.get("/guilds/{guild_id}/placeholders")
async def list_placeholders(
    user_id: int | None = None,
    guild: discord.Guild = Depends(get_guild),
) -> dict:
    """Every placeholder (core/placeholder_engine.catalogue) with an example
    value rendered by the real functions against sample_context(): the viewer
    (`user_id`, if a member) as member and actor, example moderation data."""
    from core.placeholder_engine import CATEGORIES, catalogue, resolve_text, sample_context

    member = guild.get_member(user_id) if user_id else None
    ctx = sample_context(guild, member)
    items = []
    for entry in catalogue():
        example = resolve_text(entry["syntax"], ctx)
        items.append({**entry, "example": example if len(example) <= 300 else example[:299] + "\u2026"})
    return stringify_snowflakes({"categories": list(CATEGORIES), "placeholders": items, "sample_member": member is not None})


@router.get("/guilds/{guild_id}/templates/{template_id}/preview")
async def preview_template(
    template_id: int,
    user_id: int | None = None,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
) -> dict:
    """Rendered by the bot like /template preview: with `user_id` (a member)
    every placeholder is filled from sample_context() (that member as member
    and actor, example moderation data); without it member placeholders are
    blank. Returns Discord embed dicts."""
    from core.placeholder_engine import sample_context

    template_repo = TemplateRepository(bot.db)
    row = await _get_template_row_or_404(template_id, guild, template_repo)
    full = await template_repo.get_full(row["id"])
    member = guild.get_member(user_id) if user_id else None
    try:
        rendered = TemplateRenderer().render_message(full, sample_context(guild, member))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=[f"Render error: {exc}"])
    return {
        "content": rendered.get("content"),
        "embeds": [e.to_dict() for e in (rendered.get("embeds") or [])[:10]],
    }
