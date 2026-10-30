"""
Birthdays (dashboard /dashboard/birthdays). Thin wrapper over the SAME
BirthdayCore (bot.birthday_core, cogs/birthdays.py) and BirthdayService
(services/birthday_service.py) the /birthday, /config, /override and
/birthdays slash commands use, so both paths validate, reschedule, publish
the live `birthdays` topic and write audit rows identically.

GET    /guilds/{guild_id}/birthdays                      settings + status + entries + recent/upcoming
GET    /guilds/{guild_id}/birthdays/timezones            every IANA zone the bot accepts
GET    /guilds/{guild_id}/birthdays/role-options?actor_id=  roles with blocked_reason for that actor
PUT    /guilds/{guild_id}/birthdays/config               {actor_id, ...partial}  -> overview + warnings
PUT    /guilds/{guild_id}/birthdays/entries/{user_id}    {actor_id, month, day, timezone?, clear_timezone?}
DELETE /guilds/{guild_id}/birthdays/entries/{user_id}?actor_id=
POST   /guilds/{guild_id}/birthdays/test                 {actor_id, user_ids[1..5]}
POST   /guilds/{guild_id}/birthdays/reset-timers         {actor_id}
GET    /guilds/{guild_id}/birthdays/export?format=plaintext|csv|ics&actor_id=
POST   /guilds/{guild_id}/birthdays/import               {actor_id, filename, content, dry_run}

Authorization (`manage_birthdays`) happens in the dashboard backend
(server/routes/birthdays.py); `actor_id` is always the session user.
Errors: {"error": code, "message": text} (400/404/409/422); 503 when the
birthday cogs are not loaded.
"""

from __future__ import annotations

import types
from datetime import datetime

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

import config
from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from internal_api.snowflakes import stringify_snowflakes
from services import self_role_policy
from services.birthday_service import (
    ANNOUNCE_MESSAGE_MAX,
    EVERYONE_PLACEHOLDER,
    EXPORT_FORMATS,
    IMPORT_MAX_BYTES,
    TEST_MEMBERS_MAX,
    all_timezones,
    birthday_role_block_reason,
    build_export,
    channel_status,
    days_until_next,
    everyone_block_reason,
    format_birthday,
    hardcoded_mentions,
    nearest_birthdays,
    parse_timezone,
    role_status,
    valid_month_day,
)

router = APIRouter(dependencies=[Depends(require_internal_secret)])


def _err(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"error": error, "message": message})


def get_birthday_core(bot: commands.Bot = Depends(get_bot)):
    core = getattr(bot, "birthday_core", None)
    if core is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Birthday cogs are not loaded - birthday service unavailable.",
        )
    return core


async def _member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None
    except discord.HTTPException:
        return None


async def _actor(guild: discord.Guild, actor_id: int):
    """The acting member. The owner (OWNER_ID / guild owner) may act from
    outside the guild (a stand-in that self_role_policy treats as owner)."""
    member = await _member(guild, actor_id)
    if member is not None:
        return member
    if actor_id and (actor_id == int(getattr(config, "OWNER_ID", 0) or 0) or actor_id == guild.owner_id):
        return types.SimpleNamespace(id=actor_id, roles=[], top_role=None)
    raise _err(403, "actor_not_member", "Couldn't find you in the server.")


def _tz_name(tz_str: str) -> str | None:
    """Canonical IANA name ("europe/london" -> "Europe/London"), or None."""
    tz = parse_timezone(tz_str.strip()) if tz_str else None
    return tz.key if tz is not None else None


def _settings_out(settings: dict) -> dict:
    return {
        "birthday_role_id": settings.get("birthday_role_id"),
        "announce_channel_id": settings.get("announce_channel_id"),
        "guild_timezone": settings.get("guild_timezone"),
        "announce_message": settings.get("announce_message"),
        "announce_message_pl": settings.get("announce_message_pl"),
        "announce_ping": bool(settings.get("announce_ping")),
        "ping_user": bool(settings.get("ping_user")),
        "add_only": bool(settings.get("add_only")),
        "private_confirms": bool(settings.get("private_confirms")),
    }


