"""
Role management for the staff-only dashboard "Roles" page (/dashboard/roles).

Two layers live here:

* Pure helpers (no discord.py objects, unit-tested in
  tests/unit/test_role_admin_logic.py): the Discord permission catalogue,
  colour parsing, hierarchy checks, permission-change validation and the
  reorder computation used for guild.edit_role_positions.
* `RoleAdminService`: the discord.py-facing operations (create / edit / move /
  duplicate / delete roles, list and change role members). Every Discord call
  carries an audit-log reason "Dashboard: <username>" and every mutation writes
  an `audit_log` row through AuditService (which also feeds the live `audit`
  topic).

Rules enforced here, server-side, whatever the UI shows:

* A role is manageable only when the bot holds Manage Roles, the role is not
  @everyone, not managed (bot/integration/booster roles) and sits strictly below
  the bot's top role. Everything else is read-only.
* Only Discord permissions the bot itself holds may be added or removed
  (Discord rejects granting permissions you do not have). Permissions the bot
  lacks keep whatever value the role already had. Administrator can only be
  granted when the bot has Administrator.
* Positions are clamped to 1 .. bot_top_position - 1.
* The ACTING user is held to Discord's own hierarchy too (Discord would refuse
  these actions to them directly). The owner (config.OWNER_ID or the guild
  owner) bypasses this; every other staff member:
    - may edit / move / delete / add or remove members of a role only when it
      sits strictly below their own highest role;
    - has moves and "place above" clamped to directly below their top role;
    - may add or remove only Discord permissions they hold themselves
      (Administrator = all), and never Administrator itself (owner only);
    - may not give a role to themselves, nor give anyone a role that carries
      Discord permissions they do not hold.
* Internal (bot) permission grants go through PermissionService.grant/revoke,
  the same path the Permissions page uses (any staff, no hierarchy check, the
  same as that page; staff bypass internal checks anyway).

The live `roles` topic is NOT published from here: guild role gateway events
(on_guild_role_create/update/delete) are the single publisher, owned by the
live-roles listener, so dashboard changes and hand edits in Discord look the
same to the page.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Permission catalogue (canonical discord.py flag names; aliases such as
# read_messages / manage_emojis / manage_permissions are deliberately absent).
# Flags unknown to the installed discord.py are filtered out at runtime by
# `permission_catalog(valid_flags)`.
# ---------------------------------------------------------------------------

PERMISSION_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "General",
        [
            ("view_channel", "View Channels"),
            ("manage_channels", "Manage Channels"),
            ("manage_roles", "Manage Roles"),
            ("create_expressions", "Create Expressions"),
            ("manage_expressions", "Manage Expressions"),
            ("view_audit_log", "View Audit Log"),
            ("view_guild_insights", "View Server Insights"),
            ("view_creator_monetization_analytics", "View Server Subscription Insights"),
            ("manage_webhooks", "Manage Webhooks"),
            ("manage_guild", "Manage Server"),
            ("create_events", "Create Events"),
            ("manage_events", "Manage Events"),
        ],
    ),
    (
        "Membership",
        [
            ("create_instant_invite", "Create Invite"),
            ("change_nickname", "Change Nickname"),
            ("manage_nicknames", "Manage Nicknames"),
            ("kick_members", "Kick Members"),
            ("ban_members", "Ban Members"),
            ("moderate_members", "Timeout Members"),
        ],
    ),
    (
        "Text",
        [
            ("send_messages", "Send Messages"),
            ("send_messages_in_threads", "Send Messages in Threads"),
            ("create_public_threads", "Create Public Threads"),
            ("create_private_threads", "Create Private Threads"),
            ("embed_links", "Embed Links"),
            ("attach_files", "Attach Files"),
            ("add_reactions", "Add Reactions"),
            ("external_emojis", "Use External Emoji"),
            ("external_stickers", "Use External Stickers"),
            ("mention_everyone", "Mention @everyone, @here and All Roles"),
            ("manage_messages", "Manage Messages"),
            ("pin_messages", "Pin Messages"),
            ("manage_threads", "Manage Threads"),
            ("read_message_history", "Read Message History"),
            ("send_tts_messages", "Send Text-to-Speech Messages"),
            ("send_voice_messages", "Send Voice Messages"),
            ("send_polls", "Create Polls"),
            ("bypass_slowmode", "Bypass Slowmode"),
            ("use_application_commands", "Use Application Commands"),
            ("use_external_apps", "Use External Apps"),
        ],
    ),
    (
        "Voice",
        [
            ("connect", "Connect"),
            ("speak", "Speak"),
            ("stream", "Video"),
            ("use_embedded_activities", "Use Activities"),
            ("use_soundboard", "Use Soundboard"),
            ("use_external_sounds", "Use External Sounds"),
            ("use_voice_activation", "Use Voice Activity"),
            ("priority_speaker", "Priority Speaker"),
            ("mute_members", "Mute Members"),
            ("deafen_members", "Deafen Members"),
            ("move_members", "Move Members"),
            ("set_voice_channel_status", "Set Voice Channel Status"),
            ("request_to_speak", "Request to Speak"),
        ],
    ),
    ("Advanced", [("administrator", "Administrator")]),
]

# Permissions the UI should flag as dangerous (extra emphasis, not extra rules,
# except Administrator which needs an explicit confirm flag on the request).
DANGEROUS_PERMISSIONS: frozenset[str] = frozenset(
    {
        "administrator",
        "manage_guild",
        "manage_roles",
        "manage_channels",
        "ban_members",
        "kick_members",
        "manage_webhooks",
        "mention_everyone",
        "manage_messages",
        "moderate_members",
        "manage_nicknames",
        "view_audit_log",
    }
)

ROLE_NAME_MAX = 100
ICON_MAX_BYTES = 256 * 1024
MEMBERS_PAGE_MAX = 100


class RoleAdminError(Exception):
    """A request the dashboard should get back as a clean 4xx JSON error."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def as_detail(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def all_catalog_flags() -> list[str]:
    return [flag for _, items in PERMISSION_GROUPS for flag, _ in items]


