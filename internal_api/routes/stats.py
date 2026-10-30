"""
GET /guilds/{guild_id}/stats/live

Returns live member-presence counts pulled straight from discord.py's guild
cache (no DB query, no Discord API call — just the in-memory cache the
gateway already maintains).

Presence counts (online/idle/dnd) require the Presence Intent. That intent
must be enabled both in code (intents.presences = True, see main.py) AND in
the Discord Developer Portal for this bot application. If the portal-side
toggle isn't on, discord.py will connect fine but every member's `.status`
will report as `offline`/unknown rather than their real presence, and the
counts below will be meaningless.

To avoid crashing or returning silently-wrong data in that situation, this
route degrades gracefully: it always returns `total`, and only fills in
online/idle/dnd/offline breakdowns when it detects presence data actually
looks populated (i.e. intents.presences is True). If presence data isn't
available, the breakdown fields are `null` and `presence_available` is False.
"""

from __future__ import annotations

import discord
from discord.ext import commands
from fastapi import APIRouter, Depends

from internal_api.auth import require_internal_secret
from internal_api.deps import get_bot, get_guild
from internal_api.snowflakes import stringify_snowflakes

router = APIRouter(dependencies=[Depends(require_internal_secret)])


@router.get("/guilds/{guild_id}/stats/live")
async def get_live_stats(
    guild: discord.Guild = Depends(get_guild),
    bot: commands.Bot = Depends(get_bot),
) -> dict:
    return build_live_stats(guild, bot)


def build_live_stats(guild: discord.Guild, bot: commands.Bot) -> dict:
    """Shared by the HTTP route above and the live `stats` websocket topic."""
    total = guild.member_count if guild.member_count is not None else len(guild.members)

    presences_enabled = bool(bot.intents.presences)

    if not presences_enabled:
        return stringify_snowflakes(
            {
                "guild_id": guild.id,
                "total": total,
                "online": None,
                "idle": None,
                "dnd": None,
                "offline": None,
                "presence_available": False,
                "note": (
                    "Presence intent is not enabled, so status breakdown is unavailable. "
                    "Enable intents.presences in main.py and the Presence Intent toggle "
                    "in the Discord Developer Portal to get online/idle/dnd counts."
                ),
            }
        )

    online = idle = dnd = offline = 0
    for member in guild.members:
        status = member.status
        if status is discord.Status.online:
            online += 1
        elif status is discord.Status.idle:
            idle += 1
        elif status is discord.Status.dnd:
            dnd += 1
        else:
            offline += 1

    return stringify_snowflakes(
        {
            "guild_id": guild.id,
            "total": total,
            "online": online,
            "idle": idle,
            "dnd": dnd,
            "offline": offline,
            "presence_available": True,
        }
    )
