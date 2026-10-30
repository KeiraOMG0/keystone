"""
Member profile, `!verify`, `!purge`, `!msglog` and `/serverinfo` for the dashboard.

GET  /guilds/{guild_id}/members/{user_id}/profile?actor_id=&include_honeypot=
GET  /guilds/{guild_id}/members/verify-channels
POST /guilds/{guild_id}/members/{user_id}/verify          {"actor_id", "channel_id"}
GET  /guilds/{guild_id}/moderation/purge-channels?actor_id=
POST /guilds/{guild_id}/moderation/purge                  {"moderator_id", "channel_id", "amount", "user_id", "confirm"}
GET  /guilds/{guild_id}/moderation/purge/{job_id}
GET  /guilds/{guild_id}/message-log/status
POST /guilds/{guild_id}/message-log/export                {"actor_id", "kind", "user_id", "channel_id", "window"}
GET  /guilds/{guild_id}/server-info

Thin wrappers over the services the Discord commands use:
  - profile: services/member_profile_service.build_profile (WarningService,
    ModerationService history, HoneypotRepository).
  - verify: services/verify_service.verify_member (same as `!verify` and the
    ticket "Verify & Close" button). Like `!verify`, which only runs inside the
    Unverified category, the verify message goes to a text channel of that
    category (the caller picks one from /members/verify-channels).
  - purge: services/purge_service (same bounds/parsing/purge as `!purge`).
    Runs as a background job (PurgeJobs) answered at once with the job; the
    dashboard polls GET .../purge/{job_id} (a big purge can outlast
    Cloudflare's 100 s limit). Window purges and counts over 100 need
    `confirm: true` (409 confirm_required otherwise), mirroring the cog's
    Confirm button. The moderator must be able to see the channel (view +
    read history): the command can only be typed where you can see it.
  - message log: services/message_log_service (build_export /
    send_export_dm, same as `!msglog`). Staff only (owner or STAFF_ROLE_IDS,
    re-checked here on the live member), DM only, never returned over HTTP:
    the export contains other members' messages.

The acting user (`actor_id` / `moderator_id`) is filled by the dashboard
backend from the SESSION. Errors are {"error", "message"} details (same codes
as internal_api/routes/moderation.py) so server/routes/*.py pass them through;
a Discord refusal is never a 502.

Audit rows: member_verified (detail.source = "dashboard"), messages_purged
(detail.source = "dashboard"), message_log_exported (source "dashboard").
"""

from __future__ import annotations

from typing import Literal

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field

import config as _config
from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from internal_api.routes.moderation import (
    _db_or_503,
    _err,
    _find_member,
    _get_moderation_service,
    _get_warning_service,
    discord_error,
    resolve_actor,
)
from internal_api.snowflakes import stringify_snowflakes
from services import purge_service
from services.member_profile_service import build_profile
from services.moderation_service import actor_is_owner

router = APIRouter(dependencies=[Depends(require_internal_secret)])

_SNOWFLAKE = dict(ge=1, lt=2**63)


def _audit(db):
    from database.repositories.audit_repo import AuditRepository
    from services.audit_service import AuditService

    return AuditService(AuditRepository(db))


async def _quiet_actor(guild: discord.Guild, actor_id: int | None):
    """The viewer as a Member (or an Object for the owner outside the guild);
    None when unknown. Never raises: the profile is still shown."""
    if actor_id is None:
        return None
    try:
        member = await _find_member(guild, actor_id)
    except Exception:
        return None
    if member is not None:
        return member
    return discord.Object(id=actor_id) if actor_is_owner(guild, actor_id) else None


# ---------------------------------------------------------------------------
# Member profile
# ---------------------------------------------------------------------------

@router.get("/guilds/{guild_id}/members/verify-channels")
async def get_verify_channels(guild: discord.Guild = Depends(get_guild)) -> dict:
    """Text channels of the Unverified category (where `!verify` works)."""
    from services.verify_service import verify_channels

    return {
        "category_id": str(_config.UNVERIFIED_CATEGORY_ID) if _config.UNVERIFIED_CATEGORY_ID else None,
        "channels": [{"id": str(c.id), "name": c.name} for c in verify_channels(guild)],
    }