def _ids_to_str(d: dict) -> dict:
    return {**d, "id": str(d["id"]) if d.get("id") is not None else None}


async def build_overview(core, guild: discord.Guild) -> dict:
    settings = await core.svc.get_settings(guild.id)
    tz_name = settings.get("guild_timezone") or "UTC"
    guild_tz = parse_timezone(tz_name) or parse_timezone("UTC")
    today = datetime.now(guild_tz).date()
    rows = await core.svc.get_all_for_guild(guild.id)

    entries = []
    for row in rows:
        m = guild.get_member(row["user_id"])
        next_date, days = days_until_next(row["birth_month"], row["birth_day"], today)
        entries.append({
            "user_id": row["user_id"],
            "month": row["birth_month"],
            "day": row["birth_day"],
            "date_display": format_birthday(row["birth_month"], row["birth_day"]),
            "user_timezone": row.get("user_timezone"),
            "last_celebrated": row.get("last_celebrated") or 0,
            "next_date": next_date.isoformat(),
            "days_until": days,
            "in_guild": m is not None,
            "display_name": m.display_name if m else None,
            "username": m.name if m else None,
        })
    nearest = [
        {
            "offset": g["offset"],
            "date": g["date"].isoformat(),
            "month": g["month"],
            "day": g["day"],
            "user_ids": [str(r["user_id"]) for r in g["rows"]],
        }
        for g in nearest_birthdays(rows, today)
    ]
    return stringify_snowflakes({
        "settings": _settings_out(settings),
        "status": {
            "registered": len(rows),
            "scheduled": core.scheduled_count(guild.id),
            "server_timezone": tz_name,
            "server_today": today.isoformat(),
            "role": _ids_to_str(role_status(guild, settings.get("birthday_role_id"))),
            "channel": _ids_to_str(channel_status(guild, settings.get("announce_channel_id"))),
        },
        "entries": entries,
        "nearest": nearest,
        "limits": {
            "announce_message_max": ANNOUNCE_MESSAGE_MAX,
            "import_max_bytes": IMPORT_MAX_BYTES,
            "test_members_max": TEST_MEMBERS_MAX,
        },
    })


@router.get("/guilds/{guild_id}/birthdays")
async def overview(guild: discord.Guild = Depends(get_guild), core=Depends(get_birthday_core)) -> dict:
    return await build_overview(core, guild)


@router.get("/guilds/{guild_id}/birthdays/timezones")
async def timezones(guild: discord.Guild = Depends(get_guild)) -> dict:
    return {"timezones": all_timezones()}


