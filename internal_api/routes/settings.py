"""
GET /guilds/{guild_id}/settings   -> settings report (see SettingsService.get_report)
PUT /guilds/{guild_id}/settings   {updated_by, values: {key: value|null}} -> report + {"changed": [...]}

Thin wrapper over services.settings_service.SettingsService, which owns the
field allowlist (SETTINGS_SCHEMA), validation and the audit row. See that
module's docstring for how saved values take effect (live vs next restart).

Errors (the dashboard passes them through, never as 502):
  422 {"error": "validation_failed", "message", "fields": {key: message}}
      unknown key, read-only key (owner_id), bad type/range, an ID that is not
      in the guild or is the wrong kind, a role the bot can't assign, a
      template that doesn't exist. All-or-nothing: nothing is written.
  503 {"error": "guild_unavailable", "message"}  an ID needs checking but the
      guild is not cached yet (bot still starting).

`updated_by` is the dashboard session user (server/routes/settings.py sets
it); this process has no user auth. Staff gating happens in the dashboard
(require_staff).
"""

from __future__ import annotations

from typing import Any

from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from database.repositories.audit_repo import AuditRepository
from database.repositories.settings_repo import SettingsRepository
from database.repositories.template_repo import TemplateRepository
from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot
from internal_api.snowflakes import stringify_snowflakes
from services.audit_service import AuditService
from services.settings_service import GuildUnavailableError, SettingsService, SettingsValidationError

router = APIRouter(dependencies=[Depends(require_internal_secret)])


def _get_settings_service(bot: commands.Bot) -> SettingsService:
    db = getattr(bot, "db", None)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "database_unavailable", "message": "The bot's database is not available."},
        )
    return SettingsService(SettingsRepository(db), TemplateRepository(db), AuditService(AuditRepository(db)))


@router.get("/guilds/{guild_id}/settings")
async def get_settings(guild_id: int, bot: commands.Bot = Depends(get_bot)) -> dict:
    service = _get_settings_service(bot)
    report = await service.get_report(guild_id, bot.get_guild(guild_id))
    return stringify_snowflakes(report)


class UpdateSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_by: int
    values: dict[str, Any] = {}


@router.put("/guilds/{guild_id}/settings")
async def update_settings(
    guild_id: int,
    body: UpdateSettingsRequest,
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    service = _get_settings_service(bot)
    guild = bot.get_guild(guild_id)
    try:
        changed = await service.update(guild_id, body.values, body.updated_by, guild)
    except SettingsValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_failed", "message": "Some settings are invalid.", "fields": exc.fields},
        )
    except GuildUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "guild_unavailable",
                    "message": "The bot hasn't loaded the server yet. Try again in a moment."},
        )
    report = await service.get_report(guild_id, guild)
    report["changed"] = changed
    return stringify_snowflakes(report)
