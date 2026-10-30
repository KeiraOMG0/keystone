"""
FastAPI dependency functions shared across internal API routes.

These resolve live discord.py objects (Bot -> Guild -> Member) from path
parameters, following the constraint that permission checks and other
bot-backed operations need LIVE discord.Member objects, not bare IDs.
"""

from __future__ import annotations

import discord
from discord.ext import commands
from fastapi import Depends, HTTPException, Request, status


def get_bot(request: Request) -> commands.Bot:
    """Returns the live bot instance attached to app.state.bot in main.py."""
    bot = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot instance is not available yet.",
        )
    return bot


def get_guild(guild_id: int, bot: commands.Bot = Depends(get_bot)) -> discord.Guild:
    """Resolves a live discord.Guild the bot is a member of. 404 if not found."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Guild {guild_id} not found (bot is not in that guild or hasn't cached it yet).",
        )
    return guild


async def get_member(user_id: int, guild: discord.Guild = Depends(get_guild)) -> discord.Member:
    """Resolves a live discord.Member: cache first, API fallback if not cached.

    404 if the member is not found (e.g. they left the guild).
    """
    member = guild.get_member(user_id)
    if member is not None:
        return member

    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Member {user_id} not found in guild {guild.id} (not cached and not in guild).",
        )
    except discord.HTTPException as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Discord API error while resolving member {user_id}: {exc}",
        )

    return member


def get_ticket_service(bot: commands.Bot = Depends(get_bot)):
    """Resolves the live TicketService instance from the Tickets cog.

    Unlike permission_service (attached directly to `bot` in main.py),
    TicketService is constructed inside cogs/tickets.py's setup() and only
    reachable via the cog instance — there is no bot.ticket_service
    attribute. 503 if the cog hasn't loaded (e.g. bot.db wasn't available at
    startup — see cogs/tickets.py's setup()).
    """
    cog = bot.get_cog("Tickets")
    if cog is None or not hasattr(cog, "ticket_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tickets cog is not loaded — ticket service unavailable.",
        )
    return cog.ticket_service


def get_reaction_role_service(bot: commands.Bot = Depends(get_bot)):
    """Resolves the live ReactionRoleService instance from the ReactionRoles
    cog. Same rationale/shape as get_ticket_service above: the service is
    constructed inside cogs/reaction_roles.py's ReactionRoleCog.__init__ and
    exposed as cog.reaction_role_service, not a bot.* attribute. 503 if the
    cog hasn't loaded (e.g. bot.db wasn't available at startup)."""
    cog = bot.get_cog("ReactionRoles")
    if cog is None or not hasattr(cog, "reaction_role_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ReactionRoles cog is not loaded — reaction role service unavailable.",
        )
    return cog.reaction_role_service


def get_qotd_cog(bot: commands.Bot = Depends(get_bot)):
    """Resolves the live QotdCog instance (cogs/qotd.py). Same rationale as
    get_ticket_service/get_reaction_role_service above, except QOTD's
    service/repo/audit instances are stored as cog._service / cog._repo /
    cog._audit (underscore-prefixed — an internal convention of that cog, not
    a deliberately private API). We reach into them anyway rather than adding
    public aliases nothing else needs, exactly like this module already
    reaches into bot internals elsewhere. 503 if the cog hasn't loaded (e.g.
    bot.db wasn't available at startup — see cogs/qotd.py's setup())."""
    cog = bot.get_cog("QOTD")
    if cog is None or not hasattr(cog, "_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="QOTD cog is not loaded — QOTD service unavailable.",
        )
    return cog


def get_honeypot_service(bot: commands.Bot = Depends(get_bot)):
    """Resolves the live HoneypotService from the Honeypot cog (cogs/honeypot.py).
    Same shape as get_ticket_service: the service is built inside the cog's
    setup() and exposed as cog.honeypot_service. 503 if the cog hasn't loaded."""
    cog = bot.get_cog("Honeypot")
    if cog is None or not hasattr(cog, "honeypot_service"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Honeypot cog is not loaded - honeypot service unavailable.",
        )
    return cog.honeypot_service
