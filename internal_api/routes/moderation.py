"""
GET    /guilds/{guild_id}/moderation/warnings/recent?limit=1..200
GET    /guilds/{guild_id}/moderation/warnings/{user_id}
POST   /guilds/{guild_id}/moderation/warnings                     {"user_id", "moderator_id", "reason"}
DELETE /guilds/{guild_id}/moderation/warnings/{warning_id}?moderator_id=&user_id=
GET    /guilds/{guild_id}/moderation/history/{user_id}
POST   /guilds/{guild_id}/moderation/kick/{user_id}               {"moderator_id", "reason"}
POST   /guilds/{guild_id}/moderation/ban                          {"user_id", "moderator_id", "reason", "delete_message_days"}
POST   /guilds/{guild_id}/moderation/unban                        {"user_id", "moderator_id", "reason"}
POST   /guilds/{guild_id}/moderation/timeout/{user_id}            {"moderator_id", "duration_minutes", "reason"}
POST   /guilds/{guild_id}/moderation/untimeout/{user_id}          {"moderator_id", "reason"}
GET    /guilds/{guild_id}/moderation/members/search?q=&limit=

Thin wrapper over services.warning_service.WarningService and
services.moderation_service.ModerationService, the SAME services the
`!warn`/`!warnings`/`!delwarn`/`!timeout`/`!untimeout`/`!ban`/`!unban`
(cogs/moderation.py) and `!kick` (cogs/kick.py) commands call, so
dashboard-issued and command-issued actions are stored identically (same
tables, same audit_log rows, same moderation-log / join-leave channel post).
Services are built per call from `bot.db`, like the cogs do.

Acting moderator: `moderator_id` comes from the dashboard backend, which fills
it from the SESSION (never from the browser). This route resolves it to a live
Member (cache, then fetch_member). A moderator who is not in the guild gets
403 actor_not_member, unless they are the owner (config.OWNER_ID or the guild
owner), who may act from outside the guild.

Rank checks (services/hierarchy via ModerationService.ensure_allowed / the
service's check_hierarchy=True): kick, ban, timeout, untimeout and warn are
refused with 403 {"error": "hierarchy_blocked", "message": <reason>} when the
moderator (or the bot) does not rank strictly above the target, or the target
is the moderator, the bot or the guild owner. The owner skips only the
moderator rank check. Ban-by-ID / warn of a user who is not in the guild only
gets the identity rules.

Error contract (every error detail is {"error": code, "message": text}):
  403 hierarchy_blocked / actor_not_member   rank or identity refusal
  404 member_not_found / warning_not_found / not_banned
  409 discord_forbidden                      Discord refused (bot lacks the permission / is outranked)
  400 discord_rejected                       other Discord 4xx
  503 discord_unavailable                    Discord 5xx
  422                                        pydantic bounds (reason <= 512 chars,
                                             timeout 1..40320 min, delete days 0..7,
                                             recent limit 1..200) or ModerationInputError
A Discord-side refusal is never a 502 here: the dashboard reserves 502 for
"the bot could not be reached".

Warnings GET and history do not need a live member (a user who left can still
be looked up); member search is cache-only (cheap enough per keystroke).
"""

from __future__ import annotations

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from database.repositories.mod_action_repo import ModActionRepository
from database.repositories.warning_repo import WarningRepository
from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from internal_api.snowflakes import stringify_snowflakes
from services.log_router import build_embed
from services.moderation_service import (
    MAX_DELETE_MESSAGE_DAYS,
    MAX_REASON_LENGTH,
    MAX_TIMEOUT_MINUTES,
    MOD_DELWARN_LOG_TEMPLATE_NAME,
    MOD_WARN_LOG_TEMPLATE_NAME,
    ModerationBlocked,
    ModerationInputError,
    ModerationService,
    actor_is_owner,
    member_block_reason,
)
from services.warning_service import WarningService

try:
    from database.repositories.audit_repo import AuditRepository
    from services.audit_service import AuditService
    _AUDIT_AVAILABLE = True
except ImportError:
    _AUDIT_AVAILABLE = False

try:
    from core.renderer import TemplateRenderer
    from database.repositories.embed_repo import EmbedRepository
    from database.repositories.template_repo import TemplateRepository
    from database.repositories.version_repo import VersionRepository
    from services.template_service import TemplateService
    _TEMPLATES_AVAILABLE = True
except ImportError:
    _TEMPLATES_AVAILABLE = False

import config as _config

router = APIRouter(dependencies=[Depends(require_internal_secret)])

_SNOWFLAKE = dict(ge=1, lt=2**63)
RECENT_WARNINGS_MAX = 200