@router.get("/guilds/{guild_id}/birthdays/role-options")
async def role_options(
    actor_id: int = Query(...),
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    """Every role except @everyone, top first, with `blocked_reason` (None =
    this actor may pick it as the birthday role)."""
    actor = await _actor(guild, actor_id)
    roles = [r for r in reversed(guild.roles) if not r.is_default()]
    try:
        privileged = await self_role_policy.privileged_role_ids(getattr(bot, "permission_service", None), guild.id)
        reasons = self_role_policy.block_reasons(roles, guild, actor, privileged)
    except Exception:
        reasons = {r.id: self_role_policy.GRANTS_UNAVAILABLE for r in roles}
    out = []
    for r in roles:
        reason = "That role cannot be used as a birthday role." if r.managed else reasons.get(r.id)
        out.append({
            "id": str(r.id), "name": r.name, "color": r.color.value, "position": r.position,
            "managed": r.managed, "blocked_reason": reason,
        })
    return {"roles": out}


class ConfigUpdate(BaseModel):
    actor_id: int
    birthday_role_id: int | None = None
    clear_birthday_role: bool = False
    announce_channel_id: int | None = None
    clear_announce_channel: bool = False
    guild_timezone: str | None = Field(default=None, max_length=64)
    clear_guild_timezone: bool = False
    announce_message: str | None = Field(default=None, max_length=ANNOUNCE_MESSAGE_MAX)
    announce_message_pl: str | None = Field(default=None, max_length=ANNOUNCE_MESSAGE_MAX)
    # True = write announce_message / announce_message_pl as sent (None / "" clears).
    set_messages: bool = False
    announce_ping: bool | None = None
    ping_user: bool | None = None
    add_only: bool | None = None
    private_confirms: bool | None = None


@router.put("/guilds/{guild_id}/birthdays/config")
async def update_config(
    body: ConfigUpdate,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
    core=Depends(get_birthday_core),
) -> dict:
    actor = await _actor(guild, body.actor_id)
    changes: dict = {}
    fields: dict[str, str] = {}

    if body.clear_birthday_role:
        changes["birthday_role_id"] = None
    elif body.birthday_role_id is not None:
        role = guild.get_role(body.birthday_role_id)
        reason = await birthday_role_block_reason(role, guild, actor, getattr(bot, "permission_service", None))
        if reason:
            fields["birthday_role_id"] = reason
        else:
            changes["birthday_role_id"] = role.id

    if body.clear_announce_channel:
        changes["announce_channel_id"] = None
    elif body.announce_channel_id is not None:
        channel = guild.get_channel(body.announce_channel_id)
        if not isinstance(channel, discord.TextChannel):
            fields["announce_channel_id"] = "Pick a text channel in this server."
        else:
            changes["announce_channel_id"] = channel.id

    if body.clear_guild_timezone:
        changes["guild_timezone"] = None
    elif body.guild_timezone is not None and body.guild_timezone.strip():
        name = _tz_name(body.guild_timezone)
        if name is None:
            fields["guild_timezone"] = f"'{body.guild_timezone}' is not a valid timezone."
        else:
            changes["guild_timezone"] = name

    before = await core.svc.get_settings(guild.id)
    if body.set_messages:
        changes["announce_message"] = (body.announce_message or "").strip() or None
        changes["announce_message_pl"] = (body.announce_message_pl or "").strip() or None
        perm_service = getattr(bot, "permission_service", None)
        blocked = everyone_block_reason(
            before.get("announce_message"), before.get("announce_message_pl"),
            changes["announce_message"], changes["announce_message_pl"],
            bool(perm_service is not None and perm_service.is_staff(actor)),
        )
        if blocked:
            single = changes["announce_message"]
            field = (
                "announce_message"
                if single and EVERYONE_PLACEHOLDER in single and single != before.get("announce_message")
                else "announce_message_pl"
            )
            fields[field] = blocked

    for key in ("announce_ping", "ping_user", "add_only", "private_confirms"):
        value = getattr(body, key)
        if value is not None:
            changes[key] = 1 if value else 0

    if fields:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_config", "message": next(iter(fields.values())), "fields": fields},
        )

    changed = {k: v for k, v in changes.items() if before.get(k) != v}
    if changed:
        await core.svc.update_settings(guild.id, **changed)
        if "guild_timezone" in changed:
            await core.schedule_guild(guild.id)
        detail: dict = {"fields": sorted(changed), "source": "dashboard"}
        for k in ("birthday_role_id", "announce_channel_id", "guild_timezone", "announce_ping",
                  "ping_user", "add_only", "private_confirms"):
            if k in changed:
                detail[k] = changed[k]
        await core.audit(guild.id, body.actor_id, "birthday_config_update", None, None, detail)

    warnings = []
    hits = hardcoded_mentions(changes.get("announce_message"), changes.get("announce_message_pl")) if body.set_messages else []
    if hits:
        warnings.append(
            f"Found a hardcoded mention ({', '.join(hits)}). Use {{mention}} instead so it pings the "
            "actual birthday person, not a fixed user."
        )
    out = await build_overview(core, guild)
    out["warnings"] = warnings
    out["changed"] = sorted(changed)
    return out


