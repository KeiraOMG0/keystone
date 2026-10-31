"""
Member hub (/hub) for the dashboard backend.

GET  /guilds/{guild_id}/hub/{user_id}                                  -> the hub view
POST /guilds/{guild_id}/hub/{user_id}/self-roles {"panel_id", "role_id", "assign"}
                                                                       -> {"result", "role_id", "removed_role_ids"}

`user_id` is ALWAYS the dashboard session user (server/routes/hub.py fills it
from the session, never from the request), so the view is the person's own
data; see services/member_hub_service.py for what is and isn't included.

Self-role toggles follow the panel buttons' rules (member_hub_service.member_toggle);
refusals are {"error", "message"} details: 404 panel_not_found / role_not_on_panel /
role_not_found / not_a_member, 403 role_blocked, 409 discord_forbidden, 502 discord_error.
"""

from __future__ import annotations

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from internal_api.routes.moderation import _find_member, _get_warning_service
from internal_api.snowflakes import stringify_snowflakes
from services.member_hub_service import HubError, MemberHubService, member_toggle

router = APIRouter(dependencies=[Depends(require_internal_secret)])

_SNOWFLAKE = dict(ge=1, lt=2**63)


def _err(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"error": error, "message": message})


def _cog_attr(bot: commands.Bot, cog_name: str, attr: str):
    cog = bot.get_cog(cog_name)
    return getattr(cog, attr, None) if cog is not None else None


def build_service(bot: commands.Bot) -> MemberHubService:
    """Every dependency is optional: a feature whose cog isn't loaded shows up in
    the response's `unavailable` list instead of failing the whole hub."""
    db = getattr(bot, "db", None)
    tickets_repo = qotd_repo = warnings = None
    if db is not None:
        from database.repositories.qotd_repo import QotdRepository
        from database.repositories.ticket_repo import TicketRepository

        tickets_repo, qotd_repo = TicketRepository(db), QotdRepository(db)
        warnings = _get_warning_service(bot)
    core = getattr(bot, "birthday_core", None)
    return MemberHubService(
        warnings=warnings,
        reaction_roles=_cog_attr(bot, "ReactionRoles", "reaction_role_service"),
        birthdays=getattr(core, "svc", None),
        honeypot=_cog_attr(bot, "Honeypot", "honeypot_service"),
        tickets_repo=tickets_repo,
        qotd_repo=qotd_repo,
        music=bot.get_cog("Music"),
        presences=bool(getattr(getattr(bot, "intents", None), "presences", False)),
    )


@router.get("/guilds/{guild_id}/hub/{user_id}")
async def hub_view(
    user_id: int,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    member = await _find_member(guild, user_id)
    user = member
    if user is None:
        user = bot.get_user(user_id)
        if user is None:
            try:
                user = await bot.fetch_user(user_id)
            except discord.HTTPException:
                user = discord.Object(id=user_id)
    return stringify_snowflakes(await build_service(bot).build(guild, user, member))


class ToggleBody(BaseModel):
    panel_id: int = Field(ge=1)
    role_id: int = Field(**_SNOWFLAKE)
    assign: bool


@router.post("/guilds/{guild_id}/hub/{user_id}/self-roles")
async def toggle_self_role(
    user_id: int,
    body: ToggleBody,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    member = await _find_member(guild, user_id)
    if member is None:
        raise _err(404, "not_a_member", "You're not in the server.")
    rr = _cog_attr(bot, "ReactionRoles", "reaction_role_service")
    if rr is None:
        raise _err(503, "unavailable", "Self-roles are not available right now.")
    try:
        return await member_toggle(rr, guild, member, body.panel_id, body.role_id, body.assign)
    except HubError as exc:
        raise _err(exc.status, exc.code, exc.message)