def permission_catalog(valid_flags: Iterable[str]) -> list[dict]:
    """The grouped catalogue restricted to flags this discord.py knows about."""
    valid = set(valid_flags)
    out = []
    for group, items in PERMISSION_GROUPS:
        perms = [
            {"key": flag, "label": label, "dangerous": flag in DANGEROUS_PERMISSIONS}
            for flag, label in items
            if flag in valid
        ]
        if perms:
            out.append({"group": group, "permissions": perms})
    return out


_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(value: str | None) -> int:
    """'#RRGGBB' / 'RRGGBB' -> int; None / '' -> 0 (Discord's 'no colour')."""
    if value is None:
        return 0
    value = value.strip()
    if value == "":
        return 0
    m = _HEX_RE.match(value)
    if not m:
        raise RoleAdminError(400, "invalid_color", "Colour must be a hex value like #5865F2.")
    return int(m.group(1), 16)


def format_color(value: int) -> str | None:
    return f"#{value:06x}" if value else None


def validate_role_name(name: str | None) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise RoleAdminError(400, "invalid_name", "Role name cannot be empty.")
    if len(cleaned) > ROLE_NAME_MAX:
        raise RoleAdminError(400, "invalid_name", f"Role name must be at most {ROLE_NAME_MAX} characters.")
    if cleaned.lower() in {"@everyone", "@here"}:
        raise RoleAdminError(400, "invalid_name", "That role name is reserved by Discord.")
    return cleaned


def unmanageable_reason(
    *,
    is_default: bool,
    managed: bool,
    position: int,
    bot_top_position: int,
    is_bot_top: bool,
    bot_can_manage_roles: bool,
) -> str | None:
    """None when the bot may edit/move/delete/assign this role, else why not."""
    if is_default:
        return "@everyone cannot be edited, moved or deleted from the dashboard."
    if is_bot_top:
        return "This is the bot's own highest role. Roles below it are manageable."
    if managed:
        return "Managed by Discord or an integration (bot, booster or linked role); it cannot be edited here."
    if not bot_can_manage_roles:
        return "The bot does not have the Manage Roles permission."
    if position >= bot_top_position:
        return "This role is at or above the bot's highest role, so Discord does not let the bot change it."
    return None


def actor_unmanageable_reason(*, position: int, actor_top_position: int, is_owner: bool) -> str | None:
    """None when the ACTING user may change this role under Discord's hierarchy
    (strictly below their own highest role; the owner bypasses), else why not."""
    if is_owner:
        return None
    if position >= actor_top_position:
        return "This role is at or above your highest role, so you cannot change it (Discord's role hierarchy)."
    return None


def actor_grantable_permissions(actor_flags: Iterable[str], *, is_owner: bool, known_flags: Iterable[str]) -> set[str]:
    """Discord permission flags the acting user may add to / remove from a role.

    Owner: every known flag. Others: the flags they hold (Administrator counts as
    holding all of them), minus Administrator itself, which only the owner may
    grant or remove from the dashboard.
    """
    known = set(known_flags)
    if is_owner:
        return known
    held = set(actor_flags)
    flags = set(known) if "administrator" in held else held & known
    flags.discard("administrator")
    return flags


def resolve_permission_change(
    requested: Iterable[str] | None,
    current: Iterable[str],
    bot_held: Iterable[str],
    *,
    known_flags: Iterable[str],
    confirm_administrator: bool = False,
    actor_grantable: Iterable[str] | None = None,
) -> set[str]:
    """Validate a requested permission set against what the bot (and, when
    `actor_grantable` is given, the acting user) may change.

    Returns the final set of enabled flags. Flags the bot does not hold must keep
    their current value (Discord rejects adding them; removing them is refused
    too so the result is predictable); the same applies to flags outside
    `actor_grantable`. Adding Administrator requires `confirm_administrator`.
    Unknown flag names are a 400.
    """
    current_set = set(current)
    if requested is None:
        return current_set
    requested_set = set(requested)
    known = set(known_flags)
    unknown = sorted(requested_set - known)
    if unknown:
        raise RoleAdminError(400, "unknown_permission", f"Unknown permission(s): {', '.join(unknown)}.", permissions=unknown)
    held = set(bot_held)
    changed = requested_set ^ current_set
    locked = sorted(p for p in changed if p not in held)
    if locked:
        raise RoleAdminError(
            403,
            "permission_not_held",
            "The bot can only grant or remove permissions it has itself: " + ", ".join(locked) + ".",
            permissions=locked,
        )
    if actor_grantable is not None:
        allowed = set(actor_grantable)
        over = sorted(p for p in changed if p not in allowed)
        if over:
            if over == ["administrator"]:
                message = "Only the server owner can grant or remove Administrator from the dashboard."
            else:
                message = "You can only grant or remove Discord permissions you have yourself: " + ", ".join(over) + "."
            raise RoleAdminError(403, "actor_permission_not_held", message, permissions=over)
    if "administrator" in requested_set and "administrator" not in current_set and not confirm_administrator:
        raise RoleAdminError(400, "administrator_needs_confirm", "Granting Administrator needs explicit confirmation.")
    return requested_set


