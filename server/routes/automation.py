"""
Dashboard "Automation" page (/dashboard/automation): web parity for the bot's
automation slash commands. Thin, permission-gated client of
internal_api/routes/automation.py.

Gates (the SAME ones the slash commands check; staff = owner / STAFF_ROLE_IDS
bypass every grant, as everywhere):

  Events tab            /event set|clear|list|test       manage_events
  Welcome & Boost tab   /welcome, /boost                 staff_base (same as the cogs)
  Schedules tab         /schedule add|remove|toggle|list manage_schedules
  Autoroles tab         /autorole add|remove|list        manage_autorole
  Managed messages tab  /managed list|untrack|sync       manage_templates
  /api/automation/options (template + channel pickers)    any of the four grants (or staff)

Note on Welcome & Boost: the cogs gate on `is_any_staff`, which is
has_permission("staff_base"), so the dashboard uses require_permission("staff_base")
too (exact parity). The picker data (/options) also accepts staff_base, since the
Welcome & Boost tab needs the channel list.

GET    /api/automation/options
GET    /api/automation/events
PUT    /api/automation/events/{event_type}          {channel_id, template_id, send_dm}
DELETE /api/automation/events/{event_type}
POST   /api/automation/events/{event_type}/test     posts to the REAL channel, session user as the member
GET    /api/automation/greetings
PUT    /api/automation/greetings/{kind}             {channel_id?, enabled?}   kind = welcome | boost
POST   /api/automation/greetings/{kind}/test        posts the real message for the session user
GET    /api/automation/schedules
POST   /api/automation/schedules/preview            {cron} -> valid / error / next 5 run times (UTC)
POST   /api/automation/schedules                    {template_id, channel_id, cron, label?}
PATCH  /api/automation/schedules/{id}               {enabled}
DELETE /api/automation/schedules/{id}
GET    /api/automation/autoroles
GET    /api/automation/autoroles/roles              picker with blocked_reason for the session user
POST   /api/automation/autoroles                    {trigger, role_id, delay_seconds, trigger_role_id?}
DELETE /api/automation/autoroles/{id}
GET    /api/automation/managed
DELETE /api/automation/managed/{message_id}
POST   /api/automation/managed/sync                 {template_id}

The actor (audit rows, self-role escalation check, the member of an event
test) is ALWAYS the session user, never taken from the body. Snowflakes
arrive as strings. Errors: the bot's {"error", "message"} details pass
through (400/404/409); 503 -> {"error": "feature_unavailable"}; unreachable
bot -> 502 {"error": "internal_api_unavailable"}.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from server import config
from server.db.models import Session
from server.permissions.guards import NOT_A_MEMBER_DETAIL, member_gone, require_permission, require_session
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/automation", tags=["automation"])

AUTOMATION_PERMISSIONS = ("manage_events", "manage_schedules", "manage_autorole", "manage_templates")
# Welcome & Boost (staff_base) also needs the picker data.
_OPTIONS_PERMISSIONS = (*AUTOMATION_PERMISSIONS, "staff_base")
EVENT_TYPES = {"member_join", "member_leave", "boost", "role_add", "role_remove"}
GREETING_KINDS = {"welcome", "boost"}
TRIGGERS = {"join", "boost", "role_add"}

_GUILD = lambda: config.DASHBOARD_GUILD_ID  # noqa: E731 - read at call time (tests patch config)


async def require_any_automation_permission(session: Session = Depends(require_session)) -> Session:
    """Picker data for the page: allowed with ANY of the automation grants
    (staff pass, since the bot's bulk check returns True for staff)."""
    if not session.is_guild_member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"error": "not_a_member"})
    try:
        perms = await internal_client.check_permissions_bulk(
            config.DASHBOARD_GUILD_ID, session.discord_user_id, list(_OPTIONS_PERMISSIONS)
        )
    except InternalAPIError as exc:
        if member_gone(exc):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_MEMBER_DETAIL) from exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"}) from exc
    if not any(perms.get(p) for p in _OPTIONS_PERMISSIONS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "forbidden",
                "required_permission": " / ".join(_OPTIONS_PERMISSIONS),
                "message": "You need one of the automation permissions.",
            },
        )
    return session