def _err(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"error": error, "message": message})


def _db_or_503(bot: commands.Bot):
    db = getattr(bot, "db", None)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is not available on this bot instance.",
        )
    return db


def _audit(db):
    return AuditService(AuditRepository(db)) if _AUDIT_AVAILABLE else None


def _get_warning_service(bot: commands.Bot) -> WarningService:
    db = _db_or_503(bot)
    return WarningService(WarningRepository(db), _audit(db))


def _get_moderation_service(bot: commands.Bot) -> ModerationService:
    """Built the same way cogs/moderation.py and cogs/kick.py build theirs."""
    db = _db_or_503(bot)
    templates = None
    renderer = None
    if _TEMPLATES_AVAILABLE:
        templates = TemplateService(
            TemplateRepository(db), EmbedRepository(db), VersionRepository(db), AuditRepository(db),
        )
        renderer = TemplateRenderer()
    return ModerationService(ModActionRepository(db), _audit(db), templates, renderer)


def discord_error(exc: discord.HTTPException, action: str) -> HTTPException:
    """Map a discord.py error to a 4xx/503 with a readable message (never 502)."""
    if isinstance(exc, discord.Forbidden):
        return _err(
            status.HTTP_409_CONFLICT, "discord_forbidden",
            f"Discord refused to {action}: the bot is missing the permission or the member ranks above the bot.",
        )
    if isinstance(exc, discord.NotFound):
        return _err(status.HTTP_404_NOT_FOUND, "not_found", f"Discord could not find the user to {action}.")
    if getattr(exc, "status", 500) < 500:
        text = (getattr(exc, "text", "") or str(exc))[:300]
        return _err(status.HTTP_400_BAD_REQUEST, "discord_rejected", f"Discord rejected the request to {action}: {text}")
    return _err(
        status.HTTP_503_SERVICE_UNAVAILABLE, "discord_unavailable",
        "Discord returned a server error. Try again in a moment.",
    )


def _blocked(exc: ModerationBlocked) -> HTTPException:
    return _err(status.HTTP_403_FORBIDDEN, "hierarchy_blocked", exc.reason)


def _invalid(exc: ModerationInputError) -> HTTPException:
    return _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", str(exc))


async def _find_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    """Live member (cache, then fetch_member), or None if not in the guild."""
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None
    except discord.HTTPException as exc:
        raise discord_error(exc, "look up that member")


async def _require_target(guild: discord.Guild, user_id: int) -> discord.Member:
    member = await _find_member(guild, user_id)
    if member is None:
        raise _err(status.HTTP_404_NOT_FOUND, "member_not_found", "That member is not in the server.")
    return member


async def resolve_actor(guild: discord.Guild, moderator_id: int) -> discord.Member | discord.Object:
    """The acting moderator as a live Member. Only the owner may act without
    being a guild member (then a bare Object). Anyone else: 403."""
    member = await _find_member(guild, moderator_id)
    if member is not None:
        return member
    if actor_is_owner(guild, moderator_id):
        return discord.Object(id=moderator_id)
    raise _err(
        status.HTTP_403_FORBIDDEN, "actor_not_member",
        "You must be a member of the server to take moderation actions.",
    )


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

class AddWarningRequest(BaseModel):
    user_id: int = Field(**_SNOWFLAKE)
    moderator_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.get("/guilds/{guild_id}/moderation/warnings/recent")
async def get_recent_warnings(
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
    limit: int = Query(20, ge=1, le=RECENT_WARNINGS_MAX),
) -> dict:
    """Most recent warnings across the guild. MUST stay registered before
    GET .../warnings/{user_id} (an int path param would 422 on "recent")."""
    warning_service = _get_warning_service(bot)
    warnings = await warning_service.list_recent(guild.id, limit)
    return stringify_snowflakes({"warnings": warnings})