def clamp_position(position: int, bot_top_position: int) -> int:
    """Clamp a target role position into the bot-manageable band 1..top-1."""
    upper = max(1, bot_top_position - 1)
    return max(1, min(int(position), upper))


def compute_reorder(
    roles: Iterable[tuple[int, int]],
    role_id: int,
    target_position: int,
    bot_top_position: int,
    ceiling_role_id: int | None = None,
) -> dict[int, int]:
    """Positions to send to guild.edit_role_positions to move `role_id`.

    `roles` is every guild role as (id, position), @everyone included or not
    (position 0 is ignored). Only roles strictly below the bot's top role are
    renumbered 1..n in their current order (ties broken by id, as Discord
    does), with `role_id` moved to `target_position` (clamped). When
    `ceiling_role_id` (the acting user's top role) is inside that band, the
    target is also clamped to directly below it. Returns only the roles whose
    position actually changes; {} means nothing to do.
    """
    band = sorted(
        ((rid, pos) for rid, pos in roles if 0 < pos < bot_top_position),
        key=lambda rp: (rp[1], rp[0]),
    )
    ids = [rid for rid, _ in band]
    if role_id not in ids:
        raise RoleAdminError(409, "role_not_manageable", "That role is not below the bot's highest role.")
    current = dict(band)
    ids.remove(role_id)
    target = clamp_position(target_position, bot_top_position)
    index = max(0, min(target - 1, len(ids)))
    if ceiling_role_id is not None and ceiling_role_id != role_id and ceiling_role_id in ids:
        index = min(index, ids.index(ceiling_role_id))
    ids.insert(index, role_id)
    if len(ids) >= bot_top_position:
        # Tied/stale positions: renumbering would collide with the bot's role.
        raise RoleAdminError(
            409, "positions_out_of_sync", "Discord's role positions are out of sync. Reload the page and try again."
        )
    changes: dict[int, int] = {}
    for i, rid in enumerate(ids):
        new_pos = i + 1
        if current[rid] != new_pos:
            changes[rid] = new_pos
    return changes


def position_above(
    roles: Iterable[tuple[int, int]], role_id: int, above_role_id: int, bot_top_position: int, everyone_id: int
) -> int:
    """Target position that puts `role_id` directly above `above_role_id`
    (@everyone = the bottom), computed against the current ordering so it stays
    right when Discord shifts positions after a create."""
    if above_role_id == everyone_id:
        return 1
    band = sorted(
        ((rid, pos) for rid, pos in roles if 0 < pos < bot_top_position and rid != role_id),
        key=lambda rp: (rp[1], rp[0]),
    )
    ids = [rid for rid, _ in band]
    if above_role_id not in ids:
        raise RoleAdminError(409, "role_not_manageable", "The role to place it above is not below the bot's highest role.")
    return ids.index(above_role_id) + 2


def audit_reason(actor_name: str | None, action: str | None = None) -> str:
    """The Discord audit-log reason for every dashboard role action."""
    who = (actor_name or "unknown").strip() or "unknown"
    reason = f"Dashboard: {who}"
    if action:
        reason += f" ({action})"
    return reason[:512]


def decode_icon(data: str | None) -> bytes | None:
    """Decode a base64 (optionally data: URL) PNG/JPEG/WebP role icon."""
    if not data:
        return None
    if data.startswith("data:"):
        _, _, data = data.partition(",")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise RoleAdminError(400, "invalid_icon", "Icon must be a base64-encoded image.")
    if len(raw) > ICON_MAX_BYTES:
        raise RoleAdminError(400, "invalid_icon", "Role icons must be 256 KB or smaller.")
    if not (raw.startswith(b"\x89PNG") or raw.startswith(b"\xff\xd8\xff") or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")):
        raise RoleAdminError(400, "invalid_icon", "Role icons must be PNG, JPEG or WebP images.")
    return raw


def validate_unicode_emoji(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > 16 or any(c.isascii() and c.isalnum() for c in value):
        raise RoleAdminError(400, "invalid_emoji", "Use a single standard emoji (custom emoji are not supported).")
    return value


def paginate(items: list, offset: int, limit: int) -> tuple[list, int]:
    total = len(items)
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), MEMBERS_PAGE_MAX))
    return items[offset : offset + limit], total


