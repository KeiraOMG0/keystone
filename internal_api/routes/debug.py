"""
GET  /guilds/{guild_id}/debug/actions            test-action catalogue (+ live param options) and skipped features
POST /guilds/{guild_id}/debug/run                {action, channel_id, owner_id, params} -> result
GET  /guilds/{guild_id}/debug/fortnite/preview   parsed Fortnite shop (JSON viewer), ?refresh=true forces a fetch
GET  /guilds/{guild_id}/debug/health             latency, uptime, loaded/failed cogs, task loops
GET  /guilds/{guild_id}/debug/appeal-test        honeypot appeal-flow test cases
POST /guilds/{guild_id}/debug/appeal-test        {owner_id, user_id, state: eligible|pending|approved}
POST /guilds/{guild_id}/debug/appeal-test/clear  {owner_id} -> {catches, appeals} deleted
GET  /guilds/{guild_id}/debug/lab/options        Lab pickers (see the Lab section at the bottom)
POST /guilds/{guild_id}/debug/lab/permissions    Lab permission simulator
POST /guilds/{guild_id}/debug/lab/preview        Lab message preview (+ optional SafeChannel copy)
POST /guilds/{guild_id}/debug/lab/antispam       Lab anti-spam tester (pure)
GET  /guilds/{guild_id}/debug/errors             the bot's in-memory error ring (core/error_ring.py), scrubbed
POST /guilds/{guild_id}/debug/errors/clear       {fingerprint?} -> {removed}

Backs the hidden OWNER-ONLY dashboard Debug page (server/routes/debug.py,
require_owner -> 404 for anyone else). Like every internal route it trusts
the shared secret; as defence in depth /debug/run additionally requires
owner_id == config.OWNER_ID (404 otherwise) because it posts to Discord.
All action logic and its safety rules live in services/debug_actions_service.py.
"""

from __future__ import annotations

import asyncio

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

import config
from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild, get_honeypot_service
from internal_api.routes.fortnite import get_fortnite_service
from internal_api.snowflakes import stringify_snowflakes
from services import debug_actions_service as dbg
from services.fortnite_shop_service import MANUAL_FETCH_ATTEMPTS, FortniteShopError
from services.honeypot_service import HoneypotError
from services.psutil_guard import serialized

router = APIRouter(dependencies=[Depends(require_internal_secret)])


@router.get("/guilds/{guild_id}/debug/actions")
async def debug_actions(
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    return await dbg.catalogue(bot, guild)


class DebugRunRequest(BaseModel):
    action: str = Field(..., max_length=64)
    channel_id: int
    owner_id: int
    params: dict = Field(default_factory=dict)


@router.post("/guilds/{guild_id}/debug/run")
async def debug_run(
    body: DebugRunRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    if not config.OWNER_ID or body.owner_id != config.OWNER_ID:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    if body.action not in dbg.ACTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unknown_action")
    channel = guild.get_channel(body.channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="channel_not_found")
    owner = guild.get_member(body.owner_id)
    if owner is None:
        try:
            owner = await guild.fetch_member(body.owner_id)
        except discord.HTTPException:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="owner_not_in_guild")
    result = await dbg.run_action(bot, guild, channel, owner, body.action, body.params)
    result["message_ids"] = [str(m) for m in result.get("message_ids", [])]
    return stringify_snowflakes(result)


class AppealTestRequest(BaseModel):
    owner_id: int
    user_id: int
    state: str = Field(..., max_length=16)


class OwnerRequest(BaseModel):
    owner_id: int


def _require_owner_id(owner_id: int) -> None:
    if not config.OWNER_ID or owner_id != config.OWNER_ID:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


@router.get("/guilds/{guild_id}/debug/appeal-test")
async def appeal_test_list(guild_id: int, service=Depends(get_honeypot_service)) -> dict:
    """Debug-page appeal-flow test cases (migration 031)."""
    return stringify_snowflakes({"cases": await service.list_test_cases(guild_id)})


@router.post("/guilds/{guild_id}/debug/appeal-test")
async def appeal_test_create(
    body: AppealTestRequest,
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_honeypot_service),
) -> dict:
    """Records a TEST honeypot ban (+ appeal) for an alt: nobody is banned, nothing is sent.
    Writes feature state on purpose (unlike the SafeChannel test actions), tagged is_test."""
    _require_owner_id(body.owner_id)
    try:
        res = await service.create_test_case(guild, body.user_id, body.state, body.owner_id)
    except HoneypotError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return stringify_snowflakes(res)


