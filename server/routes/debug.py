"""
GET  /api/debug/actions            test-action catalogue (live options) + skipped features
POST /api/debug/run                {action, channel_id, params} -> result (message link)
GET  /api/debug/fortnite/preview   parsed Fortnite shop JSON (?refresh=true forces a fetch)
GET  /api/debug/health             latency, uptime, loaded/failed cogs, task loops
GET  /api/debug/channels           text-channel picker
GET  /api/debug/appeal-test        honeypot appeal-flow test cases
POST /api/debug/appeal-test        {user_id, state: eligible|pending|approved} -> test case (no real ban)
POST /api/debug/appeal-test/clear  delete every test case
GET  /api/debug/lab/options        Lab pickers: roles, templates, event bindings, anti-spam rules
POST /api/debug/lab/permissions    {user_id} | {role_ids: [...]} -> permission simulation
POST /api/debug/lab/preview        {kind, user_id, event_type?, role_id?, template?, send_channel_id?}
POST /api/debug/lab/antispam       {overrides?, messages: [...]} -> anti-spam verdicts (pure)
GET  /api/debug/lab/latency        time read-only internal API GETs from this process
GET  /api/debug/errors             Errors tab: browser + website-server reports (stored) and bot errors (in memory)
POST /api/debug/errors/clear       {source?: all|browser|server|bot, id?: fingerprint} -> how many removed

The HIDDEN owner-only Debug page. Every route is gated by require_owner
(server/permissions/guards.py): the session's Discord user ID must equal
OWNER_ID from the root .env, and EVERYONE else (including staff and logged-out
visitors) gets 404 {"error": "not_found"}, so the section does not appear to
exist. The owner id sent to the bot is always taken from the session, never
from the request body. Thin client of internal_api/routes/debug.py.

Nothing may answer differently for a non-owner than an unknown /api path:
  * require_owner is a ROUTER-level dependency (runs before any query/body
    validation, and covers every route added here later);
  * POST bodies are read raw and validated only AFTER require_owner passed
    (FastAPI parses a declared JSON body before any dependency, so a malformed
    body would otherwise 422 for everyone);
  * wrong methods / trailing slashes fall through to server/app.py's /api
    catch-all, which returns the same bare 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from server import config
from server.db.models import Session
from server.permissions.guards import owner_body, require_owner
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/debug", tags=["debug"], dependencies=[Depends(require_owner)])

_SAFE_DETAILS = {"unknown_action", "channel_not_found", "owner_not_in_guild"}


def _error(exc: InternalAPIError) -> HTTPException:
    detail = exc.detail if isinstance(exc.detail, str) else None
    if exc.status_code == 400 and detail in _SAFE_DETAILS:
        return HTTPException(status_code=400, detail={"error": detail})
    if exc.status_code == 404:
        return HTTPException(status_code=404, detail={"error": "not_found"})
    if exc.status_code == 502 and detail and detail.startswith("shop_unavailable"):
        # Owner-only page: the upstream reason is useful here.
        return HTTPException(status_code=502, detail={"error": "shop_unavailable", "message": detail})
    if exc.status_code == 503:
        return HTTPException(status_code=503, detail={"error": "feature_unavailable"})
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


@router.get("/actions")
async def actions(session: Session = Depends(require_owner)) -> dict:
    try:
        return await internal_client.get_debug_actions(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _error(exc)


class RunBody(BaseModel):
    action: str = Field(..., min_length=1, max_length=64)
    channel_id: str = Field(..., min_length=1, max_length=25)  # snowflake as a string
    params: dict = Field(default_factory=dict)


@router.post("/run")
async def run(request: Request, session: Session = Depends(require_owner)) -> dict:
    body = await owner_body(request, RunBody)
    assert isinstance(body, RunBody)
    if not body.channel_id.isdigit():
        raise HTTPException(status_code=400, detail={"error": "invalid_request"})
    try:
        return await internal_client.run_debug_action(
            config.DASHBOARD_GUILD_ID,
            action=body.action,
            channel_id=int(body.channel_id),
            owner_id=int(session.discord_user_id),
            params=body.params,
        )
    except InternalAPIError as exc:
        raise _error(exc)


@router.get("/fortnite/preview")
async def fortnite_preview(refresh: bool = False, session: Session = Depends(require_owner)) -> dict:
    try:
        return await internal_client.get_debug_fortnite_preview(config.DASHBOARD_GUILD_ID, refresh=refresh)
    except InternalAPIError as exc:
        raise _error(exc)


@router.get("/health")
async def health(session: Session = Depends(require_owner)) -> dict:
    try:
        return await internal_client.get_debug_health(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _error(exc)


@router.get("/channels")
async def channels(session: Session = Depends(require_owner)) -> list[dict]:
    try:
        return await internal_client.list_guild_channels(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _error(exc)


# ---------------------------------------------------------------------------
# Honeypot appeal-flow test cases (migration 031). Unlike the test actions above,
# these WRITE honeypot rows (tagged is_test, removable with /appeal-test/clear) so
# the owner can try the real appeal sign-in, review and rejoin with an alt account.
# Nobody is banned and nothing is posted to Discord.
# ---------------------------------------------------------------------------

class AppealTestBody(BaseModel):
    user_id: str = Field(..., min_length=15, max_length=22)  # snowflake as a string
    state: str = Field(..., pattern="^(eligible|pending|approved)$")


def _appeal_test_error(exc: InternalAPIError) -> HTTPException:
    # Owner-only: the bot's 400 text ("That account has real honeypot records ...") is useful here.
    if exc.status_code == 400 and isinstance(exc.detail, str):
        return HTTPException(status_code=400, detail={"error": "invalid_request", "message": exc.detail[:300]})
    return _error(exc)


@router.get("/appeal-test")
async def appeal_test_list(session: Session = Depends(require_owner)) -> dict:
    try:
        return await internal_client.list_appeal_test_cases(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _error(exc)


@router.post("/appeal-test")
async def appeal_test_create(request: Request, session: Session = Depends(require_owner)) -> dict:
    body = await owner_body(request, AppealTestBody)
    assert isinstance(body, AppealTestBody)
    if not body.user_id.isdigit():
        raise HTTPException(status_code=400, detail={"error": "invalid_request"})
    try:
        return await internal_client.create_appeal_test_case(
            config.DASHBOARD_GUILD_ID, owner_id=int(session.discord_user_id),
            user_id=int(body.user_id), state=body.state,
        )
    except InternalAPIError as exc:
        raise _appeal_test_error(exc)


@router.post("/appeal-test/clear")
async def appeal_test_clear(session: Session = Depends(require_owner)) -> dict:
    try:
        return await internal_client.clear_appeal_test_cases(
            config.DASHBOARD_GUILD_ID, owner_id=int(session.discord_user_id),
        )
    except InternalAPIError as exc:
        raise _error(exc)


# ---------------------------------------------------------------------------
# Lab tab (services/debug_lab_service.py via internal_api/routes/debug.py).
# Side-effect free: the only Discord output is an optional SafeChannel copy of
# a preview into the channel the owner picked. Bodies are read with owner_body
# (after require_owner), like every other POST here.
# ---------------------------------------------------------------------------

def _lab_error(exc: InternalAPIError) -> HTTPException:
    # Owner-only: the bot's own message ("No message is bound to boost.") is useful here.
    detail = exc.detail if isinstance(exc.detail, dict) else None
    if exc.status_code == 400 and detail and detail.get("error") == "lab_error":
        return HTTPException(status_code=400, detail={"error": "lab_error", "message": str(detail.get("message") or "")[:300]})
    if exc.status_code == 422:
        return HTTPException(status_code=400, detail={"error": "invalid_request"})
    return _error(exc)


def _snowflake(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    s = str(value).strip()
    if not s.isdigit() or not 15 <= len(s) <= 21:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": "Not a Discord ID."})
    return int(s)


@router.get("/lab/options")
async def lab_options(session: Session = Depends(require_owner)) -> dict:
    try:
        return await internal_client.get_debug_lab_options(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _lab_error(exc)


class LabPermissionsBody(BaseModel):
    user_id: str | None = Field(default=None, max_length=25)
    role_ids: list[str] | None = Field(default=None, max_length=250)


@router.post("/lab/permissions")
async def lab_permissions(request: Request, session: Session = Depends(require_owner)) -> dict:
    body = await owner_body(request, LabPermissionsBody)
    assert isinstance(body, LabPermissionsBody)
    user_id = _snowflake(body.user_id)
    role_ids = [rid for rid in (_snowflake(r) for r in (body.role_ids or [])) if rid is not None]
    if user_id is None and body.role_ids is None:
        raise HTTPException(status_code=400, detail={"error": "invalid_request", "message": "Pick a member or roles."})
    try:
        return await internal_client.debug_lab_permissions(
            config.DASHBOARD_GUILD_ID, owner_id=int(session.discord_user_id),
            user_id=user_id, role_ids=None if user_id is not None else role_ids,
        )
    except InternalAPIError as exc:
        raise _lab_error(exc)


class LabPreviewBody(BaseModel):
    kind: str = Field(..., pattern="^(welcome|boost|event|template)$")
    user_id: str = Field(..., min_length=15, max_length=22)
    event_type: str | None = Field(default=None, max_length=32)
    role_id: str | None = Field(default=None, max_length=25)
    template: str | None = Field(default=None, max_length=100)
    send_channel_id: str | None = Field(default=None, max_length=25)


@router.post("/lab/preview")
async def lab_preview(request: Request, session: Session = Depends(require_owner)) -> dict:
    body = await owner_body(request, LabPreviewBody)
    assert isinstance(body, LabPreviewBody)
    payload = {
        "kind": body.kind,
        "user_id": _snowflake(body.user_id),
        "event_type": body.event_type,
        "role_id": _snowflake(body.role_id),
        "template": body.template,
        "send_channel_id": _snowflake(body.send_channel_id),
    }
    try:
        return await internal_client.debug_lab_preview(
            config.DASHBOARD_GUILD_ID, owner_id=int(session.discord_user_id), payload=payload,
        )
    except InternalAPIError as exc:
        raise _lab_error(exc)


class LabAntispamMessage(BaseModel):
    at: float = Field(..., ge=0, le=86_400)
    user: str | None = Field(default=None, max_length=32)
    channel: str | None = Field(default=None, max_length=64)
    content: str | None = Field(default=None, max_length=4000)
    mentions: int | None = Field(default=None, ge=0, le=1000)


class LabAntispamBody(BaseModel):
    overrides: dict[str, int | bool | None] = Field(default_factory=dict)
    messages: list[LabAntispamMessage] = Field(..., min_length=1, max_length=200)


@router.post("/lab/antispam")
async def lab_antispam(request: Request, session: Session = Depends(require_owner)) -> dict:
    body = await owner_body(request, LabAntispamBody)
    assert isinstance(body, LabAntispamBody)
    try:
        return await internal_client.debug_lab_antispam(
            config.DASHBOARD_GUILD_ID, owner_id=int(session.discord_user_id),
            overrides=dict(body.overrides), messages=[m.model_dump() for m in body.messages],
        )
    except InternalAPIError as exc:
        raise _lab_error(exc)


# Read-only internal GET routes timed by the latency matrix. Fixed list (no
# user-supplied paths); nothing here writes, posts or starts heavy work
# (Diagnostics' dump is left out on purpose).
LATENCY_PROBES: list[tuple[str, str]] = [
    ("Health (no auth)", "/health"),
    ("Live stats", "/guilds/{g}/stats/live"),
    ("Debug health", "/guilds/{g}/debug/health"),
    ("Roles", "/guilds/{g}/roles"),
    ("Channels", "/guilds/{g}/channels"),
    ("Categories", "/guilds/{g}/categories"),
    ("Permission grants", "/guilds/{g}/permission-grants"),
    ("Settings", "/guilds/{g}/settings"),
    ("Templates", "/guilds/{g}/templates"),
    ("Ticket panels", "/guilds/{g}/ticket-panels"),
    ("Open tickets", "/guilds/{g}/tickets?status_filter=open"),
    ("Recent warnings", "/guilds/{g}/moderation/warnings/recent?limit=1"),
    ("Honeypot config", "/guilds/{g}/honeypot/config"),
    ("Honeypot stats", "/guilds/{g}/honeypot/stats"),
    ("Audit (1 row)", "/guilds/{g}/audit?limit=1"),
    ("Audit actions", "/guilds/{g}/audit/actions"),
    ("QOTD config", "/guilds/{g}/qotd/config"),
    ("Self-role panels", "/guilds/{g}/reaction-roles/panels"),
    ("Role admin overview", "/guilds/{g}/role-admin/roles"),
    ("Fortnite status", "/guilds/{g}/fortnite/status"),
    ("Lab options", "/guilds/{g}/debug/lab/options"),
]


@router.get("/lab/latency")
async def lab_latency(session: Session = Depends(require_owner)) -> dict:
    """Times each probe sequentially (so one slow route doesn't skew the
    others) from the dashboard process: the real dashboard -> bot round trip."""
    import time

    started = time.perf_counter()
    results = []
    for name, template in LATENCY_PROBES:
        path = template.replace("{g}", str(config.DASHBOARD_GUILD_ID))
        res = await internal_client.probe_get(path)
        results.append({"name": name, "path": template.replace("{g}", "{guild}"), **res})
    return {"results": results, "total_ms": round((time.perf_counter() - started) * 1000, 1)}


# --------------------------------------------------------------------------
# Errors tab: one list of every error the site and bot have seen.
#   browser / server -> server/services/error_reports.py (dashboard.db, 30 days)
#   bot              -> the bot's in-memory ring (core/error_ring.py), since its
#                       last start; best effort, bot_available=false when down.
# --------------------------------------------------------------------------

_ERROR_SOURCES = ("all", "browser", "server", "bot")


class ErrorsClearBody(BaseModel):
    source: str = Field("all", max_length=16)
    id: str | None = Field(None, max_length=32)


@router.get("/errors")
async def errors_list(session: Session = Depends(require_owner)) -> dict:
    from server.routes.diagnostics import scrub
    from server.services import error_reports

    groups = await error_reports.list_groups()
    bot: dict = {"available": False, "started_at": None}
    try:
        res = await internal_client.get_debug_errors(config.DASHBOARD_GUILD_ID)
        groups.extend(res.get("groups") or [])
        bot = {"available": True, "started_at": res.get("started_at")}
    except InternalAPIError:
        pass
    groups.sort(key=lambda g: g.get("last_seen") or "", reverse=True)
    return {
        "groups": scrub(groups),
        "bot": bot,
        "retention_days": error_reports.ERROR_REPORT_RETENTION_DAYS,
    }


@router.post("/errors/clear")
async def errors_clear(request: Request, session: Session = Depends(require_owner)) -> dict:
    from server.services import error_reports

    body = await owner_body(request, ErrorsClearBody)
    assert isinstance(body, ErrorsClearBody)
    if body.source not in _ERROR_SOURCES:
        raise HTTPException(status_code=400, detail={"error": "invalid_source"})
    removed = 0
    if body.source in ("all", "browser", "server"):
        removed += await error_reports.clear(
            source=None if body.source == "all" else body.source, fingerprint_id=body.id
        )
    bot_error = None
    if body.source in ("all", "bot"):
        try:
            res = await internal_client.clear_debug_errors(config.DASHBOARD_GUILD_ID, fingerprint=body.id)
            removed += int(res.get("removed") or 0)
        except InternalAPIError:
            bot_error = "internal_api_unavailable"
    return {"ok": True, "removed": removed, "bot_error": bot_error}