class EntryUpdate(BaseModel):
    actor_id: int
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    timezone: str | None = Field(default=None, max_length=64)
    clear_timezone: bool = False


@router.put("/guilds/{guild_id}/birthdays/entries/{user_id}")
async def set_entry(
    user_id: int,
    body: EntryUpdate,
    guild: discord.Guild = Depends(get_guild),
    core=Depends(get_birthday_core),
) -> dict:
    """/override set-birthday (+ set-timezone): bypasses add-only. A new
    entry needs the member to be in the server; an existing one (member left)
    can still be edited."""
    await _actor(guild, body.actor_id)
    if not valid_month_day(body.month, body.day):
        raise _err(422, "invalid_date", f"Day {body.day} is not valid for that month.")
    tz_name = None
    if body.timezone and body.timezone.strip() and not body.clear_timezone:
        tz_name = _tz_name(body.timezone)
        if tz_name is None:
            raise _err(422, "invalid_timezone", f"'{body.timezone}' is not a recognised timezone.")
    existing = await core.svc.get_birthday(guild.id, user_id)
    if existing is None and await _member(guild, user_id) is None:
        raise _err(404, "member_not_found", "That user is not in the server.")

    ok, msg = await core.svc.set_birthday(
        guild.id, user_id, str(body.day), str(body.month), tz_str=tz_name, override=True
    )
    if not ok:
        raise _err(422, "invalid_birthday", msg)
    if body.clear_timezone:
        await core.svc.clear_timezone(guild.id, user_id)
    await core.schedule_guild(guild.id)
    row = await core.svc.get_birthday(guild.id, user_id) or {}
    await core.audit(
        guild.id, body.actor_id, "birthday_set", "member", user_id,
        {"month": row.get("birth_month"), "day": row.get("birth_day"), "timezone": row.get("user_timezone"),
         "created": existing is None, "source": "dashboard"},
    )
    return stringify_snowflakes({
        "ok": True,
        "created": existing is None,
        "entry": {
            "user_id": user_id, "month": row.get("birth_month"), "day": row.get("birth_day"),
            "user_timezone": row.get("user_timezone"),
        },
    })


@router.delete("/guilds/{guild_id}/birthdays/entries/{user_id}")
async def remove_entry(
    user_id: int,
    actor_id: int = Query(...),
    guild: discord.Guild = Depends(get_guild),
    core=Depends(get_birthday_core),
) -> dict:
    await _actor(guild, actor_id)
    removed = await core.svc.remove_birthday(guild.id, user_id)
    core.cancel_scheduled(guild.id, user_id)
    if removed:
        await core.audit(guild.id, actor_id, "birthday_remove", "member", user_id, {"source": "dashboard"})
    return {"removed": removed}


class TestBody(BaseModel):
    actor_id: int
    user_ids: list[int] = Field(min_length=1, max_length=TEST_MEMBERS_MAX)


_TEST_MESSAGES = {
    "channel_not_set": "The announcement channel is not configured.",
    "channel_missing": "The configured channel no longer exists.",
    "channel_no_permission": "The bot can't send messages in the announcement channel.",
}