def copy_name(name: str) -> str:
    suffix = " (copy)"
    return (name[: ROLE_NAME_MAX - len(suffix)] + suffix) if len(name) + len(suffix) > ROLE_NAME_MAX else name + suffix


def role_member_counts(members: Iterable[Any], guild_id: int, total_members: int | None) -> dict[int, int]:
    """Member count per role id in ONE pass over the member cache.

    discord.py's Role.members scans every cached member on each call, so
    len(role.members) per role is O(roles x members) on the bot's event loop.
    Uses the member's raw role-id list (`_roles`, which excludes @everyone)
    when present, else `roles`. @everyone = the guild's member count.
    """
    counts: Counter[int] = Counter()
    n = 0
    for m in members:
        n += 1
        ids = getattr(m, "_roles", None)
        if ids is None:
            ids = [r.id for r in getattr(m, "roles", ())]
        counts.update(ids)
    counts[guild_id] = total_members if total_members is not None else n
    return dict(counts)


# ---------------------------------------------------------------------------
# discord.py-facing service
# ---------------------------------------------------------------------------


_DISCORD_FIELDS = ("name", "color", "hoist", "mentionable", "permissions", "unicode_emoji", "icon_base64")


@dataclass
class Actor:
    id: int
    name: str | None


@dataclass
class ActorContext:
    """The acting dashboard user's standing in Discord's role hierarchy."""

    id: int
    is_owner: bool
    is_member: bool
    top_role_id: int | None
    top_position: int  # 0 = only @everyone (or not a member)
    grantable: frozenset[str] = field(default_factory=frozenset)

    def as_dict(self) -> dict:
        return {
            "user_id": str(self.id),
            "is_owner": self.is_owner,
            "is_member": self.is_member,
            "top_role_id": str(self.top_role_id) if self.top_role_id else None,
            "top_position": self.top_position,
            "permissions": sorted(self.grantable),
        }


