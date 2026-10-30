"""
services/member_profile_service.py - the dashboard member profile (a richer
`/userinfo`): who the user is, their membership state, warnings, unified
moderation history and honeypot record, in one response.

Works for users who are NOT in the server (left, kicked, banned): the account
facts then come from bot.get_user / fetch_user (created_at is always derived
from the snowflake, so it never needs an API call), `ban` says whether they are
banned (guild.fetch_ban; None when the bot may not read bans), and `status`
is "member" | "banned" | "kicked" (last kick/ban/unban in mod_actions was a
kick) | "not_member" (left, or never joined).

`actor_block_reason` is the rank/identity verdict (services/hierarchy via
moderation_service.member_block_reason) for the VIEWER acting on this user, so
the page can explain a disabled kick/ban/timeout up front. It is advisory:
every action re-checks with check_hierarchy=True when it is taken.

The honeypot section is only built when the caller asks for it (the dashboard
includes it only for viewers with `moderate_members`, the Honeypot page's
gate) and never contains Debug-page test cases (is_test = 1).
"""

from __future__ import annotations

from typing import Any

import discord

import config as _config
from services.honeypot_service import appeal_to_dict, catch_to_dict
from services.moderation_service import member_block_reason

HONEYPOT_CATCH_LIMIT = 20
HISTORY_LIMIT = 100


def _iso(value) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


async def find_member(guild, user_id: int):
    """Live member (cache, then fetch_member), or None if not in the guild.
    Other Discord errors propagate (the route maps them)."""
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


async def lookup_user(bot, user_id: int):
    """discord.User for someone who is not a member (best effort: None when
    Discord does not know the id or cannot be asked right now)."""
    user = bot.get_user(user_id) if hasattr(bot, "get_user") else None
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except discord.HTTPException:
        return None


async def lookup_ban(guild, user_id: int) -> dict:
    """{"banned": True/False/None, "reason": str|None}; None = unknown (the
    bot lacks Ban Members or Discord failed)."""
    try:
        ban = await guild.fetch_ban(discord.Object(id=user_id))
    except discord.NotFound:
        return {"banned": False, "reason": None}
    except discord.HTTPException:
        return {"banned": None, "reason": None}
    return {"banned": True, "reason": getattr(ban, "reason", None)}


def serialize_user(user, user_id: int) -> dict:
    created = discord.utils.snowflake_time(user_id)
    if user is None:
        return {
            "id": str(user_id), "username": None, "global_name": None, "display_name": None,
            "avatar_url": None, "bot": False, "created_at": created.isoformat(), "known": False,
        }
    avatar = getattr(user, "display_avatar", None)
    return {
        "id": str(user_id),
        "username": getattr(user, "name", None),
        "global_name": getattr(user, "global_name", None),
        "display_name": getattr(user, "display_name", None) or getattr(user, "name", None),
        "avatar_url": str(avatar.url) if avatar is not None else None,
        "bot": bool(getattr(user, "bot", False)),
        "created_at": created.isoformat(),
        "known": True,
    }


def serialize_member(member, guild) -> dict:
    role_ids = {r.id for r in getattr(member, "roles", [])}
    roles = [
        r for r in getattr(member, "roles", [])
        if not (getattr(r, "is_default", None) and r.is_default())
    ]
    roles.sort(key=lambda r: getattr(r, "position", 0), reverse=True)
    timed_out_until = None
    is_timed_out = getattr(member, "is_timed_out", None)
    if callable(is_timed_out) and is_timed_out():
        timed_out_until = _iso(getattr(member, "timed_out_until", None))
    color = getattr(getattr(member, "color", None), "value", 0) or 0
    return {
        "nick": getattr(member, "nick", None),
        "joined_at": _iso(getattr(member, "joined_at", None)),
        "premium_since": _iso(getattr(member, "premium_since", None)),
        "pending": bool(getattr(member, "pending", False)),
        "timed_out_until": timed_out_until,
        "color": f"#{color:06x}" if color else None,
        "roles": [
            {
                "id": str(r.id), "name": r.name,
                "color": f"#{r.color.value:06x}" if getattr(getattr(r, "color", None), "value", 0) else None,
            }
            for r in roles
        ],
        "verified": int(getattr(_config, "VERIFIED_ROLE_ID", 0) or 0) in role_ids,
        "unverified": int(getattr(_config, "UNVERIFIED_ROLE_ID", 0) or 0) in role_ids,
        "is_guild_owner": getattr(guild, "owner_id", None) == member.id,
    }


def departure_from_history(history: list[dict]) -> dict | None:
    """The latest kick/ban/unban in mod_actions (history is newest first)."""
    for row in history:
        if row.get("action_type") in ("kick", "ban", "unban"):
            return {"action": row["action_type"], "at": row.get("created_at")}
    return None


async def build_profile(
    bot,
    guild,
    user_id: int,
    *,
    warning_service,
    moderation_service,
    honeypot_repo=None,
    include_honeypot: bool = False,
    actor: Any = None,
) -> dict:
    member = await find_member(guild, user_id)
    user = member if member is not None else await lookup_user(bot, user_id)
    warnings = await warning_service.get_warnings(guild.id, user_id)
    history = await moderation_service.get_history(guild.id, user_id, HISTORY_LIMIT)

    if member is not None:
        ban = {"banned": False, "reason": None}
        status = "member"
    else:
        ban = await lookup_ban(guild, user_id)
        last = departure_from_history(history)
        if ban["banned"]:
            status = "banned"
        elif last is not None and last["action"] == "kick":
            status = "kicked"
        else:
            status = "not_member"

    honeypot = None
    if include_honeypot and honeypot_repo is not None:
        catches = await honeypot_repo.list_events_for_user(guild.id, user_id, HONEYPOT_CATCH_LIMIT)
        appeal = await honeypot_repo.get_latest_real_appeal_for_user(guild.id, user_id)
        honeypot = {
            "catches": [catch_to_dict(c) for c in catches],
            "latest_appeal": appeal_to_dict(appeal) if appeal else None,
        }

    block = None
    if actor is not None:
        block = member_block_reason(guild, actor, member if member is not None else discord.Object(id=user_id))

    return {
        "user_id": user_id,
        "in_guild": member is not None,
        "status": status,
        "user": serialize_user(user, user_id),
        "member": serialize_member(member, guild) if member is not None else None,
        "ban": ban,
        "last_departure": departure_from_history(history),
        "warnings": warnings,
        "history": history,
        "honeypot": honeypot,
        "actor_block_reason": block,
        "actor_checked": actor is not None,
    }
