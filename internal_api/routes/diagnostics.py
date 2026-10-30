"""
GET  /guilds/{guild_id}/diagnostics
POST /guilds/{guild_id}/diagnostics/cache/clear   {"requested_by"?: str}

Full bot-process diagnostics dump for the STAFF-ONLY dashboard Diagnostics
page (server/routes/diagnostics.py, gated by require_staff). All collection
logic lives in services/diagnostics_service.py so it is unit-testable; this
file is a thin wrapper.

Like every internal_api route this trusts any caller holding the shared
internal secret (require_internal_secret); the staff check happens one layer
up. Each section of the dump is isolated: a failing section yields
{"error": "..."} instead of failing the request. Hard-line secrets (bot token,
*_SECRET/*_TOKEN/*_PASSWORD values, raw cookie values) are never included -
see the service module docstring.

`cache/clear` empties every diagnostics cache (the TTL cache, node version,
log tails) via diagnostics_service.clear_caches(), so the next collection
(the live producer's next tick or the next GET) recomputes everything. It is
read-only with respect to bot state, so it only logs (no audit_log row).
"""

from __future__ import annotations

import logging

from discord.ext import commands
from fastapi import APIRouter, Body, Depends, Request

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot
from services import diagnostics_service

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_internal_secret)])


@router.get("/guilds/{guild_id}/diagnostics")
async def get_diagnostics(
    guild_id: int,
    request: Request,
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    return await diagnostics_service.collect_diagnostics(bot, guild_id, app=request.app)


@router.post("/guilds/{guild_id}/diagnostics/cache/clear")
async def clear_diagnostics_cache(guild_id: int, body: dict | None = Body(default=None)) -> dict:
    requested_by = " ".join(str((body or {}).get("requested_by") or "unknown").split())[:120]
    result = diagnostics_service.clear_caches()
    log.info(
        "diagnostics caches cleared by %s (%d entries, log cache %s)",
        requested_by, result["entries"], "cleared" if result["log_cache_cleared"] else "not cleared",
    )
    return result