def _translate(exc: InternalAPIError) -> HTTPException:
    code = exc.status_code
    detail = exc.detail if isinstance(exc.detail, dict) else None
    if code in (400, 404, 409, 422):
        if detail is None:
            detail = {"error": "invalid_request" if code in (400, 422) else ("not_found" if code == 404 else "conflict")}
        return HTTPException(status_code=400 if code == 422 else code, detail=detail)
    if code == 503:
        return HTTPException(status_code=503, detail=detail or {"error": "feature_unavailable"})
    if code == 502 and detail is not None:
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


def _snowflake(value: str | None, what: str = "ID") -> int:
    if not isinstance(value, str) or not value.isdigit() or not (15 <= len(value) <= 21):
        raise HTTPException(status_code=400, detail={"error": "invalid_id", "message": f"Not a Discord {what}."})
    return int(value)


def _check(value: str, allowed: set[str], error: str) -> None:
    if value not in allowed:
        raise HTTPException(status_code=400, detail={"error": error, "message": f"Unknown value '{value}'."})


async def _call(coro):
    try:
        return await coro
    except InternalAPIError as exc:
        raise _translate(exc)


# ---------------------------------------------------------------------------
# pickers
# ---------------------------------------------------------------------------

@router.get("/options")
async def options(session: Session = Depends(require_any_automation_permission)) -> dict:
    return await _call(internal_client.get_automation_options(_GUILD()))


# ---------------------------------------------------------------------------
# events (manage_events, like /event)
# ---------------------------------------------------------------------------

_EVENTS = "manage_events"


@router.get("/events")
async def list_events(session: Session = Depends(require_permission(_EVENTS))) -> dict:
    return await _call(internal_client.list_event_bindings(_GUILD()))


class SetEventBody(BaseModel):
    channel_id: str
    template_id: int
    send_dm: bool = False


@router.put("/events/{event_type}")
async def set_event(event_type: str, body: SetEventBody, session: Session = Depends(require_permission(_EVENTS))) -> dict:
    _check(event_type, EVENT_TYPES, "invalid_event_type")
    payload = {
        "channel_id": _snowflake(body.channel_id, "channel ID"),
        "template_id": body.template_id,
        "send_dm": body.send_dm,
        "actor_id": session.discord_user_id,
    }
    return await _call(internal_client.set_event_binding(_GUILD(), event_type, payload))


@router.delete("/events/{event_type}")
async def clear_event(event_type: str, session: Session = Depends(require_permission(_EVENTS))) -> dict:
    _check(event_type, EVENT_TYPES, "invalid_event_type")
    return await _call(internal_client.clear_event_binding(_GUILD(), event_type, session.discord_user_id))


@router.post("/events/{event_type}/test")
async def test_event(event_type: str, session: Session = Depends(require_permission(_EVENTS))) -> dict:
    _check(event_type, EVENT_TYPES, "invalid_event_type")
    return await _call(internal_client.test_event_binding(_GUILD(), event_type, session.discord_user_id))


# ---------------------------------------------------------------------------
# welcome / boost (staff_base, same as the cogs; see module docstring)
# ---------------------------------------------------------------------------

@router.get("/greetings")
async def get_greetings(session: Session = Depends(require_permission("staff_base"))) -> dict:
    return await _call(internal_client.get_greetings(_GUILD()))


class GreetingBody(BaseModel):
    channel_id: str | None = None
    enabled: bool | None = None


@router.put("/greetings/{kind}")
async def update_greeting(kind: str, body: GreetingBody, session: Session = Depends(require_permission("staff_base"))) -> dict:
    _check(kind, GREETING_KINDS, "invalid_kind")
    if body.channel_id is None and body.enabled is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": "Nothing to change."})
    payload = {
        "actor_id": session.discord_user_id,
        "channel_id": _snowflake(body.channel_id, "channel ID") if body.channel_id is not None else None,
        "enabled": body.enabled,
    }
    return await _call(internal_client.update_greeting(_GUILD(), kind, payload))


@router.post("/greetings/{kind}/test")
async def test_greeting(kind: str, session: Session = Depends(require_permission("staff_base"))) -> dict:
    _check(kind, GREETING_KINDS, "invalid_kind")
    return await _call(internal_client.test_greeting(_GUILD(), kind, session.discord_user_id))


# ---------------------------------------------------------------------------
# schedules (manage_schedules, like /schedule)
# ---------------------------------------------------------------------------

_SCHEDULES = "manage_schedules"


@router.get("/schedules")
async def list_schedules(session: Session = Depends(require_permission(_SCHEDULES))) -> dict:
    return await _call(internal_client.list_schedules(_GUILD()))


