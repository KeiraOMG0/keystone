"""
Dashboard "Automation" page (/dashboard/automation): the web side of the bot's
automation slash commands. Every route is a thin wrapper over the SAME service
instance the cog uses, so a change made here and one made with the slash
command are indistinguishable (storage, audit rows, live events).

GET    /guilds/{guild_id}/automation/options                      template + text-channel pickers
GET    /guilds/{guild_id}/automation/events                       /event list
PUT    /guilds/{guild_id}/automation/events/{event_type}          /event set     {channel_id, template_id, send_dm, actor_id}
DELETE /guilds/{guild_id}/automation/events/{event_type}?actor_id /event clear
POST   /guilds/{guild_id}/automation/events/{event_type}/test     /event test    {actor_id}  (actor = the member; posts for real)
GET    /guilds/{guild_id}/automation/greetings                    /welcome status + /boost status
PUT    /guilds/{guild_id}/automation/greetings/{kind}             setchannel / toggle  {actor_id, channel_id?, enabled?}
POST   /guilds/{guild_id}/automation/greetings/{kind}/test        setchannel test=True equivalent {actor_id}
GET    /guilds/{guild_id}/automation/schedules                    /schedule list (+ next run times)
POST   /guilds/{guild_id}/automation/schedules/preview            cron check + next run times {cron}
POST   /guilds/{guild_id}/automation/schedules                    /schedule add  {template_id, channel_id, cron, label?, actor_id}
PATCH  /guilds/{guild_id}/automation/schedules/{id}               /schedule toggle {enabled, actor_id}
DELETE /guilds/{guild_id}/automation/schedules/{id}?actor_id      /schedule remove
GET    /guilds/{guild_id}/automation/autoroles                    /autorole list (+ grant-time block reason)
GET    /guilds/{guild_id}/automation/autoroles/roles?actor_id     role picker with blocked_reason for that actor
POST   /guilds/{guild_id}/automation/autoroles                    /autorole add  {trigger, role_id, delay_seconds, trigger_role_id?, actor_id}
DELETE /guilds/{guild_id}/automation/autoroles/{id}?actor_id      /autorole remove
GET    /guilds/{guild_id}/automation/managed                      /managed list
DELETE /guilds/{guild_id}/automation/managed/{message_id}?actor_id /managed untrack
POST   /guilds/{guild_id}/automation/managed/sync                 /managed sync  {template_id, actor_id}

Authorization is the dashboard backend's job (server/routes/automation.py:
manage_events / require_staff / manage_schedules / manage_autorole /
manage_templates, the same gates as the commands). `actor_id` is always the
dashboard session user.

Autoroles go through services/self_role_policy.py exactly like /autorole add:
the actor is resolved to a live Member and the role is refused if it is a staff
role, holds a bot permission grant, carries moderator permissions or is above
the actor / the bot (409 {"error": "role_not_allowed", "message"}).

Errors are {"error", "message"} details; 503 {"error": "feature_unavailable"}
when the owning cog is not loaded.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild, get_member
from internal_api.snowflakes import stringify_snowflakes

router = APIRouter(dependencies=[Depends(require_internal_secret)])

_PREFIX = "/guilds/{guild_id}/automation"
EVENT_TYPES = ["member_join", "member_leave", "boost", "role_add", "role_remove"]
GREETING_KINDS = {"welcome": "Welcome", "boost": "Boost"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _err(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"error": error, "message": message})


def _unavailable(what: str) -> HTTPException:
    return _err(status.HTTP_503_SERVICE_UNAVAILABLE, "feature_unavailable", f"{what} is not loaded in the bot right now.")


def _iso(value) -> str | None:
    """SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) -> ISO 8601 with Z."""
    if not value:
        return None
    text = str(value)
    try:
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dt_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _channel_name(guild: discord.Guild, channel_id) -> str | None:
    ch = guild.get_channel(int(channel_id)) if channel_id else None
    return getattr(ch, "name", None)


def _role_name(guild: discord.Guild, role_id) -> str | None:
    role = guild.get_role(int(role_id)) if role_id else None
    return getattr(role, "name", None)


def _text_channel(guild: discord.Guild, channel_id: int) -> discord.TextChannel:
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise _err(status.HTTP_400_BAD_REQUEST, "channel_not_found", "Pick a text channel in this server.")
    return channel


async def _actor(actor_id: int, guild: discord.Guild) -> discord.Member:
    try:
        return await get_member(actor_id, guild)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise _err(status.HTTP_409_CONFLICT, "actor_not_found", "Couldn't find you in the server.")
        raise


async def _template_row(bot, guild: discord.Guild, template_id: int) -> dict:
    from database.repositories.template_repo import TemplateRepository

    row = await TemplateRepository(bot.db).get_by_id(template_id)
    if not row or row["guild_id"] != guild.id:
        raise _err(status.HTTP_404_NOT_FOUND, "template_not_found", "That template no longer exists.")
    return row


def _audit(bot):
    from database.repositories.audit_repo import AuditRepository
    from services.audit_service import AuditService

    return AuditService(AuditRepository(bot.db))


# ---------------------------------------------------------------------------
# service resolution (the cog-owned instances)
# ---------------------------------------------------------------------------

def get_event_service(bot=Depends(get_bot)):
    svc = getattr(bot, "event_service", None)
    if svc is None:
        raise _unavailable("The events cog")
    return svc


def get_scheduler_service(bot=Depends(get_bot)):
    svc = getattr(bot, "scheduler_service", None)
    if svc is None:
        raise _unavailable("The scheduler")
    return svc


def get_autorole_service(bot=Depends(get_bot)):
    svc = getattr(bot, "autorole_service", None)
    if svc is None:
        raise _unavailable("The autorole service")
    return svc


def get_managed_service(bot=Depends(get_bot)):
    cog = bot.get_cog("ManagedMessages")
    svc = getattr(cog, "managed_service", None) if cog is not None else None
    if svc is None:
        raise _unavailable("The managed messages cog")
    return svc


def _greeting_service(bot, kind: str):
    if kind not in GREETING_KINDS:
        raise _err(status.HTTP_404_NOT_FOUND, "not_found", "Unknown greeting kind.")
    cog = bot.get_cog(GREETING_KINDS[kind])
    svc = getattr(cog, "_service", None) if cog is not None else None
    if svc is None:
        raise _unavailable(f"The {kind} cog")
    return svc


# ---------------------------------------------------------------------------
# pickers
# ---------------------------------------------------------------------------

@router.get(_PREFIX + "/options")
async def options(guild: discord.Guild = Depends(get_guild), bot=Depends(get_bot)) -> dict:
    from database.repositories.template_repo import TemplateRepository

    templates = await TemplateRepository(bot.db).list_for_guild(guild.id)
    return {
        "templates": [{"id": t["id"], "name": t["name"], "category": t.get("category")} for t in templates],
        "channels": [
            {"id": str(c.id), "name": c.name, "position": c.position} for c in guild.text_channels
        ],
    }


# ---------------------------------------------------------------------------
# events (/event)
# ---------------------------------------------------------------------------

def _check_event_type(event_type: str) -> None:
    if event_type not in EVENT_TYPES:
        raise _err(status.HTTP_400_BAD_REQUEST, "invalid_event_type", f"Unknown event type '{event_type}'.")


@router.get(_PREFIX + "/events")
async def list_events(guild: discord.Guild = Depends(get_guild), svc=Depends(get_event_service)) -> dict:
    bindings = await svc.list_bindings(guild.id)
    out = []
    for b in bindings:
        out.append({
            "id": b["id"],
            "event_type": b["event_type"],
            "channel_id": b["channel_id"],
            "channel_name": _channel_name(guild, b["channel_id"]),
            "send_dm": bool(b.get("send_dm")),
            "enabled": bool(b.get("enabled")),
            "templates": [{"id": t["id"], "name": t["name"]} for t in b.get("templates", [])],
            "created_by": b.get("created_by"),
            "created_at": _iso(b.get("created_at")),
        })
    return stringify_snowflakes({"event_types": EVENT_TYPES, "bindings": out})


class SetEventRequest(BaseModel):
    channel_id: int
    template_id: int
    send_dm: bool = False
    actor_id: int


@router.put(_PREFIX + "/events/{event_type}")
async def set_event(
    event_type: str,
    body: SetEventRequest,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    svc=Depends(get_event_service),
) -> dict:
    _check_event_type(event_type)
    channel = _text_channel(guild, body.channel_id)
    template = await _template_row(bot, guild, body.template_id)
    errors = await svc.set_binding(
        guild_id=guild.id, event_type=event_type, channel_id=channel.id,
        template_name=template["name"], actor_id=body.actor_id, send_dm=body.send_dm,
    )
    if errors:
        raise _err(status.HTTP_400_BAD_REQUEST, "invalid_request", errors[0])
    return {"ok": True}


@router.delete(_PREFIX + "/events/{event_type}")
async def clear_event(
    event_type: str,
    actor_id: int,
    guild: discord.Guild = Depends(get_guild),
    svc=Depends(get_event_service),
) -> dict:
    _check_event_type(event_type)
    if not await svc.clear_binding(guild.id, event_type, actor_id=actor_id):
        raise _err(status.HTTP_404_NOT_FOUND, "not_found", f"No binding for {event_type}.")
    return {"ok": True}


class ActorRequest(BaseModel):
    actor_id: int


@router.post(_PREFIX + "/events/{event_type}/test")
async def test_event(
    event_type: str,
    body: ActorRequest,
    guild: discord.Guild = Depends(get_guild),
    svc=Depends(get_event_service),
) -> dict:
    """Same as /event test: dispatches the event with the actor as the member,
    so the bound template is posted to the REAL channel (and DMed to the actor
    when the binding has send_dm)."""
    _check_event_type(event_type)
    member = await _actor(body.actor_id, guild)
    sent, errors = await svc.test_event(event_type=event_type, guild=guild, member=member)
    if errors:
        raise _err(status.HTTP_400_BAD_REQUEST, "invalid_request", errors[0])
    if not sent:
        raise _err(
            status.HTTP_409_CONFLICT, "no_binding",
            f"No active binding for {event_type}, or its channel/template was not found.",
        )
    channel_id = await svc.active_channel_id(guild.id, event_type)
    return stringify_snowflakes({"sent": True, "channel_id": channel_id, "channel_name": _channel_name(guild, channel_id)})


# ---------------------------------------------------------------------------
# welcome / boost (/welcome, /boost)
# ---------------------------------------------------------------------------

async def _greeting_state(bot, guild: discord.Guild, kind: str) -> dict:
    cog = bot.get_cog(GREETING_KINDS[kind])
    svc = getattr(cog, "_service", None) if cog is not None else None
    if svc is None:
        return {"available": False, "configured": False, "enabled": False, "channel_id": None, "channel_name": None}
    cfg = await svc.get_config(guild.id) or {}
    channel_id = cfg.get("channel_id")
    return {
        "available": True,
        "configured": bool(channel_id),
        "enabled": bool(cfg.get("enabled")) if cfg else False,
        "channel_id": channel_id,
        "channel_name": _channel_name(guild, channel_id),
        "updated_at": _iso(cfg.get("updated_at")),
    }


@router.get(_PREFIX + "/greetings")
async def get_greetings(guild: discord.Guild = Depends(get_guild), bot=Depends(get_bot)) -> dict:
    return stringify_snowflakes({
        "welcome": await _greeting_state(bot, guild, "welcome"),
        "boost": await _greeting_state(bot, guild, "boost"),
    })


class GreetingUpdate(BaseModel):
    actor_id: int
    channel_id: int | None = None
    enabled: bool | None = None


@router.put(_PREFIX + "/greetings/{kind}")
async def update_greeting(
    kind: str,
    body: GreetingUpdate,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
) -> dict:
    svc = _greeting_service(bot, kind)
    if body.channel_id is None and body.enabled is None:
        raise _err(status.HTTP_400_BAD_REQUEST, "invalid_request", "Nothing to change.")
    audit = _audit(bot)
    if body.channel_id is not None:
        channel = _text_channel(guild, body.channel_id)
        await svc.set_channel(guild.id, channel.id)  # also turns it on, like /<kind> setchannel
        await audit.log(guild.id, body.actor_id, f"{kind}_setchannel", target_type="channel",
                        target_id=channel.id, detail={"test": False})
    if body.enabled is not None:
        await svc.set_enabled(guild.id, body.enabled)
        await audit.log(guild.id, body.actor_id, f"{kind}_toggle", detail={"enabled": body.enabled})
    return stringify_snowflakes(await _greeting_state(bot, guild, kind))


@router.post(_PREFIX + "/greetings/{kind}/test")
async def test_greeting(
    kind: str,
    body: ActorRequest,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
) -> dict:
    """Same as `/<kind> setchannel test:True`: posts the real welcome/boost
    message for the actor to the configured channel."""
    svc = _greeting_service(bot, kind)
    cfg = await svc.get_config(guild.id) or {}
    if not cfg.get("channel_id"):
        raise _err(status.HTTP_409_CONFLICT, "channel_not_set", "Set a channel first.")
    channel = guild.get_channel(cfg["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        raise _err(status.HTTP_409_CONFLICT, "channel_not_found", "The configured channel no longer exists.")
    member = await _actor(body.actor_id, guild)
    send = svc.send_welcome if kind == "welcome" else svc.send_boost
    try:
        message = await send(channel, member)
    except discord.Forbidden:
        raise _err(status.HTTP_409_CONFLICT, "discord_forbidden", "The bot can't send messages in that channel.")
    except discord.HTTPException as exc:
        raise _err(status.HTTP_502_BAD_GATEWAY, "discord_error", f"Discord refused the message ({exc.status}).")
    await _audit(bot).log(guild.id, body.actor_id, f"{kind}_test", target_type="channel", target_id=channel.id)
    return stringify_snowflakes({
        "sent": True, "channel_id": channel.id, "channel_name": channel.name,
        "jump_url": getattr(message, "jump_url", None),
    })


# ---------------------------------------------------------------------------
# schedules (/schedule)
# ---------------------------------------------------------------------------

@router.get(_PREFIX + "/schedules")
async def list_schedules(guild: discord.Guild = Depends(get_guild), svc=Depends(get_scheduler_service)) -> dict:
    from services.scheduler_service import next_runs

    out = []
    for s in await svc.list_schedules(guild.id):
        runs = next_runs(s["cron_expr"], 3) or []
        out.append({
            "id": s["id"],
            "label": s.get("label"),
            "template_id": s["template_id"],
            "template_name": s.get("template_name"),
            "channel_id": s["channel_id"],
            "channel_name": _channel_name(guild, s["channel_id"]),
            "cron_expr": s["cron_expr"],
            "enabled": bool(s.get("enabled")),
            "next_run_at": _iso(s.get("next_run_at")),
            "last_run_at": _iso(s.get("last_run_at")),
            "upcoming": [_dt_iso(d) for d in runs],
            "created_by": s.get("created_by"),
            "created_at": _iso(s.get("created_at")),
        })
    return stringify_snowflakes({"schedules": out})


class CronPreviewRequest(BaseModel):
    cron: str = Field(max_length=200)


@router.post(_PREFIX + "/schedules/preview")
async def preview_cron(body: CronPreviewRequest, guild: discord.Guild = Depends(get_guild)) -> dict:
    """Validates with the scheduler's own parser (croniter) and lists the next
    5 UTC run times. Never writes anything."""
    from services.scheduler_service import next_runs

    runs = next_runs(body.cron.strip(), 5)
    if not runs:
        return {
            "valid": False,
            "error": "Not a valid cron expression. Five fields: minute hour day-of-month month day-of-week (UTC), e.g. 0 9 * * 1.",
            "next_runs": [],
        }
    return {"valid": True, "error": None, "next_runs": [_dt_iso(d) for d in runs]}


class CreateScheduleRequest(BaseModel):
    template_id: int
    channel_id: int
    cron: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=200)
    actor_id: int


@router.post(_PREFIX + "/schedules")
async def create_schedule(
    body: CreateScheduleRequest,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    svc=Depends(get_scheduler_service),
) -> dict:
    channel = _text_channel(guild, body.channel_id)
    template = await _template_row(bot, guild, body.template_id)
    label = (body.label or "").strip() or None
    schedule_id, errors = await svc.add(
        guild_id=guild.id, channel_id=channel.id, template_name=template["name"],
        cron_expr=body.cron.strip(), created_by=body.actor_id, label=label,
    )
    if errors:
        raise _err(status.HTTP_400_BAD_REQUEST, "invalid_request", errors[0].replace("`", ""))
    return {"schedule_id": schedule_id}


class ToggleScheduleRequest(BaseModel):
    enabled: bool
    actor_id: int


@router.patch(_PREFIX + "/schedules/{schedule_id}")
async def toggle_schedule(
    schedule_id: int,
    body: ToggleScheduleRequest,
    guild: discord.Guild = Depends(get_guild),
    svc=Depends(get_scheduler_service),
) -> dict:
    if not await svc.toggle(guild.id, schedule_id, body.enabled, actor_id=body.actor_id):
        raise _err(status.HTTP_404_NOT_FOUND, "not_found", f"Schedule {schedule_id} not found.")
    return {"ok": True}


@router.delete(_PREFIX + "/schedules/{schedule_id}")
async def remove_schedule(
    schedule_id: int,
    actor_id: int,
    guild: discord.Guild = Depends(get_guild),
    svc=Depends(get_scheduler_service),
) -> dict:
    if not await svc.remove(guild.id, schedule_id, actor_id=actor_id):
        raise _err(status.HTTP_404_NOT_FOUND, "not_found", f"Schedule {schedule_id} not found.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# autoroles (/autorole)
# ---------------------------------------------------------------------------

@router.get(_PREFIX + "/autoroles")
async def list_autoroles(
    guild: discord.Guild = Depends(get_guild),
    svc=Depends(get_autorole_service),
) -> dict:
    from services import self_role_policy
    from services.autorole_service import MAX_DELAY_SECONDS, VALID_TRIGGERS

    rules = await svc.list_rules(guild.id)
    # Grant-time view (no actor): would the bot hand this role out right now?
    try:
        privileged = await self_role_policy.privileged_role_ids(svc._perms, guild.id)
        reasons = self_role_policy.block_reasons(
            [r for r in (guild.get_role(x["role_id"]) for x in rules) if r is not None], guild, None, privileged,
        )
    except Exception:
        reasons = {}
    out = []
    for r in rules:
        role = guild.get_role(r["role_id"])
        out.append({
            "id": r["id"],
            "trigger_type": r["trigger_type"],
            "trigger_role_id": r.get("trigger_role_id"),
            "trigger_role_name": _role_name(guild, r.get("trigger_role_id")),
            "role_id": r["role_id"],
            "role_name": getattr(role, "name", None),
            "delay_seconds": r.get("delay_seconds") or 0,
            "enabled": bool(r.get("enabled")),
            "blocked_now": "That role no longer exists." if role is None else reasons.get(role.id),
            "created_by": r.get("created_by"),
            "created_at": _iso(r.get("created_at")),
        })
    return stringify_snowflakes({
        "rules": out, "triggers": sorted(VALID_TRIGGERS), "max_delay_seconds": MAX_DELAY_SECONDS,
    })


@router.get(_PREFIX + "/autoroles/roles")
async def autorole_role_picker(
    actor_id: int | None = None,
    guild: discord.Guild = Depends(get_guild),
    svc=Depends(get_autorole_service),
) -> dict:
    """Every role (except @everyone), highest first, with `blocked_reason` for
    this actor (None = may be used as an autorole). A UI hint only: POST
    re-checks. The trigger-role picker uses the same list ignoring the reason."""
    from services import self_role_policy

    actor = None
    if actor_id is not None:
        try:
            actor = await get_member(actor_id, guild)
        except HTTPException:
            actor = None
    roles = [r for r in guild.roles if not r.is_default()]
    try:
        privileged = await self_role_policy.privileged_role_ids(svc._perms, guild.id)
        reasons = self_role_policy.block_reasons(roles, guild, actor, privileged)
        if actor is None:
            reasons = {rid: (why or "Couldn't find you in the server.") for rid, why in reasons.items()}
    except Exception:
        reasons = {r.id: self_role_policy.GRANTS_UNAVAILABLE for r in roles}
    out = [
        {
            "id": str(role.id),
            "name": role.name,
            "color": f"#{role.color.value:06x}" if role.color.value else None,
            "position": role.position,
            "managed": role.managed,
            "blocked_reason": reasons.get(role.id),
        }
        for role in roles
    ]
    out.sort(key=lambda r: r["position"], reverse=True)
    return {"roles": out}


class CreateAutoroleRequest(BaseModel):
    trigger: str
    role_id: int
    delay_seconds: int = 0
    trigger_role_id: int | None = None
    actor_id: int


@router.post(_PREFIX + "/autoroles")
async def create_autorole(
    body: CreateAutoroleRequest,
    guild: discord.Guild = Depends(get_guild),
    svc=Depends(get_autorole_service),
) -> dict:
    from services import self_role_policy

    role = guild.get_role(body.role_id)
    if role is None:
        raise _err(status.HTTP_400_BAD_REQUEST, "role_not_found", "That role no longer exists.")
    actor = await _actor(body.actor_id, guild)
    # Same check AutoroleService.add_rule makes (it re-checks), surfaced as its own error code.
    reason = await self_role_policy.configure_block_reason(role, guild, actor, svc._perms)
    if reason:
        raise _err(status.HTTP_409_CONFLICT, "role_not_allowed", reason)
    rule_id, errors = await svc.add_rule(
        guild_id=guild.id,
        trigger_type=body.trigger,
        role_id=role.id,
        created_by=actor.id,
        delay_seconds=body.delay_seconds,
        trigger_role_id=body.trigger_role_id if body.trigger == "role_add" else None,
        guild=guild,
        actor=actor,
    )
    if errors:
        raise _err(status.HTTP_400_BAD_REQUEST, "invalid_request", errors[0])
    return {"rule_id": rule_id}


@router.delete(_PREFIX + "/autoroles/{rule_id}")
async def remove_autorole(
    rule_id: int,
    actor_id: int,
    guild: discord.Guild = Depends(get_guild),
    svc=Depends(get_autorole_service),
) -> dict:
    if not await svc.remove_rule(guild.id, rule_id, actor_id=actor_id):
        raise _err(status.HTTP_404_NOT_FOUND, "not_found", f"Rule {rule_id} not found.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# managed messages (/managed)
# ---------------------------------------------------------------------------

@router.get(_PREFIX + "/managed")
async def list_managed(guild: discord.Guild = Depends(get_guild), svc=Depends(get_managed_service)) -> dict:
    out = []
    for r in await svc.list_managed(guild.id):
        out.append({
            "id": r["id"],
            "message_id": r["message_id"],
            "channel_id": r["channel_id"],
            "channel_name": _channel_name(guild, r["channel_id"]),
            "template_id": r["template_id"],
            "template_name": r.get("template_name"),
            "last_synced_at": _iso(r.get("last_synced_at")),
            "sync_failed": bool(r.get("sync_failed")),
            "created_by": r.get("created_by"),
            "created_at": _iso(r.get("created_at")),
            "jump_url": f"https://discord.com/channels/{guild.id}/{r['channel_id']}/{r['message_id']}",
        })
    return stringify_snowflakes({"messages": out})


@router.delete(_PREFIX + "/managed/{message_id}")
async def untrack_managed(
    message_id: int,
    actor_id: int,
    guild: discord.Guild = Depends(get_guild),
    svc=Depends(get_managed_service),
) -> dict:
    if not await svc.untrack(guild.id, message_id, actor_id=actor_id):
        raise _err(status.HTTP_404_NOT_FOUND, "not_found", "No managed message with that ID in this server.")
    return {"ok": True}


class SyncManagedRequest(BaseModel):
    template_id: int
    actor_id: int


@router.post(_PREFIX + "/managed/sync")
async def sync_managed(
    body: SyncManagedRequest,
    guild: discord.Guild = Depends(get_guild),
    bot=Depends(get_bot),
    svc=Depends(get_managed_service),
) -> dict:
    template = await _template_row(bot, guild, body.template_id)
    await svc.sync_now(guild.id, template["id"], actor_id=body.actor_id)
    return {"ok": True, "template_name": template["name"]}