@router.get("/guilds/{guild_id}/members/{user_id}/profile")
async def get_profile(
    user_id: int,
    actor_id: int | None = Query(None, **_SNOWFLAKE),
    include_honeypot: bool = False,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    if user_id < 1 or user_id >= 2**63:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", "That is not a Discord user ID.")
    honeypot_repo = None
    if include_honeypot:
        from database.repositories.honeypot_repo import HoneypotRepository

        honeypot_repo = HoneypotRepository(_db_or_503(bot))
    actor = await _quiet_actor(guild, actor_id)
    try:
        profile = await build_profile(
            bot, guild, user_id,
            warning_service=_get_warning_service(bot),
            moderation_service=_get_moderation_service(bot),
            honeypot_repo=honeypot_repo, include_honeypot=include_honeypot, actor=actor,
        )
    except discord.HTTPException as exc:
        raise discord_error(exc, "look up that member")
    return stringify_snowflakes(profile)


class VerifyRequest(BaseModel):
    actor_id: int = Field(**_SNOWFLAKE)
    channel_id: int = Field(**_SNOWFLAKE)


@router.post("/guilds/{guild_id}/members/{user_id}/verify")
async def verify(
    user_id: int,
    body: VerifyRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    from core.renderer import TemplateRenderer
    from database.repositories.audit_repo import AuditRepository
    from database.repositories.embed_repo import EmbedRepository
    from database.repositories.template_repo import TemplateRepository
    from database.repositories.version_repo import VersionRepository
    from services.template_service import TemplateService
    from services.verify_service import verify_channels, verify_member

    db = _db_or_503(bot)
    actor = await resolve_actor(guild, body.actor_id)
    member = await _find_member(guild, user_id)
    if member is None:
        raise _err(status.HTTP_404_NOT_FOUND, "member_not_found", "That member is not in the server.")
    channel = next((c for c in verify_channels(guild) if c.id == body.channel_id), None)
    if channel is None:
        raise _err(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_channel",
            "Pick a text channel in the Unverified category (that is where !verify works).",
        )
    templates = TemplateService(TemplateRepository(db), EmbedRepository(db), VersionRepository(db), AuditRepository(db))
    result = await verify_member(member, actor, channel, templates, TemplateRenderer(), _audit(db), source="dashboard")
    if not result.ok:
        if result.already_verified:
            raise _err(status.HTTP_409_CONFLICT, "already_verified", "That member is already verified.")
        raise _err(status.HTTP_409_CONFLICT, "verify_failed", result.error or "Verification failed.")
    return {"ok": True, "message_status": result.message_status}


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

def _actor_can_see(channel, actor, guild) -> bool:
    if not isinstance(actor, discord.Member) or actor_is_owner(guild, actor.id):
        return True
    perms = channel.permissions_for(actor)
    return bool(perms.view_channel and perms.read_message_history)


def _bot_can_purge(channel, guild) -> bool:
    me = getattr(guild, "me", None)
    if me is None:
        return False
    perms = channel.permissions_for(me)
    return bool(perms.view_channel and perms.read_message_history and perms.manage_messages)


@router.get("/guilds/{guild_id}/moderation/purge-channels")
async def get_purge_channels(
    actor_id: int = Query(..., **_SNOWFLAKE),
    guild: discord.Guild = Depends(get_guild),
) -> dict:
    """Text channels the moderator can see; `bot_can_purge` false where the
    bot lacks Manage Messages / Read Message History."""
    actor = await resolve_actor(guild, actor_id)
    out = []
    for ch in sorted(guild.text_channels, key=lambda c: (getattr(c.category, "position", -1), c.position)):
        if not _actor_can_see(ch, actor, guild):
            continue
        out.append({
            "id": str(ch.id), "name": ch.name,
            "category": ch.category.name if ch.category else None,
            "bot_can_purge": _bot_can_purge(ch, guild),
        })
    return {"channels": out}


class PurgeRequest(BaseModel):
    moderator_id: int = Field(**_SNOWFLAKE)
    channel_id: int = Field(**_SNOWFLAKE)
    amount: str = Field(min_length=1, max_length=16)
    user_id: int | None = Field(default=None, **_SNOWFLAKE)
    confirm: bool = False


@router.post("/guilds/{guild_id}/moderation/purge")
async def start_purge(
    body: PurgeRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    count, window, error = purge_service.parse_purge_amount(body.amount)
    if error:
        message = error if "Usage" not in error else (
            "Enter a count from 1 to 1000, or a window like 30s, 15m, 2h or 1d (up to 14 days)."
        )
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", message)
    if purge_service.needs_confirm(count, window) and not body.confirm:
        raise _err(
            status.HTTP_409_CONFLICT, "confirm_required",
            "Purges of more than 100 messages and time-window purges must be confirmed.",
        )
    actor = await resolve_actor(guild, body.moderator_id)
    channel = guild.get_channel(body.channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise _err(status.HTTP_404_NOT_FOUND, "channel_not_found", "That channel no longer exists or is not a text channel.")
    if not _actor_can_see(channel, actor, guild):
        raise _err(status.HTTP_403_FORBIDDEN, "channel_forbidden", "You can only purge channels you can see.")
    if not _bot_can_purge(channel, guild):
        raise _err(
            status.HTTP_409_CONFLICT, "discord_forbidden",
            f"The bot needs Manage Messages and Read Message History in #{channel.name}.",
        )

    db = _db_or_503(bot)
    moderation = _get_moderation_service(bot)
    audit = _audit(db)
    user_id = body.user_id
    actor_name = str(actor) if isinstance(actor, discord.Member) else str(actor.id)

    async def run() -> int:
        deleted = await purge_service.purge_channel(
            channel, count=count, window=window, user_id=user_id,
            reason=f"Dashboard purge by {actor_name} ({actor.id})",
        )
        n = len(deleted)
        await audit.log(
            guild.id, actor.id, "messages_purged",
            target_type="channel", target_id=channel.id,
            detail=purge_service.purge_audit_detail(n, user_id, window, source="dashboard"),
        )
        await moderation.send_mod_log(
            guild, purge_service.MOD_PURGE_LOG_TEMPLATE_NAME, _config.LOG_CHANNEL_ID,
            purge_service.purge_log_embed(channel, actor.id, n, user_id, window),
            member=guild.get_member(user_id) if user_id else None,
            actor=actor if isinstance(actor, discord.Member) else None,
            extra={"action": "Purge", "purge_count": n, "purge_channel": channel, "target_id": user_id,
                   "purge_filter": purge_service.describe_filter(count, window, user_id), "source": "dashboard"},
        )
        return n

    job = purge_service.JOBS.start(
        guild.id, channel.id, run,
        summary={"amount": body.amount.strip(), "user_id": user_id},
    )
    if job is None:
        raise _err(status.HTTP_409_CONFLICT, "purge_running", "A purge is already running in that channel.")
    return stringify_snowflakes(job)


@router.get("/guilds/{guild_id}/moderation/purge/{job_id}")
async def get_purge_job(job_id: str, guild: discord.Guild = Depends(get_guild)) -> dict:
    job = purge_service.JOBS.get(guild.id, job_id)
    if job is None:
        raise _err(
            status.HTTP_404_NOT_FOUND, "job_not_found",
            "That purge is no longer tracked (the bot may have restarted). Check the channel and the audit log.",
        )
    return stringify_snowflakes(job)


# ---------------------------------------------------------------------------
# Message log (`!msglog`, staff only, DM only)
# ---------------------------------------------------------------------------

def _message_log_cog(bot):
    cog = bot.get_cog("MessageLog")
    if cog is None or getattr(cog, "store", None) is None:
        raise _err(
            status.HTTP_503_SERVICE_UNAVAILABLE, "message_log_unavailable",
            "The message log is turned off (MESSAGE_LOG_ENABLED=false) or not loaded.",
        )
    return cog


@router.get("/guilds/{guild_id}/message-log/status")
async def message_log_status(
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    import os

    cog = bot.get_cog("MessageLog")
    if cog is None or getattr(cog, "store", None) is None:
        return {"enabled": False, "retention_days": _config.MESSAGE_LOG_RETENTION_DAYS}
    stats = await cog.store.stats()
    path = cog.store.path
    return {
        "enabled": True,
        "rows": stats["rows"], "deleted": stats["deleted"], "oldest": stats["oldest"],
        "retention_days": cog.store.retention_days,
        "size_kb": os.path.getsize(path) // 1024 if os.path.exists(path) else 0,
    }


class ExportRequest(BaseModel):
    actor_id: int = Field(**_SNOWFLAKE)
    kind: Literal["user", "channel", "deleted"]
    user_id: int | None = Field(default=None, **_SNOWFLAKE)
    channel_id: int | None = Field(default=None, **_SNOWFLAKE)
    window: str | None = Field(default=None, max_length=8)


@router.post("/guilds/{guild_id}/message-log/export")
async def message_log_export(
    body: ExportRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    from services.message_log_service import build_export, parse_window, send_export_dm

    cog = _message_log_cog(bot)
    actor = await resolve_actor(guild, body.actor_id)
    if isinstance(actor, discord.Member):
        allowed = bot.permission_service.is_staff(actor)  # type: ignore[attr-defined]
    else:
        allowed = actor_is_owner(guild, actor.id)
    if not allowed:
        raise _err(status.HTTP_403_FORBIDDEN, "not_staff", "The message log export is for staff only.")

    window = parse_window(body.window, cog.store.retention_days)
    if window is None:
        raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", "The window must look like 30m, 6h or 2d.")
    user_id = channel_id = None
    deleted_only = False
    if body.kind == "user":
        if body.user_id is None:
            raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", "Pick a user.")
        user_id, label = body.user_id, f"<@{body.user_id}>"
    elif body.kind == "channel":
        if body.channel_id is None:
            raise _err(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_input", "Pick a channel.")
        channel_id, label = body.channel_id, f"<#{body.channel_id}>"
    else:
        deleted_only, label = True, "deleted messages"

    window_label = (body.window or "24h").strip()
    rows, data, filename = await build_export(
        cog.store, guild.id, window=window, user_id=user_id, channel_id=channel_id,
        deleted_only=deleted_only, label=label, window_label=window_label,
        exported_by=f"{actor} ({actor.id}) via the dashboard",
    )
    if not rows:
        raise _err(status.HTTP_404_NOT_FOUND, "no_messages", "No stored messages match that in the window.")

    recipient = actor
    if not isinstance(actor, discord.Member):
        recipient = bot.get_user(actor.id) or await bot.fetch_user(actor.id)
    try:
        await send_export_dm(recipient, label, len(rows), data, filename)
    except discord.HTTPException:
        raise _err(
            status.HTTP_409_CONFLICT, "dm_failed",
            "The bot couldn't DM you the export. Allow DMs from server members and try again.",
        )
    await _audit(_db_or_503(bot)).log(
        guild.id, actor.id, "message_log_exported",
        target_type="user" if user_id else "channel" if channel_id else None,
        target_id=user_id or channel_id,
        detail={"window": window_label, "count": len(rows), "deleted_only": deleted_only, "source": "dashboard"},
    )
    return {"sent": True, "count": len(rows)}


# ---------------------------------------------------------------------------
# /serverinfo
# ---------------------------------------------------------------------------

@router.get("/guilds/{guild_id}/server-info")
async def server_info(guild: discord.Guild = Depends(get_guild)) -> dict:
    """The `/serverinfo` facts (plus a channel breakdown), from the cache."""
    return stringify_snowflakes({
        "id": str(guild.id),
        "name": guild.name,
        "icon_url": str(guild.icon.url) if guild.icon else None,
        "owner_id": guild.owner_id,
        "member_count": guild.member_count,
        "created_at": guild.created_at.isoformat(),
        "boost_tier": guild.premium_tier,
        "boosts": guild.premium_subscription_count or 0,
        "channels": len(guild.channels),
        "text_channels": len(guild.text_channels),
        "voice_channels": len(guild.voice_channels),
        "categories": len(guild.categories),
        "roles": len(guild.roles),
        "emojis": len(guild.emojis),
        "verification_level": str(guild.verification_level),
    })