@router.get("/guilds/{guild_id}/moderation/warnings/{user_id}")
async def get_warnings(
    user_id: int,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    warning_service = _get_warning_service(bot)
    warnings = await warning_service.get_warnings(guild.id, user_id)
    return stringify_snowflakes({"user_id": user_id, "warnings": warnings})


@router.post("/guilds/{guild_id}/moderation/warnings", status_code=status.HTTP_201_CREATED)
async def add_warning(
    body: AddWarningRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    warning_service = _get_warning_service(bot)
    moderation_service = _get_moderation_service(bot)
    actor = await resolve_actor(guild, body.moderator_id)
    target = await _find_member(guild, body.user_id)
    blocked = member_block_reason(guild, actor, target if target is not None else discord.Object(id=body.user_id))
    if blocked:
        raise _err(status.HTTP_403_FORBIDDEN, "hierarchy_blocked", blocked)

    # add_warning also writes the member_warned audit row; the mod_actions
    # companion row and the moderation-log post match `!warn`.
    warning_id = await warning_service.add_warning(guild.id, body.user_id, body.moderator_id, body.reason)
    await moderation_service.record_warn_action(guild.id, body.user_id, body.moderator_id, body.reason, warning_id)
    fallback = build_embed(
        "member_warned", member=target, member_id=body.user_id, actor_id=body.moderator_id, reason=body.reason,
        extra_fields={"Warning ID": f"#{warning_id}"},
    )
    await moderation_service.send_mod_log(
        guild, MOD_WARN_LOG_TEMPLATE_NAME, _config.LOG_CHANNEL_ID, fallback,
        member=target, actor=actor if isinstance(actor, discord.Member) else None,
        extra={"action": "Warning", "reason": body.reason, "warning_id": warning_id, "target_id": body.user_id,
               "warning_count": await _warning_count(warning_service, guild.id, body.user_id),
               "source": "dashboard"},
    )
    return {"warning_id": warning_id}


async def _warning_count(warning_service, guild_id: int, user_id: int) -> int | None:
    """{warningcount} for the warn / warning-removed log; None if it can't be read."""
    try:
        return len(await warning_service.get_warnings(guild_id, user_id))
    except Exception:
        return None


@router.delete("/guilds/{guild_id}/moderation/warnings/{warning_id}")
async def remove_warning(
    warning_id: int,
    moderator_id: int = Query(..., ge=1, lt=2**63),
    user_id: int | None = Query(None, ge=1, lt=2**63),
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    """Remove a warning, recording WHO removed it (`moderator_id`, from the
    dashboard session) in the warning_removed audit row and mod_actions. With
    `user_id`, only a warning that belongs to that user is removed."""
    warning_service = _get_warning_service(bot)
    moderation_service = _get_moderation_service(bot)
    removed = await warning_service.remove_warning(guild.id, warning_id, user_id=user_id, actor_id=moderator_id)
    if not removed:
        raise _err(status.HTTP_404_NOT_FOUND, "warning_not_found", f"Warning #{warning_id} was not found.")
    owner_id = removed.get("user_id", user_id)
    if owner_id is not None:
        await moderation_service.record_warning_removed_action(guild.id, owner_id, moderator_id, warning_id)
        fallback = build_embed(
            "warning_removed", member_id=owner_id, actor_id=moderator_id,
            extra_fields={"Warning ID": f"#{warning_id}"},
        )
        await moderation_service.send_mod_log(
            guild, MOD_DELWARN_LOG_TEMPLATE_NAME, _config.LOG_CHANNEL_ID, fallback,
            member=guild.get_member(owner_id), actor=guild.get_member(moderator_id),
            extra={"action": "Warning removed", "warning_id": warning_id, "target_id": owner_id,
                   "warning_count": await _warning_count(warning_service, guild.id, owner_id),
                   "source": "dashboard"},
        )
    return {"removed": True}


# ---------------------------------------------------------------------------
# Unified mod-action history (mod_actions table).
# ---------------------------------------------------------------------------
@router.get("/guilds/{guild_id}/moderation/history/{user_id}")
async def get_history(
    user_id: int,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    moderation_service = _get_moderation_service(bot)
    history = await moderation_service.get_history(guild.id, user_id)
    return stringify_snowflakes({"user_id": user_id, "actions": history})


# ---------------------------------------------------------------------------
# Kick / ban / unban / timeout / untimeout
# ---------------------------------------------------------------------------

class KickRequest(BaseModel):
    moderator_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.post("/guilds/{guild_id}/moderation/kick/{user_id}", status_code=status.HTTP_201_CREATED)
async def kick_member(
    user_id: int,
    body: KickRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    moderation_service = _get_moderation_service(bot)
    actor = await resolve_actor(guild, body.moderator_id)
    member = await _require_target(guild, user_id)
    try:
        action_id = await moderation_service.kick(
            member, guild, actor, body.reason, log_channel_id=_config.JOIN_LEAVE_CHANNEL_ID,
            check_hierarchy=True, log_extra={"source": "dashboard"},
        )
    except ModerationBlocked as exc:
        raise _blocked(exc)
    except ModerationInputError as exc:
        raise _invalid(exc)
    except discord.HTTPException as exc:
        raise discord_error(exc, "kick that member")
    return {"action_id": action_id}


class BanRequest(BaseModel):
    user_id: int = Field(**_SNOWFLAKE)
    moderator_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    delete_message_days: int = Field(default=0, ge=0, le=MAX_DELETE_MESSAGE_DAYS)


@router.post("/guilds/{guild_id}/moderation/ban", status_code=status.HTTP_201_CREATED)
async def ban_user(
    body: BanRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    moderation_service = _get_moderation_service(bot)
    actor = await resolve_actor(guild, body.moderator_id)
    member_obj = await _find_member(guild, body.user_id)
    try:
        action_id = await moderation_service.ban(
            guild, body.user_id, actor, body.reason,
            member=member_obj, log_channel_id=_config.JOIN_LEAVE_CHANNEL_ID,
            delete_message_days=body.delete_message_days, check_hierarchy=True,
            log_extra={"source": "dashboard"},
        )
    except ModerationBlocked as exc:
        raise _blocked(exc)
    except ModerationInputError as exc:
        raise _invalid(exc)
    except discord.HTTPException as exc:
        raise discord_error(exc, "ban that user")
    return {"action_id": action_id}


class UnbanRequest(BaseModel):
    user_id: int = Field(**_SNOWFLAKE)
    moderator_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.post("/guilds/{guild_id}/moderation/unban", status_code=status.HTTP_201_CREATED)
async def unban_user(
    body: UnbanRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    moderation_service = _get_moderation_service(bot)
    actor = await resolve_actor(guild, body.moderator_id)
    try:
        action_id = await moderation_service.unban(
            guild, body.user_id, actor, body.reason, log_channel_id=_config.JOIN_LEAVE_CHANNEL_ID,
            log_extra={"source": "dashboard"},
        )
    except ModerationInputError as exc:
        raise _invalid(exc)
    except discord.NotFound:
        raise _err(status.HTTP_404_NOT_FOUND, "not_banned", "That user is not banned.")
    except discord.HTTPException as exc:
        raise discord_error(exc, "unban that user")
    return {"action_id": action_id}


class TimeoutRequest(BaseModel):
    moderator_id: int = Field(**_SNOWFLAKE)
    duration_minutes: int = Field(ge=1, le=MAX_TIMEOUT_MINUTES)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.post("/guilds/{guild_id}/moderation/timeout/{user_id}", status_code=status.HTTP_201_CREATED)
async def timeout_member(
    user_id: int,
    body: TimeoutRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    moderation_service = _get_moderation_service(bot)
    actor = await resolve_actor(guild, body.moderator_id)
    member = await _require_target(guild, user_id)
    try:
        action_id = await moderation_service.timeout(
            member, guild, actor, body.duration_minutes, body.reason,
            log_channel_id=_config.LOG_CHANNEL_ID, check_hierarchy=True, log_extra={"source": "dashboard"},
        )
    except ModerationBlocked as exc:
        raise _blocked(exc)
    except ModerationInputError as exc:
        raise _invalid(exc)
    except discord.HTTPException as exc:
        raise discord_error(exc, "time out that member")
    return {"action_id": action_id}


class UntimeoutRequest(BaseModel):
    moderator_id: int = Field(**_SNOWFLAKE)
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)


@router.post("/guilds/{guild_id}/moderation/untimeout/{user_id}", status_code=status.HTTP_201_CREATED)
async def untimeout_member(
    user_id: int,
    body: UntimeoutRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    moderation_service = _get_moderation_service(bot)
    actor = await resolve_actor(guild, body.moderator_id)
    member = await _require_target(guild, user_id)
    try:
        action_id = await moderation_service.untimeout(
            member, guild, actor, body.reason, log_channel_id=_config.LOG_CHANNEL_ID,
            check_hierarchy=True, log_extra={"source": "dashboard"},
        )
    except ModerationBlocked as exc:
        raise _blocked(exc)
    except ModerationInputError as exc:
        raise _invalid(exc)
    except discord.HTTPException as exc:
        raise discord_error(exc, "remove that member's timeout")
    return {"action_id": action_id}


# ---------------------------------------------------------------------------
# Live-cache member search (username/display name typeahead).
# ---------------------------------------------------------------------------
@router.get("/guilds/{guild_id}/moderation/members/search")
async def search_members(
    q: str = Query("", max_length=100),
    limit: int = 20,
    guild: discord.Guild = Depends(get_guild),
) -> dict:
    query = q.strip().lower()
    if not query:
        return {"members": []}

    capped_limit = max(1, min(limit, 25))
    results = []
    for member in guild.members:
        if query in member.name.lower() or query in member.display_name.lower():
            results.append(
                {
                    "id": str(member.id),
                    "username": member.name,
                    "display_name": member.display_name,
                    "avatar_url": str(member.display_avatar.url),
                }
            )
            if len(results) >= capped_limit:
                break
    return {"members": results}