@router.post("/guilds/{guild_id}/debug/appeal-test/clear")
async def appeal_test_clear(guild_id: int, body: OwnerRequest, service=Depends(get_honeypot_service)) -> dict:
    _require_owner_id(body.owner_id)
    return await service.delete_test_cases(guild_id, body.owner_id)


@router.get("/guilds/{guild_id}/debug/fortnite/preview")
async def debug_fortnite_preview(
    refresh: bool = False,
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_fortnite_service),
) -> dict:
    try:
        shop = await service.get_shop(force=refresh, attempts=MANUAL_FETCH_ATTEMPTS)
    except FortniteShopError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"shop_unavailable: {exc}")
    return shop.to_dict()


@serialized  # shared psutil lock (services/psutil_guard.py)
def _process_snapshot() -> dict:
    try:
        from services import diagnostics_service as diag

        basic = diag.proc_basic(diag.get_proc())
        return {k: basic.get(k) for k in ("pid", "started_at", "uptime_seconds", "cpu_percent", "rss_bytes", "threads")}
    except Exception as exc:  # psutil missing or access denied: the panel just shows less
        return {"error": f"{type(exc).__name__}: {exc}"}


@router.get("/guilds/{guild_id}/debug/health")
async def debug_health(
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    from services import diagnostics_service as diag

    expected = list(getattr(bot, "expected_cogs", []) or [])
    errors = dict(getattr(bot, "cog_load_errors", {}) or {})
    loaded_ext = sorted(bot.extensions)
    failed = [{"extension": e, "error": errors.get(e) or "not loaded"} for e in expected if e not in bot.extensions]
    latency = getattr(bot, "latency", None)
    collector = getattr(bot, "diagnostics_collector", None)
    return {
        "ready": bool(bot.is_ready()),
        "latency_ms": round(latency * 1000, 1) if isinstance(latency, float) and latency == latency and latency != float("inf") else None,
        "process": await asyncio.to_thread(_process_snapshot),
        # epoch seconds when cogs.diagnostics_collector loaded (~ bot start); None if not loaded
        "cogs_loaded_at": getattr(collector, "started_at", None),
        "guild": {"id": str(guild.id), "name": guild.name, "members": guild.member_count},
        "cogs": sorted(bot.cogs),
        "extensions_loaded": loaded_ext,
        "extensions_expected": expected,
        "extensions_failed": failed,
        "loops": diag.loops_of(bot),
    }


# ---------------------------------------------------------------------------
# Lab tab (services/debug_lab_service.py). Every route is read-only except
# the optional "send a copy" of a preview, which goes through SafeChannel.
# POSTs carry owner_id and are checked against OWNER_ID like /debug/run.
#   GET  /guilds/{guild_id}/debug/lab/options       roles, templates, event bindings, anti-spam rules
#   POST /guilds/{guild_id}/debug/lab/permissions   {owner_id, user_id? | role_ids?} -> simulation
#   POST /guilds/{guild_id}/debug/lab/preview       {owner_id, kind, user_id, event_type?, role_id?,
#                                                    template?, send_channel_id?} -> rendered (+ sent)
#   POST /guilds/{guild_id}/debug/lab/antispam      {owner_id, overrides?, messages} -> verdicts
# Errors: 400 {"error": "lab_error", "message"} for a bad request the owner can fix.
# ---------------------------------------------------------------------------

from services import debug_lab_service as lab  # noqa: E402


def _lab_error(exc: lab.LabError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                         detail={"error": "lab_error", "message": str(exc)[:300]})


async def _lab_member(guild: discord.Guild, user_id: int) -> discord.Member:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            raise _lab_error(lab.LabError("That user is not in the server."))
    return member


@router.get("/guilds/{guild_id}/debug/lab/options")
async def lab_options(guild: discord.Guild = Depends(get_guild), bot: commands.Bot = Depends(get_bot)) -> dict:
    return await lab.lab_options(bot, guild)


class LabPermissionsRequest(BaseModel):
    owner_id: int
    user_id: int | None = None
    role_ids: list[int] | None = Field(default=None, max_length=250)


@router.post("/guilds/{guild_id}/debug/lab/permissions")
async def lab_permissions(
    body: LabPermissionsRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    _require_owner_id(body.owner_id)
    try:
        if body.user_id is not None:
            member = await _lab_member(guild, body.user_id)
            return await lab.simulate_permissions(bot, guild, member=member)
        return await lab.simulate_permissions(bot, guild, role_ids=body.role_ids or [])
    except lab.LabError as exc:
        raise _lab_error(exc)


class LabPreviewRequest(BaseModel):
    owner_id: int
    kind: str = Field(..., max_length=16)
    user_id: int
    event_type: str | None = Field(default=None, max_length=32)
    role_id: int | None = None
    template: str | None = Field(default=None, max_length=100)
    send_channel_id: int | None = None


@router.post("/guilds/{guild_id}/debug/lab/preview")
async def lab_preview(
    body: LabPreviewRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    _require_owner_id(body.owner_id)
    target = None
    if body.send_channel_id is not None:
        target = guild.get_channel(body.send_channel_id)
        if not isinstance(target, discord.TextChannel):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="channel_not_found")
    role = guild.get_role(body.role_id) if body.role_id else None
    if body.role_id and role is None:
        raise _lab_error(lab.LabError("No role with that ID in the server."))
    try:
        member = await _lab_member(guild, body.user_id)
        preview = await lab.render_preview(
            bot, guild, member, body.kind, event_type=body.event_type, role=role, template=body.template,
        )
    except lab.LabError as exc:
        raise _lab_error(exc)
    out = lab.to_json(preview)
    out["sent"] = None
    if target is not None:
        if not preview["messages"]:
            raise _lab_error(lab.LabError("Nothing to send: it rendered empty."))
        try:
            out["sent"] = await lab.send_preview(preview, target, body.owner_id)
        except discord.Forbidden:
            raise _lab_error(lab.LabError(f"Missing permissions to post in #{target.name}."))
        except discord.HTTPException as exc:
            raise _lab_error(lab.LabError(f"Discord error: {exc}"))
    return stringify_snowflakes(out)


class LabAntispamRequest(BaseModel):
    owner_id: int
    overrides: dict = Field(default_factory=dict)
    messages: list[dict] = Field(default_factory=list, max_length=lab.MAX_SAMPLE_MESSAGES)


@router.post("/guilds/{guild_id}/debug/lab/antispam")
async def lab_antispam(
    body: LabAntispamRequest,
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    _require_owner_id(body.owner_id)
    try:
        rules = await lab.effective_spam_rules(bot, guild.id, body.overrides)
        return lab.simulate_antispam(rules, body.messages)
    except lab.LabError as exc:
        raise _lab_error(exc)


# --------------------------------------------------------------------------
# Errors tab (owner-only Debug page): ERROR+ log records since the bot started.
# --------------------------------------------------------------------------

def _scrub_error_group(group: dict) -> dict:
    from services.diagnostics_service import scrub_log_line, scrub_payload

    def text(value):
        if not isinstance(value, str):
            return value
        return "\n".join(scrub_log_line(line) for line in value.split("\n"))

    group = scrub_payload(group)
    group["message"] = text(group.get("message"))
    sample = dict(group.get("sample") or {})
    sample["stack"] = text(sample.get("stack"))
    group["sample"] = sample
    return group


@router.get("/guilds/{guild_id}/debug/errors")
async def debug_errors(guild: discord.Guild = Depends(get_guild)) -> dict:
    from core.error_ring import RING, _iso

    return {"started_at": _iso(RING.started_at), "groups": [_scrub_error_group(g) for g in RING.snapshot()]}


class DebugErrorsClearRequest(BaseModel):
    fingerprint: str | None = Field(None, max_length=32)


@router.post("/guilds/{guild_id}/debug/errors/clear")
async def debug_errors_clear(body: DebugErrorsClearRequest, guild: discord.Guild = Depends(get_guild)) -> dict:
    from core.error_ring import RING

    return {"removed": RING.clear(body.fingerprint)}
