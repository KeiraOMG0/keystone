"""
Desired-state definition for the server layout.

This is the single source of truth for what should exist: roles, categories,
channels, and permission overwrites. Nothing in this file talks to Discord —
it is plain data. Role references in overwrites are by NAME, never by ID;
IDs only ever live in the generated state.json lockfile (see state.py).

Edit this file to change server structure, then re-run setup_server — the
reconciler in main.py will create whatever is missing and leave everything
else alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import discord

# ---------------------------------------------------------------------------
# Permission overwrite shorthands
# ---------------------------------------------------------------------------
# Keyed by role name (must match a name in ROLES below, or the special
# "@everyone" sentinel). Values are kwargs passed straight to
# discord.PermissionOverwrite(...).

DENY_VIEW = {"view_channel": False}
ALLOW_VIEW_ONLY = {"view_channel": True, "send_messages": False, "add_reactions": False}
ALLOW_VIEW_READONLY_HISTORY = {"view_channel": True, "send_messages": False, "read_message_history": True}
ALLOW_VIEW_SEND = {"view_channel": True, "send_messages": True, "read_message_history": True}
ALLOW_VOICE = {"view_channel": True, "connect": True, "speak": True}
DENY_MIC = {"view_channel": True, "connect": True, "speak": False}
STAFF_FULL = {"view_channel": True, "send_messages": True, "manage_messages": True, "read_message_history": True}


@dataclass(frozen=True)
class RoleSpec:
    name: str
    color: discord.Color = field(default_factory=lambda: discord.Color.default())
    hoist: bool = False
    mentionable: bool = False
    permissions: discord.Permissions = field(default_factory=discord.Permissions.none)


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    type: discord.ChannelType = discord.ChannelType.text
    category: str | None = None
    topic: str | None = None
    overwrites: dict = field(default_factory=dict)  # role name -> PermissionOverwrite kwargs
    nsfw: bool = False
    position: int | None = None


@dataclass(frozen=True)
class CategorySpec:
    name: str
    position: int | None = None
    overwrites: dict = field(default_factory=dict)  # role name -> PermissionOverwrite kwargs


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
# NOTE: this is a fresh server with no pre-existing Admin/Mod role (only
# "owner"), so Admin is created here to gate Staff/Logs channel access.
#
# Staff tier hierarchy (bot-command access is layered on top via the
# permission_grants DB table — see services/permission_service.py and
# STAFF_ROLE_IDS/OWNER_ID in .env; Discord-native permissions below only
# cover what Discord itself enforces, e.g. kicking, message management):
#   Staff    — base tier, no Discord-native mod permissions. Bot access is
#              entirely grant-driven (staff_base permission).
#   Mod      — adds Discord manage_messages + moderate_members (timeouts).
#              Bot access: manage_tickets, manage_events,
#              manage_reaction_roles, manage_qotd.
#   Sr. Mod  — Mod + Discord kick_members. Bot access adds kick_members.
#   Admin    — full Discord administrator; bypasses all bot permission
#              checks via STAFF_ROLE_IDS (see is_staff() in
#              services/permission_service.py).

ROLES: list[RoleSpec] = [
    RoleSpec(
        name="Admin",
        color=discord.Color.from_str("#e63946"),
        hoist=True,
        mentionable=False,
        permissions=discord.Permissions(administrator=True),
    ),
    RoleSpec(
        name="Sr. Mod",
        color=discord.Color.from_str("#f3722c"),
        hoist=True,
        mentionable=False,
        permissions=discord.Permissions(
            kick_members=True,
            moderate_members=True,
            manage_messages=True,
            manage_nicknames=True,
        ),
    ),
    RoleSpec(
        name="Mod",
        color=discord.Color.from_str("#f9844a"),
        hoist=True,
        mentionable=False,
        permissions=discord.Permissions(
            moderate_members=True,
            manage_messages=True,
            manage_nicknames=True,
        ),
    ),
    RoleSpec(
        name="Staff",
        color=discord.Color.from_str("#f9c74f"),
        hoist=True,
        mentionable=False,
    ),
    RoleSpec(name="Verified", color=discord.Color.from_str("#f2b6cd"), hoist=True, mentionable=False),
    RoleSpec(name="Unverified", color=discord.Color.light_gray(), hoist=False, mentionable=False),
    RoleSpec(name="QOTD Ping", color=discord.Color.from_str("#f9c74f"), mentionable=True),
    RoleSpec(name="Announcement Ping", color=discord.Color.from_str("#f94144"), mentionable=True),
    RoleSpec(name="Event Ping", color=discord.Color.from_str("#90be6d"), mentionable=True),
    RoleSpec(name="Zero Build", color=discord.Color.from_str("#43aa8b")),
    RoleSpec(name="Build", color=discord.Color.from_str("#577590")),
    RoleSpec(name="Creative", color=discord.Color.from_str("#f3722c")),
    RoleSpec(name="LEGO Fortnite", color=discord.Color.from_str("#277da1")),
    # --- Separators (visual dividers, zero permissions) ---
    RoleSpec(name="━━━ AGE ━━━", color=discord.Color.from_str("#2c2f33"), hoist=False, mentionable=False),
    RoleSpec(name="━━━ REGION ━━━", color=discord.Color.from_str("#2c2f33"), hoist=False, mentionable=False),
    RoleSpec(name="━━━ TIMEZONE ━━━", color=discord.Color.from_str("#2c2f33"), hoist=False, mentionable=False),
    RoleSpec(name="━━━ COLORS ━━━", color=discord.Color.from_str("#2c2f33"), hoist=False, mentionable=False),
    # --- Notification pings ---
    RoleSpec(name="Giveaway Ping", color=discord.Color.from_str("#ffe9a8"), mentionable=True),
    RoleSpec(name="Tournament Ping", color=discord.Color.from_str("#ff9f9f"), mentionable=True),
    # --- Fortnite modes ---
    RoleSpec(name="Reload", color=discord.Color.from_str("#ffb385")),
    RoleSpec(name="Ballistic", color=discord.Color.from_str("#ff7f7f")),
    RoleSpec(name="Festival", color=discord.Color.from_str("#e3a6ff")),
    RoleSpec(name="Rocket Racing", color=discord.Color.from_str("#8fe3c0")),
    # --- Region ---
    RoleSpec(name="North America", color=discord.Color.from_str("#bcdcff")),
    RoleSpec(name="South America", color=discord.Color.from_str("#a8d8d8")),
    RoleSpec(name="Europe", color=discord.Color.from_str("#c9d6ff")),
    RoleSpec(name="Asia", color=discord.Color.from_str("#b8c9e8")),
    RoleSpec(name="Oceania", color=discord.Color.from_str("#9fd8e8")),
    # --- Timezone ---
    RoleSpec(name="EST", color=discord.Color.from_str("#d6c9ff")),
    RoleSpec(name="CST", color=discord.Color.from_str("#cbb8f0")),
    RoleSpec(name="MST", color=discord.Color.from_str("#c0a8e8")),
    RoleSpec(name="PST", color=discord.Color.from_str("#b59ce0")),
    RoleSpec(name="BST", color=discord.Color.from_str("#e0c9f5")),
    RoleSpec(name="CET", color=discord.Color.from_str("#d8bdf0")),
    RoleSpec(name="JST", color=discord.Color.from_str("#ceb0ea")),
    RoleSpec(name="AEST", color=discord.Color.from_str("#dcc4f2")),
    # --- Colors ---
    RoleSpec(name="Peach", color=discord.Color.from_str("#ffd8a8")),
    RoleSpec(name="Blush", color=discord.Color.from_str("#ffb3ba")),
    RoleSpec(name="Rose Quartz", color=discord.Color.from_str("#ffcad4")),
    RoleSpec(name="Apricot Cream", color=discord.Color.from_str("#fcd5ce")),
    RoleSpec(name="Buttercream", color=discord.Color.from_str("#f6dfa3")),
    RoleSpec(name="Honeydew", color=discord.Color.from_str("#d8f5b2")),
    RoleSpec(name="Sage Mist", color=discord.Color.from_str("#b8e8c8")),
    RoleSpec(name="Mint", color=discord.Color.from_str("#a8e6cf")),
    RoleSpec(name="Seafoam", color=discord.Color.from_str("#a0e7e5")),
    RoleSpec(name="Sky", color=discord.Color.from_str("#aee1f9")),
    RoleSpec(name="Periwinkle Mist", color=discord.Color.from_str("#b5d8f7")),
    RoleSpec(name="Wisteria", color=discord.Color.from_str("#c3b1e1")),
    RoleSpec(name="Lavender", color=discord.Color.from_str("#d9c5f1")),
    RoleSpec(name="Orchid Pink", color=discord.Color.from_str("#f0c6e0")),
    # --- Additional separators (older sections predating the 4 above) ---
    RoleSpec(name="━━━ STAFF ━━━", color=discord.Color.from_str("#2c2f33"), hoist=False, mentionable=False),
    RoleSpec(name="━━━ COMMUNITY ━━━", color=discord.Color.from_str("#2c2f33"), hoist=False, mentionable=False),
]

EVERYONE = "@everyone"

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

CATEGORIES: list[CategorySpec] = [
    CategorySpec(
        name="Staff",
        position=0,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": DENY_VIEW,
            "Unverified": DENY_VIEW,
            "Staff": STAFF_FULL,
            "Mod": STAFF_FULL,
            "Sr. Mod": STAFF_FULL,
            "Admin": STAFF_FULL,
        },
    ),
    CategorySpec(
        name="Unverified",
        position=1,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": DENY_VIEW,
            "Unverified": ALLOW_VIEW_SEND,
            "Staff": STAFF_FULL,
            "Mod": STAFF_FULL,
            "Sr. Mod": STAFF_FULL,
            "Admin": STAFF_FULL,
        },
    ),
    CategorySpec(
        name="Verification",
        position=2,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": DENY_VIEW,
            "Unverified": ALLOW_VIEW_SEND,
            "Staff": STAFF_FULL,
            "Mod": STAFF_FULL,
            "Sr. Mod": STAFF_FULL,
            "Admin": STAFF_FULL,
        },
    ),
    CategorySpec(
        name="Verify Tickets",
        position=3,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": DENY_VIEW,
            "Unverified": DENY_VIEW,
            "Staff": STAFF_FULL,
            "Mod": STAFF_FULL,
            "Sr. Mod": STAFF_FULL,
            "Admin": STAFF_FULL,
        },
    ),
    CategorySpec(
        name="Help",
        position=4,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": DENY_VIEW,
            "Unverified": DENY_VIEW,
            "Staff": STAFF_FULL,
            "Mod": STAFF_FULL,
            "Sr. Mod": STAFF_FULL,
            "Admin": STAFF_FULL,
        },
    ),
    CategorySpec(
        name="Information",
        position=6,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": ALLOW_VIEW_READONLY_HISTORY,
        },
    ),
    CategorySpec(
        name="Gamer Girls Hub",
        position=7,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": ALLOW_VIEW_SEND,
        },
    ),
    CategorySpec(
        name="Zero Build",
        position=8,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": ALLOW_VIEW_SEND,
        },
    ),
    CategorySpec(
        name="Build",
        position=9,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": ALLOW_VIEW_SEND,
        },
    ),
    CategorySpec(
        name="Miscellaneous",
        position=10,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": ALLOW_VIEW_SEND,
        },
    ),
    CategorySpec(
        name="Voice",
        position=11,
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": ALLOW_VOICE,
        },
    ),
]

# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
# Channel-level overwrites are only needed where a channel deviates from its
# category's inherited permissions (e.g. read-only announcement channels,
# the verification channel staying open to Unverified, staff-only logs).

CHANNELS: list[ChannelSpec] = [
    # --- Information ---
    ChannelSpec(
        name="welcome",
        category="Information",
        topic="Welcome to the server! Start here.",
        overwrites={EVERYONE: ALLOW_VIEW_READONLY_HISTORY},
    ),
    ChannelSpec(
        name="rules",
        category="Information",
        topic="Server rules — read before participating.",
        overwrites={EVERYONE: ALLOW_VIEW_READONLY_HISTORY},
    ),
    ChannelSpec(
        name="announcements",
        category="Information",
        topic="Official server announcements.",
        overwrites={EVERYONE: ALLOW_VIEW_READONLY_HISTORY},
    ),
    ChannelSpec(
        name="self-roles",
        category="Information",
        topic="Pick your notification and game mode roles here.",
        overwrites={
            EVERYONE: DENY_VIEW,
            "Verified": ALLOW_VIEW_READONLY_HISTORY,
        },
    ),
    # --- Verification ---
    ChannelSpec(
        name="verification",
        category="Verification",
        topic="Verify here to unlock the rest of the server.",
        overwrites={
            EVERYONE: DENY_VIEW,
            "Unverified": ALLOW_VIEW_SEND,
            "Verified": ALLOW_VIEW_READONLY_HISTORY,
        },
    ),
    # --- Gamer Girls Hub (formerly "General") ---
    ChannelSpec(name="general", category="Gamer Girls Hub", topic="General chat."),
    ChannelSpec(name="media", category="Gamer Girls Hub", topic="Share clips, art, and photos."),
    ChannelSpec(name="bots", category="Gamer Girls Hub", topic="Bot commands and spam."),
    ChannelSpec(
        name="qotd",
        category="Gamer Girls Hub",
        topic="Question of the day.",
        overwrites={EVERYONE: ALLOW_VIEW_READONLY_HISTORY, "Verified": ALLOW_VIEW_SEND},
    ),
    # --- Zero Build ---
    ChannelSpec(name="zb-chat", category="Zero Build", topic="Zero Build chat."),
    ChannelSpec(name="zb-lfg", category="Zero Build", topic="Looking for group — Zero Build."),
    # --- Build ---
    ChannelSpec(name="build-chat", category="Build", topic="Build chat."),
    ChannelSpec(name="build-lfg", category="Build", topic="Looking for group — Build."),
    # --- Miscellaneous ---
    ChannelSpec(name="creative", category="Miscellaneous", topic="Creative mode."),
    ChannelSpec(name="lego-fortnite", category="Miscellaneous", topic="LEGO Fortnite chat."),
    ChannelSpec(name="other-games", category="Miscellaneous", topic="Other games chat."),
    # --- Voice ---
    ChannelSpec(name="General VC", type=discord.ChannelType.voice, category="Voice"),
    ChannelSpec(
        name="No Mic VC",
        type=discord.ChannelType.voice,
        category="Voice",
        overwrites={EVERYONE: DENY_VIEW, "Verified": DENY_MIC},
    ),
    ChannelSpec(name="Music VC", type=discord.ChannelType.voice, category="Voice"),
    # --- Staff ---
    ChannelSpec(
        name="staff-chat",
        category="Staff",
        topic="Staff-only discussion.",
        overwrites={EVERYONE: DENY_VIEW},
    ),
    ChannelSpec(
        name="staff-announcements",
        category="Staff",
        topic="Leadership announcements for staff.",
        overwrites={
            EVERYONE: DENY_VIEW,
            "Staff": ALLOW_VIEW_READONLY_HISTORY,
            "Mod": ALLOW_VIEW_READONLY_HISTORY,
            "Sr. Mod": ALLOW_VIEW_READONLY_HISTORY,
            "Admin": STAFF_FULL,
        },
    ),
    ChannelSpec(name="Staff Voice", type=discord.ChannelType.voice, category="Staff"),
    ChannelSpec(
        name="moderation-log",
        category="Staff",
        topic="Moderation action log.",
        overwrites={EVERYONE: DENY_VIEW},
    ),
    ChannelSpec(
        name="join-leave",
        category="Staff",
        topic="Member join/leave log.",
        overwrites={EVERYONE: DENY_VIEW},
    ),
    # --- Unverified ---
    ChannelSpec(
        name="how-to-verify",
        category="Unverified",
        topic=(
            "How to get verified: 1) Head to #verification 2) Click Verify Me "
            "3) Chat briefly with a staff member in your private ticket "
            "4) You're in! (Once verified, check #help if you ever need general help.)"
        ),
    ),
    ChannelSpec(
        name="unverified-lounge",
        category="Unverified",
        topic="Waiting to be verified? Hang out here or head to voice for verification.",
    ),
    ChannelSpec(name="Verification VC", type=discord.ChannelType.voice, category="Unverified"),
    # --- Help ---
    ChannelSpec(
        name="help",
        category="Help",
        topic="Need help? Open a ticket via the Get Help button below, or ask here.",
        overwrites={
            EVERYONE: DENY_VIEW,
            "Unverified": DENY_VIEW,
            "Verified": ALLOW_VIEW_READONLY_HISTORY,
            "Staff": STAFF_FULL,
            "Mod": STAFF_FULL,
            "Sr. Mod": STAFF_FULL,
            "Admin": STAFF_FULL,
        },
    ),
]