class RoleAdminService:
    def __init__(self, bot, guild, audit=None) -> None:
        self.bot = bot
        self.guild = guild
        self.audit = audit
        self.permission_service = getattr(bot, "permission_service", None)

    # -- context ------------------------------------------------------------
    @property
    def me(self):
        return self.guild.me

    def _known_flags(self) -> list[str]:
        import discord

        valid = set(discord.Permissions.VALID_FLAGS)
        return [f for f in all_catalog_flags() if f in valid]

    def bot_held_permissions(self) -> set[str]:
        perms = self.me.guild_permissions
        return {f for f in self._known_flags() if getattr(perms, f, False)}

    def bot_top(self):
        return self.me.top_role

    def unmanageable_reason(self, role) -> str | None:
        top = self.bot_top()
        return unmanageable_reason(
            is_default=role.is_default(),
            managed=role.managed,
            position=role.position,
            bot_top_position=top.position,
            is_bot_top=role.id == top.id,
            bot_can_manage_roles=bool(self.me.guild_permissions.manage_roles),
        )

    def role_icons_supported(self) -> bool:
        return "ROLE_ICONS" in (self.guild.features or [])

    def get_role(self, role_id: int):
        role = self.guild.get_role(role_id)
        if role is None:
            raise RoleAdminError(404, "role_not_found", "That role no longer exists.")
        return role

    def require_manageable(self, role, ctx: ActorContext | None = None) -> None:
        reason = self.unmanageable_reason(role)
        if reason:
            raise RoleAdminError(409, "role_not_manageable", reason)
        if ctx is not None:
            reason = actor_unmanageable_reason(
                position=role.position, actor_top_position=ctx.top_position, is_owner=ctx.is_owner
            )
            if reason:
                raise RoleAdminError(403, "actor_hierarchy", reason)

    # -- acting user ------------------------------------------------------------
    def _is_owner(self, user_id: int) -> bool:
        import config

        owner_id = int(getattr(config, "OWNER_ID", 0) or 0)
        guild_owner = getattr(self.guild, "owner_id", None)
        return bool(owner_id and user_id == owner_id) or (guild_owner is not None and user_id == guild_owner)

    async def actor_context(self, actor_id: int, *, require_member: bool = True) -> ActorContext:
        """Resolve the acting user's hierarchy position and grantable flags.

        Non-owners who are not guild members get 403 `actor_not_member` when
        `require_member` (mutations), or a context that can change nothing
        (overview)."""
        import discord

        is_owner = self._is_owner(actor_id)
        known = self._known_flags()
        member = self.guild.get_member(actor_id)
        if member is None:
            try:
                member = await self.guild.fetch_member(actor_id)
            except (discord.NotFound, discord.HTTPException):
                member = None
        if member is None:
            if is_owner:
                return ActorContext(actor_id, True, False, None, 0, frozenset(known))
            if require_member:
                raise RoleAdminError(403, "actor_not_member", "You are not a member of this server, so you cannot change roles.")
            return ActorContext(actor_id, False, False, None, 0, frozenset())
        top = getattr(member, "top_role", None)
        perms = getattr(member, "guild_permissions", None)
        held = {f for f in known if perms is not None and getattr(perms, f, False)}
        return ActorContext(
            id=actor_id,
            is_owner=is_owner,
            is_member=True,
            top_role_id=top.id if top is not None else None,
            top_position=top.position if top is not None else 0,
            grantable=frozenset(actor_grantable_permissions(held, is_owner=is_owner, known_flags=known)),
        )

    def _ceiling(self, ctx: ActorContext | None) -> int | None:
        """The acting user's top role id when it caps moves (non-owner only)."""
        if ctx is None or ctx.is_owner:
            return None
        return ctx.top_role_id

    def role_permissions(self, role) -> set[str]:
        return {f for f in self._known_flags() if getattr(role.permissions, f, False)}

    async def _grants_by_role(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        if self.permission_service is None:
            return out
        for g in await self.permission_service.list_grants(self.guild.id):
            out.setdefault(int(g["role_id"]), []).append(g["permission"])
        for perms in out.values():
            perms.sort()
        return out

    def member_counts(self) -> dict[int, int]:
        members = getattr(self.guild, "members", None) or []
        return role_member_counts(members, self.guild.id, getattr(self.guild, "member_count", None))

    def serialize(
        self,
        role,
        grants: dict[int, list[str]],
        *,
        counts: dict[int, int] | None = None,
        ctx: ActorContext | None = None,
    ) -> dict:
        from services.permission_service import DEFAULT_STAFF_ROLE_IDS

        tags = getattr(role, "tags", None)
        reason = self.unmanageable_reason(role)
        if reason is None and ctx is not None:
            reason = actor_unmanageable_reason(
                position=role.position, actor_top_position=ctx.top_position, is_owner=ctx.is_owner
            )
        icon = getattr(role, "icon", None)
        member_count = counts.get(role.id, 0) if counts is not None else len(role.members)
        return {
            "id": str(role.id),
            "name": role.name,
            "color": format_color(role.colour.value),
            "position": role.position,
            "member_count": member_count,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "managed": role.managed,
            "is_default": role.is_default(),
            "is_bot_managed": bool(tags and tags.is_bot_managed()),
            "is_integration": bool(tags and tags.is_integration()),
            "is_premium_subscriber": bool(tags and tags.is_premium_subscriber()),
            "is_bot_top_role": role.id == self.bot_top().id,
            "manageable": reason is None,
            "unmanageable_reason": reason,
            "permissions": sorted(self.role_permissions(role)),
            "unicode_emoji": getattr(role, "unicode_emoji", None),
            "icon_url": icon.url if icon else None,
            "is_default_staff": role.id in DEFAULT_STAFF_ROLE_IDS,
            "internal_permissions": grants.get(role.id, []),
            "created_at": role.created_at.isoformat() if getattr(role, "created_at", None) else None,
        }

    async def overview(self, actor_id: int | None = None) -> dict:
        """Every role plus bot/actor hierarchy info. With `actor_id` (the
        dashboard always sends the session user), `manageable` also applies the
        acting user's hierarchy and `actor` describes what they may grant."""
        import discord

        from services.permission_service import VALID_PERMISSIONS

        ctx = await self.actor_context(actor_id, require_member=False) if actor_id is not None else None
        grants = await self._grants_by_role()
        counts = self.member_counts()
        roles = sorted(self.guild.roles, key=lambda r: (r.position, r.id), reverse=True)
        top = self.bot_top()
        bot_perms = self.me.guild_permissions
        return {
            "roles": [self.serialize(r, grants, counts=counts, ctx=ctx) for r in roles],
            "actor": ctx.as_dict() if ctx is not None else None,
            "bot": {
                "user_id": str(self.me.id),
                "top_role_id": str(top.id),
                "top_role_name": top.name,
                "top_position": top.position,
                "administrator": bool(bot_perms.administrator),
                "manage_roles": bool(bot_perms.manage_roles),
                "permissions": sorted(self.bot_held_permissions()),
            },
            "guild": {
                "role_icons": self.role_icons_supported(),
                "premium_tier": getattr(self.guild, "premium_tier", 0),
                "role_count": len(self.guild.roles),
                "role_limit": 250,
            },
            "permission_catalog": permission_catalog(discord.Permissions.VALID_FLAGS),
            "valid_internal_permissions": sorted(VALID_PERMISSIONS),
        }

    # -- helpers for mutations ---------------------------------------------
    def _permissions_obj(self, flags: Iterable[str]):
        import discord

        perms = discord.Permissions.none()
        for f in flags:
            setattr(perms, f, True)
        return perms

    def _icon_kwargs(self, *, unicode_emoji: str | None, icon_base64: str | None, clear_icon: bool) -> dict:
        wants = bool(unicode_emoji) or bool(icon_base64) or clear_icon
        if not wants:
            return {}
        if not self.role_icons_supported():
            if clear_icon and not unicode_emoji and not icon_base64:
                return {}
            raise RoleAdminError(400, "role_icons_unsupported", "This server cannot use role icons (it needs boost level 2).")
        if icon_base64:
            return {"display_icon": decode_icon(icon_base64)}
        if unicode_emoji:
            return {"display_icon": validate_unicode_emoji(unicode_emoji)}
        return {"display_icon": None}

    async def _sync_internal(self, role_id: int, wanted: Iterable[str] | None, actor: Actor) -> dict:
        """Grant/revoke internal permissions so the role holds exactly `wanted`."""
        if wanted is None or self.permission_service is None:
            return {"granted": [], "revoked": []}
        from services.permission_service import VALID_PERMISSIONS

        wanted_set = set(wanted)
        bad = sorted(wanted_set - set(VALID_PERMISSIONS))
        if bad:
            raise RoleAdminError(400, "unknown_internal_permission", f"Unknown bot permission(s): {', '.join(bad)}.")
        current = set((await self._grants_by_role()).get(role_id, []))
        granted, revoked = [], []
        for perm in sorted(wanted_set - current):
            errors = await self.permission_service.grant(self.guild.id, role_id, perm, actor.id)
            if not errors:
                granted.append(perm)
        for perm in sorted(current - wanted_set):
            if await self.permission_service.revoke(self.guild.id, role_id, perm, actor_id=actor.id):
                revoked.append(perm)
        return {"granted": granted, "revoked": revoked}

    async def _audit(self, actor: Actor, action: str, role_id: int | None, detail: dict) -> None:
        if self.audit is None:
            return
        try:
            await self.audit.log(self.guild.id, actor.id, action, "role", role_id, {"via": "dashboard", **detail})
        except Exception:  # an audit failure must not undo a Discord change
            log.warning("role admin audit write failed (%s)", action, exc_info=True)

    async def _move(
        self,
        role,
        target_position: int | None,
        actor: Actor,
        *,
        above_role_id: int | None = None,
        ceiling_role_id: int | None = None,
    ) -> int:
        """Reorder `role`; returns its resulting position."""
        import discord

        # Use Discord's authoritative positions (one GET): the gateway cache can
        # lag right after a create, and positions shift when roles are added.
        try:
            fresh = await self.guild.fetch_roles()
            positions = {r.id: r.position for r in fresh}
        except discord.HTTPException:
            positions = {r.id: r.position for r in self.guild.roles}
        positions.setdefault(role.id, role.position)
        top = self.bot_top()
        top_position = positions.get(top.id, top.position)
        if above_role_id is not None:
            target_position = position_above(positions.items(), role.id, above_role_id, top_position, self.guild.id)
        changes = compute_reorder(
            list(positions.items()), role.id, target_position, top_position, ceiling_role_id=ceiling_role_id
        )
        if not changes:
            return positions[role.id]
        mapping = {discord.Object(id=rid): pos for rid, pos in changes.items()}
        await self.guild.edit_role_positions(mapping, reason=audit_reason(actor.name, "reorder roles"))
        return changes.get(role.id, positions[role.id])

    async def _role_result(self, role_id: int, fallback, actor: Actor) -> dict:
        """Serialize one role after a mutation, with a fresh actor context
        (positions shift on create/move). One role, so len(role.members) is fine."""
        role = self.guild.get_role(role_id) or fallback
        ctx = await self.actor_context(actor.id, require_member=False)
        return self.serialize(role, await self._grants_by_role(), ctx=ctx)

    # -- mutations ------------------------------------------------------------
    async def create_role(
        self,
        actor: Actor,
        *,
        name: str,
        color: str | None,
        hoist: bool,
        mentionable: bool,
        permissions: list[str],
        position: int | None,
        unicode_emoji: str | None,
        icon_base64: str | None,
        internal_permissions: list[str],
        confirm_administrator: bool,
        above_role_id: int | None = None,
        action: str = "role_create",
        extra_detail: dict | None = None,
        ctx: ActorContext | None = None,
    ) -> dict:
        if not self.me.guild_permissions.manage_roles:
            raise RoleAdminError(403, "bot_missing_manage_roles", "The bot does not have the Manage Roles permission.")
        if ctx is None:
            ctx = await self.actor_context(actor.id)
        if not ctx.is_owner and ctx.top_position < 1:
            raise RoleAdminError(403, "actor_hierarchy", "You need a role of your own before you can create roles.")
        if len(self.guild.roles) >= 250:
            raise RoleAdminError(409, "role_limit", "This server already has the maximum of 250 roles.")
        clean_name = validate_role_name(name)
        colour = parse_color(color)
        flags = resolve_permission_change(
            permissions,
            set(),
            self.bot_held_permissions(),
            known_flags=self._known_flags(),
            confirm_administrator=confirm_administrator,
            actor_grantable=ctx.grantable,
        )
        icon_kwargs = self._icon_kwargs(unicode_emoji=unicode_emoji, icon_base64=icon_base64, clear_icon=False)
        # Validate internal grants before touching Discord.
        if internal_permissions:
            from services.permission_service import VALID_PERMISSIONS

            bad = sorted(set(internal_permissions) - set(VALID_PERMISSIONS))
            if bad:
                raise RoleAdminError(400, "unknown_internal_permission", f"Unknown bot permission(s): {', '.join(bad)}.")

        role = await self.guild.create_role(
            name=clean_name,
            permissions=self._permissions_obj(flags),
            colour=colour,
            hoist=bool(hoist),
            mentionable=bool(mentionable),
            reason=audit_reason(actor.name, "create role"),
            **icon_kwargs,
        )
        warnings: list[str] = []
        if above_role_id is not None or (position is not None and int(position) > 1):
            try:
                await self._move(
                    role, position, actor, above_role_id=above_role_id, ceiling_role_id=self._ceiling(ctx)
                )
            except Exception as exc:  # role exists now; report, don't fail
                warnings.append(f"Role created but could not be moved: {exc}")
        internal = await self._sync_internal(role.id, internal_permissions or [], actor)
        await self._audit(
            actor,
            action,
            role.id,
            {
                "name": clean_name,
                "color": format_color(colour),
                "hoist": bool(hoist),
                "mentionable": bool(mentionable),
                "permissions": sorted(flags),
                "position": position,
                "above_role_id": str(above_role_id) if above_role_id else None,
                "internal_permissions": internal["granted"],
                **(extra_detail or {}),
            },
        )
        return {"role": await self._role_result(role.id, role, actor), "warnings": warnings}

    async def update_role(
        self,
        actor: Actor,
        role_id: int,
        *,
        fields: dict,
        internal_permissions: list[str] | None,
        confirm_administrator: bool,
    ) -> dict:
        role = self.get_role(role_id)
        wants_discord = any(fields.get(k) is not None for k in _DISCORD_FIELDS) or bool(fields.get("clear_icon"))
        kwargs: dict[str, Any] = {}
        changes: dict[str, Any] = {}
        if wants_discord:
            ctx = await self.actor_context(actor.id)
            self.require_manageable(role, ctx)
            if fields.get("name") is not None:
                kwargs["name"] = validate_role_name(fields["name"])
                if kwargs["name"] != role.name:
                    changes["name"] = [role.name, kwargs["name"]]
            if fields.get("color") is not None:
                kwargs["colour"] = parse_color(fields["color"])
                if kwargs["colour"] != role.colour.value:
                    changes["color"] = [format_color(role.colour.value), format_color(kwargs["colour"])]
            if fields.get("hoist") is not None:
                kwargs["hoist"] = bool(fields["hoist"])
                if kwargs["hoist"] != role.hoist:
                    changes["hoist"] = [role.hoist, kwargs["hoist"]]
            if fields.get("mentionable") is not None:
                kwargs["mentionable"] = bool(fields["mentionable"])
                if kwargs["mentionable"] != role.mentionable:
                    changes["mentionable"] = [role.mentionable, kwargs["mentionable"]]
            if fields.get("permissions") is not None:
                current = self.role_permissions(role)
                final = resolve_permission_change(
                    fields["permissions"],
                    current,
                    self.bot_held_permissions(),
                    known_flags=self._known_flags(),
                    confirm_administrator=confirm_administrator,
                    actor_grantable=ctx.grantable,
                )
                if final != current:
                    # Start from the role's real value so flags outside the
                    # catalogue (if any) are preserved.
                    import discord

                    perms = discord.Permissions(role.permissions.value)
                    for f in self._known_flags():
                        setattr(perms, f, f in final)
                    kwargs["permissions"] = perms
                    changes["permissions"] = {
                        "added": sorted(final - current),
                        "removed": sorted(current - final),
                    }
            icon_kwargs = self._icon_kwargs(
                unicode_emoji=fields.get("unicode_emoji"),
                icon_base64=fields.get("icon_base64"),
                clear_icon=bool(fields.get("clear_icon")),
            )
            if icon_kwargs:
                kwargs.update(icon_kwargs)
                changes["icon"] = "cleared" if icon_kwargs.get("display_icon") is None else "changed"
            if kwargs:
                await role.edit(reason=audit_reason(actor.name, "edit role"), **kwargs)
        internal = {"granted": [], "revoked": []}
        if internal_permissions is not None:
            if role.is_default():
                raise RoleAdminError(409, "role_not_manageable", "@everyone cannot hold bot permissions.")
            if role.managed:
                raise RoleAdminError(409, "role_not_manageable", "Managed roles cannot hold bot permissions.")
            internal = await self._sync_internal(role.id, internal_permissions, actor)
            if internal["granted"] or internal["revoked"]:
                changes["internal_permissions"] = internal
        if changes:
            await self._audit(actor, "role_update", role.id, {"name": role.name, "changes": changes})
        return {"role": await self._role_result(role.id, role, actor), "changed": bool(changes)}

    async def move_role(self, actor: Actor, role_id: int, position: int) -> dict:
        role = self.get_role(role_id)
        ctx = await self.actor_context(actor.id)
        self.require_manageable(role, ctx)
        before = role.position
        after = await self._move(role, position, actor, ceiling_role_id=self._ceiling(ctx))
        await self._audit(actor, "role_move", role.id, {"name": role.name, "from": before, "to": after})
        return {"role": await self._role_result(role.id, role, actor)}

    async def duplicate_role(
        self, actor: Actor, role_id: int, name: str | None, *, confirm_administrator: bool = False
    ) -> dict:
        source = self.get_role(role_id)
        if source.is_default():
            raise RoleAdminError(409, "role_not_manageable", "@everyone cannot be duplicated.")
        ctx = await self.actor_context(actor.id)
        # Copy only what both the bot and the acting user may grant.
        held = self.bot_held_permissions() & set(ctx.grantable)
        source_perms = self.role_permissions(source)
        kept = source_perms & held
        dropped = sorted(source_perms - held)
        # Copying Administrator is a fresh grant: same typed confirmation as
        # create/edit (checked here so nothing is created without it).
        if "administrator" in kept and not confirm_administrator:
            raise RoleAdminError(
                400,
                "administrator_needs_confirm",
                "This role has Administrator. Copying it needs explicit confirmation.",
            )
        # Directly below the source when the source is in the bot's band,
        # otherwise at the bottom (moves are clamped below the actor's top role).
        position = source.position if self.unmanageable_reason(source) is None else None
        grants = (await self._grants_by_role()).get(source.id, []) if not source.managed else []
        result = await self.create_role(
            actor,
            name=name or copy_name(source.name),
            color=format_color(source.colour.value),
            hoist=source.hoist,
            mentionable=source.mentionable,
            permissions=sorted(kept),
            position=position,
            unicode_emoji=getattr(source, "unicode_emoji", None) if self.role_icons_supported() else None,
            icon_base64=None,
            internal_permissions=list(grants),
            confirm_administrator=confirm_administrator,
            action="role_duplicate",
            extra_detail={"source_role_id": str(source.id), "dropped_permissions": dropped},
            ctx=ctx,
        )
        result["dropped_permissions"] = dropped
        return result

    async def delete_role(self, actor: Actor, role_id: int, confirm_name: str) -> dict:
        role = self.get_role(role_id)
        self.require_manageable(role, await self.actor_context(actor.id))
        if (confirm_name or "").strip() != role.name.strip():
            raise RoleAdminError(400, "confirm_mismatch", "Type the role's exact name to delete it.")
        name, members = role.name, len(role.members)
        internal = (await self._grants_by_role()).get(role.id, [])
        await role.delete(reason=audit_reason(actor.name, "delete role"))
        # Leave no dangling internal grants behind for a role id that is gone.
        if internal and self.permission_service is not None:
            for perm in internal:
                await self.permission_service.revoke(self.guild.id, role_id, perm, actor_id=actor.id)
        await self._audit(
            actor, "role_delete", role_id, {"name": name, "member_count": members, "internal_permissions": internal}
        )
        return {"deleted": True}

    # -- members --------------------------------------------------------------
    @staticmethod
    def _member(member) -> dict:
        return {
            "user_id": str(member.id),
            "username": member.name,
            "display_name": member.display_name,
            "avatar_url": member.display_avatar.url,
            "bot": bool(member.bot),
        }

    def list_members(self, role_id: int, offset: int, limit: int, q: str | None) -> dict:
        role = self.get_role(role_id)
        members = list(role.members)
        if q:
            needle = q.strip().lower()
            members = [
                m
                for m in members
                if needle in m.name.lower() or needle in m.display_name.lower() or needle == str(m.id)
            ]
        members.sort(key=lambda m: (m.display_name.lower(), m.id))
        page, total = paginate(members, offset, limit)
        return {"members": [self._member(m) for m in page], "total": total, "offset": max(0, int(offset))}

    async def _resolve_member(self, user_id: int):
        import discord

        member = self.guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await self.guild.fetch_member(user_id)
        except discord.NotFound:
            raise RoleAdminError(404, "member_not_found", "That user is not a member of this server.")

    async def add_member(self, actor: Actor, role_id: int, user_id: int) -> dict:
        role = self.get_role(role_id)
        ctx = await self.actor_context(actor.id)
        self.require_manageable(role, ctx)
        if not ctx.is_owner:
            if user_id == actor.id:
                raise RoleAdminError(
                    403, "self_assign_forbidden", "Only the server owner can give roles to themselves from the dashboard."
                )
            beyond = sorted(self.role_permissions(role) - set(ctx.grantable))
            if beyond:
                raise RoleAdminError(
                    403,
                    "actor_permission_not_held",
                    "This role carries Discord permissions you do not have, so you cannot give it out: "
                    + ", ".join(beyond)
                    + ".",
                    permissions=beyond,
                )
        member = await self._resolve_member(user_id)
        if any(r.id == role.id for r in member.roles):
            return {"changed": False, "member": self._member(member)}
        await member.add_roles(role, reason=audit_reason(actor.name, "add role"))
        await self._audit(
            actor, "role_member_add", role.id, {"name": role.name, "user_id": str(member.id)}
        )
        return {"changed": True, "member": self._member(member)}

    async def remove_member(self, actor: Actor, role_id: int, user_id: int) -> dict:
        role = self.get_role(role_id)
        self.require_manageable(role, await self.actor_context(actor.id))
        member = await self._resolve_member(user_id)
        if not any(r.id == role.id for r in member.roles):
            return {"changed": False, "member": self._member(member)}
        await member.remove_roles(role, reason=audit_reason(actor.name, "remove role"))
        await self._audit(
            actor, "role_member_remove", role.id, {"name": role.name, "user_id": str(member.id)}
        )
        return {"changed": True, "member": self._member(member)}
