"""
Shared "can this person act on that target / hand out that role" checks.

Pure functions over duck-typed discord.py objects (anything with `.id`,
`.top_role.position`, `.guild_permissions`, `.permissions`, ...), so they are
unit-testable without a gateway. Every function returns None when the action
is allowed, or a short human-readable reason (safe to show to the actor) when
it is not. Callers decide how to surface it (ephemeral reply, HTTP 403, ...).

Used by moderation (Discord prefix/slash commands and the dashboard's
internal API routes) and by self-assignable roles (reaction-role buttons,
autoroles). Role-admin (services/role_admin_service.py) keeps its own richer
rules; this module does not replace them.

Rules:
  * Members: nobody acts on the guild owner, on the bot, or on themselves.
    The bot's top role must be strictly above the target's. A non-owner actor's
    top role must also be strictly above the target's (equal = blocked).
    `actor_is_owner` (config.OWNER_ID or the guild owner) skips only the actor
    rank check, never the bot's own limits.
  * Self-assignable roles: never @everyone, a managed (integration/booster)
    role, a role at or above the bot's top role, or a role carrying any
    DANGEROUS_PERMISSIONS flag. A non-owner configuring actor must also rank
    strictly above the role. Callers must additionally refuse roles that hold
    bot permission grants or are in STAFF_ROLE_IDS (that needs the DB, so it is
    not done here; see role_grant_block_reason's `privileged_role_ids`).
"""

from __future__ import annotations

from typing import Any, Iterable

# Discord permission flags that make a role "staff-like". A role with any of
# these must never be self-assignable (button, reaction, autorole).
DANGEROUS_PERMISSIONS: frozenset[str] = frozenset({
    "administrator",
    "ban_members",
    "kick_members",
    "moderate_members",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "manage_nicknames",
    "manage_threads",
    "manage_events",
    "manage_expressions",
    "manage_emojis",
    "manage_emojis_and_stickers",
    "mention_everyone",
    "view_audit_log",
    "move_members",
    "mute_members",
    "deafen_members",
})


def _pos(member_or_role: Any) -> int:
    """Position of a role, or of a member's top role (0 when unknown)."""
    if member_or_role is None:
        return 0
    top = getattr(member_or_role, "top_role", None)
    if top is not None:
        return int(getattr(top, "position", 0) or 0)
    return int(getattr(member_or_role, "position", 0) or 0)


def dangerous_flags(role: Any) -> list[str]:
    """Sorted DANGEROUS_PERMISSIONS flags the role carries."""
    perms = getattr(role, "permissions", None)
    if perms is None:
        return []
    return sorted(f for f in DANGEROUS_PERMISSIONS if getattr(perms, f, False))


def member_action_block_reason(
    actor: Any,
    target: Any,
    bot_member: Any,
    *,
    actor_is_owner: bool = False,
    guild_owner_id: int | None = None,
) -> str | None:
    """Why `actor` may not moderate (kick/ban/timeout/warn/...) `target`, or None.

    `target` may be a Member or, for users who are not in the guild (ban by
    ID), None-like for roles: pass a plain object with only `.id`; it then has
    no top role (position 0) and only the identity rules apply."""
    target_id = getattr(target, "id", None)
    if target_id is None:
        return "Unknown target."
    if getattr(actor, "id", None) == target_id:
        return "You can't use this on yourself."
    if bot_member is not None and getattr(bot_member, "id", None) == target_id:
        return "I can't use this on myself."
    if guild_owner_id is not None and target_id == guild_owner_id:
        return "The server owner can't be moderated."
    target_pos = _pos(target)
    if bot_member is not None and getattr(target, "top_role", None) is not None and _pos(bot_member) <= target_pos:
        return "That member's top role is at or above mine, so I can't act on them."
    if not actor_is_owner and getattr(target, "top_role", None) is not None and _pos(actor) <= target_pos:
        return "That member's top role is at or above yours, so you can't act on them."
    return None


def role_grant_block_reason(
    role: Any,
    bot_member: Any,
    *,
    actor: Any = None,
    actor_is_owner: bool = False,
    privileged_role_ids: Iterable[int] = (),
) -> str | None:
    """Why `role` may not be made self-assignable (or granted by a self-role
    click), or None. Pass `actor` when a person is configuring it; leave it
    None for the click-time re-check."""
    if role is None:
        return "That role no longer exists."
    is_default = getattr(role, "is_default", None)
    if callable(is_default) and is_default():
        return "@everyone can't be a self-assignable role."
    if getattr(role, "managed", False):
        return "That role is managed by an integration and can't be assigned."
    if bot_member is not None and _pos(bot_member) <= _pos(role):
        return "That role is at or above my top role, so I can't assign it."
    flags = dangerous_flags(role)
    if flags:
        return "That role has moderator permissions (" + ", ".join(flags) + ") and can't be self-assignable."
    if int(getattr(role, "id", 0) or 0) in {int(r) for r in privileged_role_ids}:
        return "That role is a staff role or holds bot permissions, so it can't be self-assignable."
    if actor is not None and not actor_is_owner and _pos(actor) <= _pos(role):
        return "That role is at or above your top role."
    return None