@router.post("/guilds/{guild_id}/birthdays/test")
async def test_announcement(
    body: TestBody,
    guild: discord.Guild = Depends(get_guild),
    core=Depends(get_birthday_core),
) -> dict:
    """Same as /config announce test: the configured announcement for these
    members, sent to the announcement channel. Nothing marked celebrated."""
    await _actor(guild, body.actor_id)
    members = []
    for uid in dict.fromkeys(body.user_ids):
        m = await _member(guild, uid)
        if m is None:
            raise _err(404, "member_not_found", "One of the chosen members is not in the server.")
        members.append(m)
    try:
        error = await core.send_test_announcement(guild, members)
    except discord.Forbidden:
        raise _err(409, "discord_forbidden", "Discord refused the message (missing permissions).")
    except discord.HTTPException as exc:
        raise _err(400, "discord_rejected", f"Discord rejected the message: {exc.text or exc}"[:300])
    if error:
        raise _err(409, error, _TEST_MESSAGES.get(error, error))
    settings = await core.svc.get_settings(guild.id)
    channel_id = settings.get("announce_channel_id")
    await core.audit(
        guild.id, body.actor_id, "birthday_test_announcement", "channel", channel_id,
        {"user_ids": [str(m.id) for m in members], "source": "dashboard"},
    )
    return stringify_snowflakes({"ok": True, "channel_id": channel_id, "member_count": len(members)})


class ActorBody(BaseModel):
    actor_id: int


@router.post("/guilds/{guild_id}/birthdays/reset-timers")
async def reset_timers(
    body: ActorBody,
    guild: discord.Guild = Depends(get_guild),
    core=Depends(get_birthday_core),
) -> dict:
    """Same as /config announce timers-reset."""
    await _actor(guild, body.actor_id)
    count = await core.reset_timers(guild.id)
    await core.audit(guild.id, body.actor_id, "birthday_timers_reset", None, None, {"reset": count, "source": "dashboard"})
    return {"reset": count, "scheduled": core.scheduled_count(guild.id)}


@router.get("/guilds/{guild_id}/birthdays/export")
async def export(
    format: str = Query("plaintext"),
    actor_id: int = Query(...),
    guild: discord.Guild = Depends(get_guild),
    core=Depends(get_birthday_core),
) -> dict:
    if format not in EXPORT_FORMATS:
        raise _err(422, "invalid_format", "Format must be plaintext, csv or ics.")
    rows = await core.svc.get_all_for_guild(guild.id)
    member_map = {m.id: m for m in guild.members}
    filename, content = build_export(format, guild.id, guild.name, rows, member_map)
    await core.audit(guild.id, actor_id, "birthday_export", None, None,
                     {"format": format, "count": len(rows), "source": "dashboard"})
    return {"filename": filename, "content": content, "count": len(rows)}


class ImportBody(BaseModel):
    actor_id: int
    filename: str = Field(max_length=255)
    content: str = Field(max_length=IMPORT_MAX_BYTES)
    dry_run: bool = True


@router.post("/guilds/{guild_id}/birthdays/import")
async def import_birthdays(
    body: ImportBody,
    guild: discord.Guild = Depends(get_guild),
    core=Depends(get_birthday_core),
) -> dict:
    """Same file format and limits as /birthdays import (.txt, <= 2 MB).
    dry_run=True returns a preview; False imports."""
    await _actor(guild, body.actor_id)
    if not body.filename.lower().endswith(".txt"):
        raise _err(422, "invalid_file", "Please upload a .txt file.")
    if len(body.content.encode("utf-8")) > IMPORT_MAX_BYTES:
        raise _err(422, "file_too_large", "File too large (max 2 MB).")
    preview = await core.svc.preview_import(guild.id, body.content)
    preview["invalid"] = preview["invalid"][:200]
    preview["entries"] = [
        {**e, "user_id": str(e["user_id"]), "in_guild": guild.get_member(e["user_id"]) is not None}
        for e in preview["entries"][:500]
    ]
    if body.dry_run:
        return {"dry_run": True, **preview}
    imported, skipped = await core.svc.import_from_text(guild.id, body.content)
    await core.schedule_guild(guild.id)
    await core.audit(
        guild.id, body.actor_id, "birthday_import", None, None,
        {"imported": imported, "skipped": skipped, "new": preview["new"], "updated": preview["updated"],
         "filename": body.filename[:100], "source": "dashboard"},
    )
    return {
        "dry_run": False, **preview, "imported": imported, "skipped": skipped,
        "scheduled": core.scheduled_count(guild.id),
    }
