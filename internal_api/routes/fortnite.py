"""
GET  /guilds/{guild_id}/fortnite/status   config + next post time + cache + wishlist counts
PUT  /guilds/{guild_id}/fortnite/config   partial config update
POST /guilds/{guild_id}/fortnite/post-now post today's shop to the configured channel
                                          (does NOT touch the daily dedupe state)

Thin wrapper over services/fortnite_shop_service.FortniteShopService, the SAME
instance cogs/fortnite_shop.py's /fortnite commands and daily loop use
(bot.get_cog("FortniteShop").shop_service). Authorization happens in the
dashboard backend (server/routes/fortnite.py, perm `manage_schedules`).
"""

from __future__ import annotations

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from internal_api.snowflakes import stringify_snowflakes
from services.fortnite_shop_service import MANUAL_FETCH_ATTEMPTS, FortniteShopError

router = APIRouter(dependencies=[Depends(require_internal_secret)])


def get_fortnite_service(bot: commands.Bot = Depends(get_bot)):
    """The live FortniteShopService (503 if cogs.fortnite_shop is not loaded)."""
    cog = bot.get_cog("FortniteShop")
    if cog is None or not hasattr(cog, "shop_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FortniteShop cog is not loaded - fortnite shop service unavailable.",
        )
    return cog.shop_service


@router.get("/guilds/{guild_id}/fortnite/status")
async def fortnite_status(
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_fortnite_service),
) -> dict:
    return stringify_snowflakes(await service.status(guild.id))


class FortniteConfigUpdate(BaseModel):
    channel_id: int | None = None
    clear_channel: bool = False
    enabled: bool | None = None
    include_jam_tracks: bool | None = None
    ping_role_id: int | None = None
    clear_ping_role: bool = False


@router.put("/guilds/{guild_id}/fortnite/config")
async def update_fortnite_config(
    body: FortniteConfigUpdate,
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_fortnite_service),
) -> dict:
    changes: dict = {}
    if body.clear_channel:
        changes["channel_id"] = None
    elif body.channel_id is not None:
        if not isinstance(guild.get_channel(body.channel_id), discord.TextChannel):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="channel_not_found")
        changes["channel_id"] = body.channel_id
    if body.clear_ping_role:
        changes["ping_role_id"] = None
    elif body.ping_role_id is not None:
        role = guild.get_role(body.ping_role_id)
        if role is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="role_not_found")
        if role.is_default() or role.managed:
            # @everyone (id == guild id) renders as a broken <@&id> mention;
            # managed bot/integration roles are not meaningful ping targets.
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="role_not_pingable")
        changes["ping_role_id"] = body.ping_role_id
    if body.enabled is not None:
        changes["enabled"] = body.enabled
    if body.include_jam_tracks is not None:
        changes["include_jam_tracks"] = body.include_jam_tracks
    await service.repo.update_config(guild.id, **changes)
    return stringify_snowflakes(await service.status(guild.id))


@router.post("/guilds/{guild_id}/fortnite/post-now")
async def fortnite_post_now(
    guild: discord.Guild = Depends(get_guild),
    service=Depends(get_fortnite_service),
) -> dict:
    cfg = await service.repo.get_config(guild.id)
    channel = guild.get_channel(cfg["channel_id"]) if cfg and cfg.get("channel_id") else None
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="channel_not_set")
    try:
        shop = await service.get_shop(attempts=MANUAL_FETCH_ATTEMPTS)  # ~30s worst case, see internal_client
        msgs = await service.post_shop(channel, shop, include_jam_tracks=bool(cfg.get("include_jam_tracks")))
    except FortniteShopError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"shop_unavailable: {exc}")
    except discord.Forbidden:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="discord_forbidden")
    except discord.HTTPException as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"discord_error: {exc}")
    return stringify_snowflakes(
        {
            "shop_date": shop.shop_date,
            "channel_id": channel.id,
            "message_id": msgs[0].id if msgs else None,
            "jump_url": msgs[0].jump_url if msgs else None,
            "message_count": len(msgs),
        }
    )