class CronBody(BaseModel):
    cron: str = Field(max_length=100)


@router.post("/schedules/preview")
async def preview_cron(body: CronBody, session: Session = Depends(require_permission(_SCHEDULES))) -> dict:
    return await _call(internal_client.preview_schedule_cron(_GUILD(), body.cron))


class CreateScheduleBody(BaseModel):
    template_id: int
    channel_id: str
    cron: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=100)


@router.post("/schedules")
async def create_schedule(body: CreateScheduleBody, session: Session = Depends(require_permission(_SCHEDULES))) -> dict:
    payload = {
        "template_id": body.template_id,
        "channel_id": _snowflake(body.channel_id, "channel ID"),
        "cron": body.cron,
        "label": body.label,
        "actor_id": session.discord_user_id,
    }
    return await _call(internal_client.create_schedule(_GUILD(), payload))


class ToggleBody(BaseModel):
    enabled: bool


@router.patch("/schedules/{schedule_id}")
async def toggle_schedule(schedule_id: int, body: ToggleBody, session: Session = Depends(require_permission(_SCHEDULES))) -> dict:
    return await _call(internal_client.toggle_schedule(_GUILD(), schedule_id, body.enabled, session.discord_user_id))


@router.delete("/schedules/{schedule_id}")
async def remove_schedule(schedule_id: int, session: Session = Depends(require_permission(_SCHEDULES))) -> dict:
    return await _call(internal_client.remove_schedule(_GUILD(), schedule_id, session.discord_user_id))


# ---------------------------------------------------------------------------
# autoroles (manage_autorole, like /autorole; self_role_policy runs bot-side)
# ---------------------------------------------------------------------------

_AUTOROLE = "manage_autorole"


@router.get("/autoroles")
async def list_autoroles(session: Session = Depends(require_permission(_AUTOROLE))) -> dict:
    return await _call(internal_client.list_autoroles(_GUILD()))


@router.get("/autoroles/roles")
async def autorole_roles(session: Session = Depends(require_permission(_AUTOROLE))) -> dict:
    return await _call(internal_client.list_autorole_roles(_GUILD(), session.discord_user_id))


class CreateAutoroleBody(BaseModel):
    trigger: str
    role_id: str
    delay_seconds: int = Field(default=0, ge=0, le=7 * 24 * 3600)
    trigger_role_id: str | None = None


@router.post("/autoroles")
async def create_autorole(body: CreateAutoroleBody, session: Session = Depends(require_permission(_AUTOROLE))) -> dict:
    _check(body.trigger, TRIGGERS, "invalid_trigger")
    trigger_role_id = None
    if body.trigger == "role_add":
        if not body.trigger_role_id:
            raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": "Pick the role whose addition triggers this rule."})
        trigger_role_id = _snowflake(body.trigger_role_id, "role ID")
    payload = {
        "trigger": body.trigger,
        "role_id": _snowflake(body.role_id, "role ID"),
        "delay_seconds": body.delay_seconds,
        "trigger_role_id": trigger_role_id,
        "actor_id": session.discord_user_id,
    }
    return await _call(internal_client.create_autorole(_GUILD(), payload))


@router.delete("/autoroles/{rule_id}")
async def remove_autorole(rule_id: int, session: Session = Depends(require_permission(_AUTOROLE))) -> dict:
    return await _call(internal_client.remove_autorole(_GUILD(), rule_id, session.discord_user_id))


# ---------------------------------------------------------------------------
# managed messages (manage_templates, like /managed)
# ---------------------------------------------------------------------------

_MANAGED = "manage_templates"


@router.get("/managed")
async def list_managed(session: Session = Depends(require_permission(_MANAGED))) -> dict:
    return await _call(internal_client.list_managed_messages(_GUILD()))


@router.delete("/managed/{message_id}")
async def untrack_managed(message_id: str, session: Session = Depends(require_permission(_MANAGED))) -> dict:
    return await _call(
        internal_client.untrack_managed_message(_GUILD(), _snowflake(message_id, "message ID"), session.discord_user_id)
    )


class SyncBody(BaseModel):
    template_id: int


@router.post("/managed/sync")
async def sync_managed(body: SyncBody, session: Session = Depends(require_permission(_MANAGED))) -> dict:
    return await _call(internal_client.sync_managed_messages(_GUILD(), body.template_id, session.discord_user_id))
