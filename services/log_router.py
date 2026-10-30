"""
services/log_router.py — routes audit-logged actions to live Discord channels.

This is a thin, DB-agnostic companion to AuditService. AuditService/AuditRepository
only deal with persisting audit rows and intentionally hold no discord.Client
reference (many cogs construct AuditService directly from bot.db). Rather than
making AuditService Discord-aware for every caller, callers invoke
`route_and_post(...)` alongside their existing `audit.log(...)` call.

Channel selection is driven by a small action -> category mapping. Categories
resolve to config-driven channel IDs:
  - "join_leave"  -> config.JOIN_LEAVE_CHANNEL_ID  (joins, leaves, kicks, bans)
  - "moderation"  -> config.LOG_CHANNEL_ID          (timeouts, warns, purges, etc.)

Actions not present in the mapping are DB-only (no live post) — e.g. role
add/remove, message edit/delete, boosts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord

import config as _config

log = logging.getLogger(__name__)

# Action -> channel category. Anything absent here is DB-only (no live post).
_CHANNEL_CATEGORIES: dict[str, str] = {
    # Membership changes (joins/leaves/bot-driven removals)
    "member_joined": "join_leave",
    "member_left": "join_leave",
    "member_kicked": "join_leave",
    "member_banned": "join_leave",
    "member_unbanned": "join_leave",
    # Other moderation actions
    "member_timeout": "moderation",
    "member_untimeout": "moderation",
    "member_warned": "moderation",
    "warning_removed": "moderation",
    "messages_purged": "moderation",
    "spam_detected": "moderation",
    # Gateway-level ("native", i.e. via Discord's own UI) moderation actions —
    # see cogs/logging_moderation.py. Bans/unbans are membership removals ->
    # join_leave, same as their command-driven counterparts. Distinct action
    # strings from the command-level ones by design (see that module's
    # docstring for the double-log tradeoff).
    "member_banned_native": "join_leave",
    "member_unbanned_native": "join_leave",
    "member_timeout_native": "moderation",
    "member_untimeout_native": "moderation",
    # Voice: server-mute/deafen only (moderator-applied, not self toggles) —
    # see cogs/logging_voice.py.
    "member_voice_server_muted": "moderation",
    "member_voice_server_unmuted": "moderation",
    "member_voice_server_deafened": "moderation",
    "member_voice_server_undeafened": "moderation",
}

# Category -> (config attr name, embed color, default title).
_CATEGORY_CONFIG: dict[str, tuple[str, discord.Color]] = {
    "join_leave": ("JOIN_LEAVE_CHANNEL_ID", discord.Color.blue()),
    "moderation": ("LOG_CHANNEL_ID", discord.Color.orange()),
}

# Per-action color overrides (falls back to the category color above).
_ACTION_COLORS: dict[str, discord.Color] = {
    "member_joined": discord.Color.green(),
    "member_left": discord.Color.light_grey(),
    "member_kicked": discord.Color.red(),
    "member_banned": discord.Color.red(),
    "member_unbanned": discord.Color.green(),
    "member_timeout": discord.Color.red(),
    "member_untimeout": discord.Color.green(),
    "member_warned": discord.Color.orange(),
    "warning_removed": discord.Color.green(),
    "messages_purged": discord.Color.orange(),
    "spam_detected": discord.Color.red(),
    "member_banned_native": discord.Color.red(),
    "member_unbanned_native": discord.Color.green(),
    "member_timeout_native": discord.Color.red(),
    "member_untimeout_native": discord.Color.green(),
    "member_voice_server_muted": discord.Color.red(),
    "member_voice_server_unmuted": discord.Color.green(),
    "member_voice_server_deafened": discord.Color.red(),
    "member_voice_server_undeafened": discord.Color.green(),
}

_ACTION_TITLES: dict[str, str] = {
    "member_joined": "Member Joined",
    "member_left": "Member Left",
    "member_kicked": "Member Kicked",
    "member_banned": "Member Banned",
    "member_unbanned": "Member Unbanned",
    "member_timeout": "Member Timed Out",
    "member_untimeout": "Timeout Removed",
    "member_warned": "Member Warned",
    "warning_removed": "Warning Removed",
    "messages_purged": "Messages Purged",
    "spam_detected": "Spam Detected",
    "member_banned_native": "Member Banned (Native)",
    "member_unbanned_native": "Member Unbanned (Native)",
    "member_timeout_native": "Member Timed Out (Native)",
    "member_untimeout_native": "Timeout Removed (Native)",
    "member_voice_server_muted": "Member Server-Muted",
    "member_voice_server_unmuted": "Member Server-Unmuted",
    "member_voice_server_deafened": "Member Server-Deafened",
    "member_voice_server_undeafened": "Member Server-Undeafened",
}


def build_embed(
    action: str,
    *,
    member: discord.abc.User | discord.Object | None = None,
    member_id: int | None = None,
    actor: discord.abc.User | None = None,
    actor_id: int | None = None,
    reason: str | None = None,
    extra_fields: dict[str, str] | None = None,
) -> discord.Embed:
    """Build a simple, consistent embed for a log_router-routed action."""
    color = _ACTION_COLORS.get(action, discord.Color.greyple())
    title = _ACTION_TITLES.get(action, action.replace("_", " ").title())

    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))

    resolved_member_id = member_id if member_id is not None else getattr(member, "id", None)
    if resolved_member_id is not None:
        username = getattr(member, "name", None)
        if username is not None:
            display_name = getattr(member, "display_name", None)
            member_value = f"<@{resolved_member_id}> — **{username}**"
            if display_name and display_name != username:
                member_value += f" (display: {display_name})"
            member_value += f"\nID: {resolved_member_id}"
        else:
            member_value = f"<@{resolved_member_id}> ({resolved_member_id})"
        embed.add_field(name="Member", value=member_value, inline=False)

    resolved_actor_id = actor_id if actor_id is not None else getattr(actor, "id", None)
    if resolved_actor_id is not None and resolved_actor_id != resolved_member_id:
        embed.add_field(name="Moderator", value=f"<@{resolved_actor_id}>", inline=False)

    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)

    if extra_fields:
        for name, value in extra_fields.items():
            embed.add_field(name=name, value=value, inline=True)

    return embed


async def route_and_post(
    bot: discord.Client,
    guild: discord.Guild,
    action: str,
    embed: discord.Embed | None = None,
    *,
    content: str | None = None,
) -> None:
    """
    Post a live message to the channel mapped for `action`, if one is configured.

    Silently no-ops if:
      - `action` has no category mapping (DB-only action)
      - the configured channel ID is 0 / unset
      - the channel can't be resolved in `guild` (not cached, wrong guild, etc.)
      - discord.py raises Forbidden/HTTPException posting (logged, not raised)

    Callers pass a prebuilt `embed` (e.g. a template-rendered one) when available;
    otherwise pass nothing and log_router does not fabricate one on your behalf —
    use `build_embed()` first if you want the default styling.
    """
    category = _CHANNEL_CATEGORIES.get(action)
    if category is None:
        return

    config_attr, _default_color = _CATEGORY_CONFIG[category]
    channel_id = getattr(_config, config_attr, 0)
    if not channel_id:
        return

    channel = guild.get_channel(channel_id)
    if channel is None:
        log.warning(
            "log_router: channel for category=%s (id=%s) not found in guild %s; skipping live post for action=%s",
            category, channel_id, guild.id, action,
        )
        return

    try:
        await channel.send(content=content, embed=embed)
    except discord.Forbidden as e:
        log.warning("log_router: forbidden posting to channel %s for action=%s: %s", channel_id, action, e)
    except discord.HTTPException as e:
        log.warning("log_router: failed posting to channel %s for action=%s: %s", channel_id, action, e)
