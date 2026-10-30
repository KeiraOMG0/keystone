"""
Template placeholders: `{name}` tokens replaced when a template is rendered
(core/renderer.py) or a birthday message is built (cogs/birthdays.py).

Syntax (2026-10-30):
  {name}                plain placeholder, e.g. {mention}, {server}
  {name:argument}       placeholders that take an argument, e.g. {channel:general},
                        {role:Moderator}, {joindate:R}, {random:Hi;Hello;Hey}
  {name|fallback}       if the value is empty, use the fallback instead: another
                        placeholder name ({nickname|user}) or plain text
                        ({reason|No reason given}). Works with an argument too.
Unknown names are left exactly as written. A placeholder that fails renders
empty (or its fallback); rendering never raises.

Every placeholder is registered with metadata (category, where it has a value,
description) so the catalogue can be listed: the dashboard template editor's
Placeholders panel (GET /api/templates/placeholders) and `/template
placeholders` both read `catalogue()`, and examples are rendered from the same
functions with `sample_context()`. Add new ones with `register_placeholder` /
`register_arg_placeholder` below, with a description; nothing else is needed.

Context data: `RenderContext.member` (the member the message is about),
`.actor` (whoever did the action: moderator, ticket claimer, verifier),
`.channel`, `.ticket_number`, and `.extra` for feature data:
  role, target_user, target_id, reason, action, case_id, duration_minutes,
  expires_at, delete_days, warning_id, warning_count, kick_number, purge_count,
  purge_channel, purge_filter, source.
"""

from __future__ import annotations

import datetime
import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import discord


@dataclass
class RenderContext:
    guild: discord.Guild
    member: discord.Member | None = None
    channel: discord.abc.GuildChannel | None = None
    actor: discord.Member | None = None
    ticket_number: int | None = None
    extra: dict = field(default_factory=dict)


PlaceholderFn = Callable[[RenderContext], Any]
ArgPlaceholderFn = Callable[[RenderContext, str], Any]

_REGISTRY: dict[str, PlaceholderFn] = {}
_ARG_REGISTRY: dict[str, ArgPlaceholderFn] = {}
_CATALOGUE: list[dict[str, str]] = []

# Where a placeholder has a value (shown in the catalogue).
ANYWHERE = "Anywhere"
MEMBER = "Messages about a member (welcome, boost, verify, tickets, role events, logs)"
MODERATION = "Moderation logs"
ACTOR = "Moderation logs, ticket claimed/closed, verification"
TICKETS = "Ticket messages"
ROLE_EVENTS = "Role added / removed events"
PURGE = "Purge log"
KICK = "Kick log"

CATEGORIES = ("Member", "Server", "Channel", "Date & time", "Moderation", "Staff", "Tickets", "Roles", "Lookups & extras")

_PATTERN = re.compile(r"\{(\w+)(?::([^{}|]*))?(?:\|([^{}]*))?\}")
_TIME_STYLES = {"t", "T", "d", "D", "f", "F", "R"}


def register_placeholder(
    name: str,
    fn: PlaceholderFn,
    *,
    category: str = "Other",
    description: str = "",
    where: str = ANYWHERE,
    example: str | None = None,
) -> None:
    """Register `{name}`. `example` is the syntax shown in the catalogue (default `{name}`)."""
    _REGISTRY[name] = fn
    _CATALOGUE.append({
        "name": name, "syntax": example or "{" + name + "}",
        "category": category, "description": description, "where": where,
    })


def register_arg_placeholder(
    name: str,
    fn: ArgPlaceholderFn,
    *,
    category: str,
    description: str,
    example: str,
    where: str = ANYWHERE,
) -> None:
    """Register `{name:argument}`. If no plain `{name}` exists, `{name}` calls fn with ""."""
    _ARG_REGISTRY[name] = fn
    _CATALOGUE.append({
        "name": name, "syntax": example, "category": category, "description": description, "where": where,
    })


def resolve_text(text: str | None, ctx: RenderContext) -> str:
    if text is None:
        return ""

    def value_of(name: str, arg: str | None) -> str | None:
        if arg is not None:
            fn_arg = _ARG_REGISTRY.get(name)
            if fn_arg is None:
                return None
            return _safe(lambda: fn_arg(ctx, arg.strip()))
        fn = _REGISTRY.get(name)
        if fn is not None:
            return _safe(lambda: fn(ctx))
        fn_arg = _ARG_REGISTRY.get(name)
        if fn_arg is not None:
            return _safe(lambda: fn_arg(ctx, ""))
        return None

    def replacer(match: re.Match) -> str:
        name, arg, fallback = match.group(1), match.group(2), match.group(3)
        value = value_of(name, arg)
        if value is None:
            return match.group(0)  # unknown placeholder: leave it as written
        if not value and fallback is not None:
            fb = fallback.strip()
            alt = value_of(fb, None) if re.fullmatch(r"\w+", fb) else None
            value = alt if alt else fallback
        return value

    return _PATTERN.sub(replacer, text)


def _safe(call: Callable[[], Any]) -> str:
    try:
        value = call()
    except Exception:
        return ""
    return "" if value is None else str(value)


def catalogue() -> list[dict[str, str]]:
    """Every registered placeholder form, in registration (= category) order."""
    return [dict(entry) for entry in _CATALOGUE]


def sample_context(
    guild: discord.Guild,
    member: discord.Member | None = None,
    channel: discord.abc.GuildChannel | None = None,
) -> RenderContext:
    """A context filled with example data (the viewer as member AND actor), for
    previews and catalogue examples. Never used to send a real message."""
    now = discord.utils.utcnow()
    channel = channel or getattr(guild, "system_channel", None) or getattr(guild, "rules_channel", None) or next(
        iter(getattr(guild, "text_channels", []) or []), None
    )
    role = getattr(member, "top_role", None) if member is not None else None
    if role is not None and getattr(role, "is_default", lambda: False)():
        role = None
    return RenderContext(
        guild=guild,
        member=member,
        channel=channel,
        actor=member,
        ticket_number=42,
        extra={
            "sample": True,
            "role": role,
            "reason": "Breaking rule 3 (example)",
            "action": "Timeout",
            "case_id": 128,
            "duration_minutes": 90,
            "expires_at": now + datetime.timedelta(minutes=90),
            "delete_days": 1,
            "warning_id": 7,
            "warning_count": 2,
            "kick_number": 101,
            "purge_count": 25,
            "purge_channel": channel,
            "purge_filter": "messages from the last 15 minutes",
            "source": "dashboard",
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user(ctx: RenderContext):
    """The member the message is about, or the target user of a moderation
    action on someone who is not (or no longer) a member."""
    return ctx.member if ctx.member is not None else ctx.extra.get("target_user")


def _uid(ctx: RenderContext) -> int | None:
    u = _user(ctx)
    if u is not None:
        return u.id
    return ctx.extra.get("target_id")


def _joined(ctx: RenderContext):
    return getattr(ctx.member, "joined_at", None) if ctx.member is not None else None


def _dt(value, style: str = "f") -> str:
    if value is None:
        return ""
    style = style if style in _TIME_STYLES else "f"
    return discord.utils.format_dt(value, style)


def _date(value) -> str:
    return value.strftime("%b %d, %Y") if value else ""


def _days_since(value) -> str:
    if value is None:
        return ""
    return str(max((discord.utils.utcnow() - value).days, 0))


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def human_duration(minutes: int | None) -> str:
    """90 -> "1 hour 30 minutes", 1440 -> "1 day"."""
    if minutes is None:
        return ""
    minutes = int(minutes)
    parts = []
    for unit, size in (("week", 10080), ("day", 1440), ("hour", 60), ("minute", 1)):
        count, minutes = divmod(minutes, size)
        if count:
            parts.append(f"{count} {unit}{'' if count == 1 else 's'}")
    return " ".join(parts[:2]) or "0 minutes"


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _clip(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _member_roles(ctx: RenderContext) -> list:
    roles = list(getattr(ctx.member, "roles", None) or [])
    return [r for r in reversed(roles) if not r.is_default()]  # highest first, no @everyone


def _role(ctx: RenderContext):
    return ctx.extra.get("role")


def _join_position(ctx: RenderContext) -> int | None:
    joined = _joined(ctx)
    if joined is None:
        return None
    members = getattr(ctx.guild, "members", None) or []
    return 1 + sum(1 for m in members if getattr(m, "joined_at", None) and m.joined_at < joined)


def _find(items, query: str):
    """By ID, then exact name, then case-insensitive name, then name contains."""
    query = query.strip().lstrip("#@")
    if not query:
        return None
    items = list(items or [])
    if query.isdigit():
        for item in items:
            if item.id == int(query):
                return item
    folded = query.casefold()
    for test in (
        lambda n: n == query,
        lambda n: n.casefold() == folded,
        lambda n: folded in n.casefold(),
    ):
        for item in items:
            if test(getattr(item, "name", "")):
                return item
    return None


def _now() -> datetime.datetime:
    return discord.utils.utcnow()


# ---------------------------------------------------------------------------
# Member
# ---------------------------------------------------------------------------

_M = "Member"
register_placeholder("user", lambda ctx: _user(ctx).display_name if _user(ctx) else "", category=_M, where=MEMBER,
                     description="Their display name (server nickname if set, else their Discord display name).")
register_placeholder("displayname", lambda ctx: _user(ctx).display_name if _user(ctx) else "", category=_M, where=MEMBER,
                     description="Same as {user}.")
register_placeholder("username", lambda ctx: str(_user(ctx).name) if _user(ctx) else "", category=_M, where=MEMBER,
                     description="Their unique Discord username, e.g. sunny.day.")
register_placeholder("globalname", lambda ctx: (getattr(_user(ctx), "global_name", None) or "") if _user(ctx) else "",
                     category=_M, where=MEMBER, description="Their Discord display name, ignoring the server nickname.")
register_placeholder("nickname", lambda ctx: (getattr(ctx.member, "nick", None) or "") if ctx.member else "",
                     category=_M, where=MEMBER, description="Their server nickname (blank if none). Try {nickname|user}.")
register_placeholder("mention", lambda ctx: f"<@{_uid(ctx)}>" if _uid(ctx) else "", category=_M, where=MEMBER,
                     description="Mentions (pings) them.")
register_placeholder("id", lambda ctx: str(_uid(ctx) or ""), category=_M, where=MEMBER, description="Their user ID.")
register_placeholder("userid", lambda ctx: str(_uid(ctx) or ""), category=_M, where=MEMBER, description="Same as {id}.")
register_placeholder("avatar", lambda ctx: str(_user(ctx).display_avatar.url) if _user(ctx) else "", category=_M,
                     where=MEMBER, description="Link to their avatar (use in an icon, thumbnail or image field).")
register_placeholder("joindate", lambda ctx: _date(_joined(ctx)), category=_M, where=MEMBER,
                     description="The date they joined the server, e.g. Oct 30, 2026.")
register_arg_placeholder("joindate", lambda ctx, a: _dt(_joined(ctx), a) if a else _date(_joined(ctx)), category=_M,
                         where=MEMBER, example="{joindate:R}",
                         description="When they joined, as a Discord timestamp that shows in each reader's own time "
                                     "zone. Styles: t, T, d, D, f, F, R (R = \"3 days ago\").")
register_placeholder("joindaterelative", lambda ctx: _dt(_joined(ctx), "R"), category=_M, where=MEMBER,
                     description="When they joined, as \"3 days ago\".")
register_placeholder("joindays", lambda ctx: _days_since(_joined(ctx)), category=_M, where=MEMBER,
                     description="How many days ago they joined.")
register_placeholder("joinposition", lambda ctx: str(_join_position(ctx) or ""), category=_M, where=MEMBER,
                     description="Which member they are by join date among current members, e.g. 43.")
register_placeholder("joinpositionordinal",
                     lambda ctx: ordinal(_join_position(ctx)) if _join_position(ctx) else "", category=_M,
                     where=MEMBER, description="The same as an ordinal, e.g. 43rd.")
register_placeholder("accountage", lambda ctx: _date(getattr(_user(ctx), "created_at", None)), category=_M,
                     where=MEMBER, description="The date their Discord account was created.")
register_arg_placeholder("accountage", lambda ctx, a: _dt(getattr(_user(ctx), "created_at", None), a or "D"),
                         category=_M, where=MEMBER, example="{accountage:R}",
                         description="Account creation as a Discord timestamp (styles t, T, d, D, f, F, R).")
register_placeholder("accountagerelative", lambda ctx: _dt(getattr(_user(ctx), "created_at", None), "R"),
                     category=_M, where=MEMBER, description="Account age as \"2 years ago\".")
register_placeholder("accountagedays", lambda ctx: _days_since(getattr(_user(ctx), "created_at", None)),
                     category=_M, where=MEMBER, description="How many days old their account is.")
register_placeholder("toprole", lambda ctx: ctx.member.top_role.name if getattr(ctx.member, "top_role", None) else "",
                     category=_M, where=MEMBER, description="Their highest role's name.")
register_placeholder("topmention",
                     lambda ctx: ctx.member.top_role.mention
                     if getattr(ctx.member, "top_role", None) and not ctx.member.top_role.is_default() else "",
                     category=_M, where=MEMBER, description="Their highest role, as a mention.")
register_placeholder("usercolor",
                     lambda ctx: str(ctx.member.colour) if ctx.member is not None and ctx.member.colour.value else "",
                     category=_M, where=MEMBER, description="Their name colour as hex, e.g. #f2b6cd.")
register_placeholder("rolecount", lambda ctx: str(len(_member_roles(ctx))) if ctx.member else "", category=_M,
                     where=MEMBER, description="How many roles they have.")
register_placeholder("roles", lambda ctx: _clip(", ".join(r.mention for r in _member_roles(ctx))), category=_M,
                     where=MEMBER, description="All their roles as mentions, highest first.")
register_placeholder("rolenames", lambda ctx: _clip(", ".join(r.name for r in _member_roles(ctx))), category=_M,
                     where=MEMBER, description="All their role names, highest first.")
register_placeholder("isbooster", lambda ctx: _yes_no(bool(getattr(ctx.member, "premium_since", None)))
                     if ctx.member else "", category=_M, where=MEMBER, description="Yes or No: are they boosting.")
register_placeholder("boostingsince", lambda ctx: _dt(getattr(ctx.member, "premium_since", None), "R")
                     if ctx.member else "", category=_M, where=MEMBER,
                     description="How long they have boosted (\"2 months ago\"); blank if not boosting.")
register_placeholder("isbot", lambda ctx: _yes_no(bool(getattr(_user(ctx), "bot", False))) if _user(ctx) else "",
                     category=_M, where=MEMBER, description="Yes or No: is this a bot account.")
register_placeholder("timeoutends", lambda ctx: _dt(getattr(ctx.member, "timed_out_until", None), "R")
                     if ctx.member else "", category=_M, where=MEMBER,
                     description="When their current timeout ends (\"in 2 hours\"); blank if none.")

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

_S = "Server"
register_placeholder("server", lambda ctx: ctx.guild.name, category=_S, description="The server name.")
register_placeholder("serverid", lambda ctx: str(ctx.guild.id), category=_S, description="The server ID.")
register_placeholder("servericon", lambda ctx: str(ctx.guild.icon.url) if ctx.guild.icon else "", category=_S,
                     description="Link to the server icon.")
register_placeholder("serverbanner", lambda ctx: str(ctx.guild.banner.url) if ctx.guild.banner else "",
                     category=_S, description="Link to the server banner (blank if none).")
register_placeholder("serverdescription", lambda ctx: ctx.guild.description or "", category=_S,
                     description="The server description from Server Settings.")
register_placeholder("servercreated", lambda ctx: _date(ctx.guild.created_at), category=_S,
                     description="The date the server was created.")
register_arg_placeholder("servercreated", lambda ctx, a: _dt(ctx.guild.created_at, a or "D"), category=_S,
                         example="{servercreated:R}",
                         description="Server creation as a Discord timestamp (styles t, T, d, D, f, F, R).")
register_placeholder("serverage", lambda ctx: _days_since(ctx.guild.created_at), category=_S,
                     description="How many days old the server is.")
register_placeholder("membercount", lambda ctx: str(ctx.guild.member_count), category=_S,
                     description="How many members the server has (bots included).")
register_placeholder("membercountordinal", lambda ctx: ordinal(ctx.guild.member_count or 0), category=_S,
                     description="The member count as an ordinal, e.g. 43rd (\"You are our {membercountordinal} member\").")
register_placeholder("humancount", lambda ctx: str(sum(1 for m in ctx.guild.members if not m.bot)), category=_S,
                     description="How many members are people (no bots).")
register_placeholder("botcount", lambda ctx: str(sum(1 for m in ctx.guild.members if m.bot)), category=_S,
                     description="How many bots are in the server.")
register_placeholder("boostcount", lambda ctx: str(ctx.guild.premium_subscription_count or 0), category=_S,
                     description="How many boosts the server has.")
register_placeholder("boostercount", lambda ctx: str(len(ctx.guild.premium_subscribers)), category=_S,
                     description="How many members are boosting.")
register_placeholder("boosttier", lambda ctx: str(ctx.guild.premium_tier), category=_S, description="Boost level, 0 to 3.")
register_placeholder("boosttiername",
                     lambda ctx: {0: "None", 1: "Level 1", 2: "Level 2", 3: "Level 3"}.get(ctx.guild.premium_tier, "None"),
                     category=_S, description="Boost level as a label, e.g. Level 2.")
register_placeholder("boostsneeded",
                     lambda ctx: str(max({0: 2, 1: 7, 2: 14}.get(ctx.guild.premium_tier, 14)
                                         - (ctx.guild.premium_subscription_count or 0), 0))
                     if ctx.guild.premium_tier < 3 else "0",
                     category=_S, description="Boosts still needed for the next boost level (0 at Level 3).")
register_placeholder("owner", lambda ctx: ctx.guild.owner.display_name if ctx.guild.owner else "", category=_S,
                     description="The server owner's name.")
register_placeholder("ownermention", lambda ctx: ctx.guild.owner.mention if ctx.guild.owner else "", category=_S,
                     description="Mentions the server owner.")
register_placeholder("serverrolecount", lambda ctx: str(len(ctx.guild.roles)), category=_S,
                     description="How many roles the server has.")
register_placeholder("channelcount", lambda ctx: str(len(ctx.guild.channels)), category=_S,
                     description="How many channels the server has (categories included).")
register_placeholder("textchannelcount", lambda ctx: str(len(ctx.guild.text_channels)), category=_S,
                     description="How many text channels.")
register_placeholder("voicechannelcount", lambda ctx: str(len(ctx.guild.voice_channels)), category=_S,
                     description="How many voice channels.")
register_placeholder("categorycount", lambda ctx: str(len(ctx.guild.categories)), category=_S,
                     description="How many channel categories.")
register_placeholder("emojicount", lambda ctx: str(len(ctx.guild.emojis)), category=_S,
                     description="How many custom emojis.")
register_placeholder("stickercount", lambda ctx: str(len(ctx.guild.stickers)), category=_S,
                     description="How many custom stickers.")
register_placeholder("ruleschannel", lambda ctx: ctx.guild.rules_channel.mention if ctx.guild.rules_channel else "",
                     category=_S, description="Link to the rules channel set in Server Settings.")
register_placeholder("systemchannel", lambda ctx: ctx.guild.system_channel.mention if ctx.guild.system_channel else "",
                     category=_S, description="Link to the system messages channel.")
register_placeholder("verificationlevel", lambda ctx: str(ctx.guild.verification_level).replace("_", " ").title(),
                     category=_S, description="Discord's verification level setting, e.g. High.")

# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------

_C = "Channel"
register_placeholder("channel", lambda ctx: ctx.channel.mention if ctx.channel else "", category=_C,
                     description="Link to the channel the message is sent in.")
register_arg_placeholder("channel", lambda ctx, a: c.mention if (c := _find(
    [x for x in ctx.guild.channels if not isinstance(x, discord.CategoryChannel)], a)) else "",
                         category=_C, example="{channel:general}",
                         description="Link to any channel, by name or ID. The name can be part of it, so "
                                     "{channel:general} finds \"💬┊general\".")
register_placeholder("channelname", lambda ctx: ctx.channel.name if ctx.channel else "", category=_C,
                     description="The name of the channel the message is sent in.")
register_placeholder("channelid", lambda ctx: str(ctx.channel.id) if ctx.channel else "", category=_C,
                     description="Its ID.")
register_placeholder("channeltopic", lambda ctx: getattr(ctx.channel, "topic", None) or "", category=_C,
                     description="Its topic.")
register_placeholder("categoryname", lambda ctx: ctx.channel.category.name
                     if ctx.channel is not None and getattr(ctx.channel, "category", None) else "", category=_C,
                     description="The category it is in.")

# ---------------------------------------------------------------------------
# Date & time (Discord timestamps show in each reader's own time zone)
# ---------------------------------------------------------------------------

_T = "Date & time"
register_placeholder("timestamp", lambda ctx: _dt(_now(), "F"), category=_T,
                     description="Now, as a full date and time in the reader's time zone.")
register_placeholder("timestamp_relative", lambda ctx: _dt(_now(), "R"), category=_T,
                     description="Now, as \"just now\" (updates live in Discord).")
register_arg_placeholder("time", lambda ctx, a: _dt(_now(), a or "t"), category=_T, example="{time:F}",
                         description="Now as a Discord timestamp. Styles: t = 9:41 PM, T = 9:41:30 PM, "
                                     "d = 10/30/2026, D = October 30, 2026, f = date + time, F = with weekday, "
                                     "R = relative. {time} alone = t.")
register_arg_placeholder("date", lambda ctx, a: _now().strftime(a) if a else _dt(_now(), "D"), category=_T,
                         example="{date:%A %d %B}",
                         description="Today's date. {date} alone shows it in the reader's time zone; with a "
                                     "format (%A weekday, %d day, %B month, %Y year, %H:%M time) it is written "
                                     "out in UTC.")
register_placeholder("unix", lambda ctx: str(int(_now().timestamp())), category=_T,
                     description="Now as a Unix timestamp (for <t:...> codes).")
register_placeholder("weekday", lambda ctx: _now().strftime("%A"), category=_T, description="Today's weekday (UTC).")
register_placeholder("year", lambda ctx: str(_now().year), category=_T, description="The current year.")

# ---------------------------------------------------------------------------
# Moderation (mod logs). The member placeholders above work there too, also
# for someone who is no longer in the server (ban / unban by ID).
# ---------------------------------------------------------------------------

_MOD = "Moderation"


def _reason(ctx: RenderContext) -> str:
    if "reason" not in ctx.extra:
        return ""
    return ctx.extra.get("reason") or "No reason given"


register_placeholder("reason", _reason, category=_MOD, where=MODERATION,
                     description="Why it happened (\"No reason given\" if the moderator left it empty).")
register_placeholder("action", lambda ctx: ctx.extra.get("action") or "", category=_MOD, where=MODERATION,
                     description="What happened: Ban, Unban, Kick, Timeout, Timeout removed, Warning, "
                                 "Warning removed, Purge.")
register_placeholder("caseid", lambda ctx: str(ctx.extra.get("case_id") or ""), category=_MOD, where=MODERATION,
                     description="The case number of this action in the moderation history.")
register_placeholder("duration", lambda ctx: human_duration(ctx.extra.get("duration_minutes")), category=_MOD,
                     where=MODERATION, description="How long a timeout lasts, e.g. 1 hour 30 minutes.")
register_placeholder("durationminutes", lambda ctx: str(ctx.extra.get("duration_minutes") or ""), category=_MOD,
                     where=MODERATION, description="The timeout length in minutes.")
register_placeholder("expires", lambda ctx: _dt(ctx.extra.get("expires_at"), "R"), category=_MOD, where=MODERATION,
                     description="When the timeout ends, as \"in 2 hours\".")
register_arg_placeholder("expires", lambda ctx, a: _dt(ctx.extra.get("expires_at"), a or "R"), category=_MOD,
                         where=MODERATION, example="{expires:f}",
                         description="When the timeout ends, in any timestamp style.")
register_placeholder("deletedays", lambda ctx: str(ctx.extra["delete_days"]) if "delete_days" in ctx.extra else "",
                     category=_MOD, where=MODERATION, description="How many days of their messages a ban deleted.")
register_placeholder("warningid", lambda ctx: str(ctx.extra.get("warning_id") or ""), category=_MOD,
                     where=MODERATION, description="The warning number (warn / warning removed logs).")
register_placeholder("warningcount", lambda ctx: str(ctx.extra["warning_count"])
                     if ctx.extra.get("warning_count") is not None else "", category=_MOD, where=MODERATION,
                     description="How many warnings they have now.")
register_placeholder("kicknumber", lambda ctx: str(ctx.extra.get("kick_number") or ""), category=_MOD, where=KICK,
                     description="The running kick counter (the number in kicks.log).")
register_placeholder("purgecount", lambda ctx: str(ctx.extra["purge_count"])
                     if ctx.extra.get("purge_count") is not None else "", category=_MOD, where=PURGE,
                     description="How many messages a purge deleted.")
register_placeholder("purgechannel", lambda ctx: ctx.extra["purge_channel"].mention
                     if ctx.extra.get("purge_channel") is not None else "", category=_MOD, where=PURGE,
                     description="The channel that was purged.")
register_placeholder("purgefilter", lambda ctx: ctx.extra.get("purge_filter") or "", category=_MOD, where=PURGE,
                     description="What was purged, e.g. \"last 50 messages from @user\".")
register_placeholder("source", lambda ctx: {"dashboard": "Dashboard", "command": "Discord command",
                                            "automatic": "Automatic", "button": "Button"}.get(
                         ctx.extra.get("source") or "", ctx.extra.get("source") or ""),
                     category=_MOD, where=ACTOR, description="Where it was done: Dashboard, Discord command, "
                                                             "Button or Automatic.")

# ---------------------------------------------------------------------------
# Staff (whoever did the action)
# ---------------------------------------------------------------------------

_ST = "Staff"
register_placeholder("moderator", lambda ctx: ctx.actor.display_name if ctx.actor else "", category=_ST, where=ACTOR,
                     description="Name of whoever did it (moderator, ticket claimer, verifier).")
register_placeholder("moderatormention", lambda ctx: ctx.actor.mention if ctx.actor else "", category=_ST,
                     where=ACTOR, description="Mentions them.")
register_placeholder("moderatorid", lambda ctx: str(ctx.actor.id) if ctx.actor else "", category=_ST, where=ACTOR,
                     description="Their user ID.")
register_placeholder("moderatorusername", lambda ctx: str(ctx.actor.name) if ctx.actor else "", category=_ST,
                     where=ACTOR, description="Their username.")
register_placeholder("moderatoravatar", lambda ctx: str(ctx.actor.display_avatar.url) if ctx.actor else "",
                     category=_ST, where=ACTOR, description="Link to their avatar.")
register_placeholder("staff", lambda ctx: ctx.actor.display_name if ctx.actor else "", category=_ST, where=ACTOR,
                     description="Same as {moderator} (reads better in ticket and verify messages).")
register_placeholder("staffmention", lambda ctx: ctx.actor.mention if ctx.actor else "", category=_ST, where=ACTOR,
                     description="Same as {moderatormention}.")

# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

_TK = "Tickets"
register_placeholder("ticketnumber", lambda ctx: str(ctx.ticket_number) if ctx.ticket_number is not None else "",
                     category=_TK, where=TICKETS, description="The ticket number, e.g. 42.")
register_placeholder("ticketnumberpadded", lambda ctx: f"{ctx.ticket_number:04d}"
                     if ctx.ticket_number is not None else "", category=_TK, where=TICKETS,
                     description="The ticket number with zeros, e.g. 0042.")
register_placeholder("ticketchannel", lambda ctx: ctx.channel.mention if ctx.channel else "", category=_TK,
                     where=TICKETS, description="Link to the ticket channel (same as {channel}).")

# ---------------------------------------------------------------------------
# Roles (role added / removed events)
# ---------------------------------------------------------------------------

_R = "Roles"
register_placeholder("role", lambda ctx: _role(ctx).name if _role(ctx) else "", category=_R, where=ROLE_EVENTS,
                     description="The role that was added or removed, by name.")
register_arg_placeholder("role", lambda ctx, a: r.mention if (r := _find(
    [x for x in ctx.guild.roles if not x.is_default()], a)) else "", category=_R, example="{role:Moderator}",
    description="Mentions any role by name or ID (part of the name works). Never @everyone.")
register_placeholder("rolemention", lambda ctx: _role(ctx).mention if _role(ctx) else "", category=_R,
                     where=ROLE_EVENTS, description="The role that was added or removed, as a mention.")
register_placeholder("roleid", lambda ctx: str(_role(ctx).id) if _role(ctx) else "", category=_R, where=ROLE_EVENTS,
                     description="Its ID.")
register_placeholder("rolecolor", lambda ctx: str(_role(ctx).colour) if _role(ctx) and _role(ctx).colour.value else "",
                     category=_R, where=ROLE_EVENTS, description="Its colour as hex.")
register_placeholder("rolemembers", lambda ctx: str(len(_role(ctx).members)) if _role(ctx) else "", category=_R,
                     where=ROLE_EVENTS, description="How many members have it now.")

# ---------------------------------------------------------------------------
# Lookups & extras
# ---------------------------------------------------------------------------

_X = "Lookups & extras"
register_arg_placeholder("member", lambda ctx, a: m.mention if (m := _find(ctx.guild.members, a)) else "",
                         category=_X, example="{member:sunny.day}",
                         description="Mentions any member by username or ID.")
register_arg_placeholder("emoji", lambda ctx, a: str(e) if (e := _find(ctx.guild.emojis, a.strip(":"))) else "",
                         category=_X, example="{emoji:heart}",
                         description="Shows one of the server's custom emojis by name.")
register_arg_placeholder("random", lambda ctx, a: random.choice([p.strip() for p in a.split(";") if p.strip()] or [""]),
                         category=_X, example="{random:Hi;Hello;Hey there}",
                         description="Picks one of the options (separated by ;) at random each time.")
register_placeholder("newline", lambda ctx: "\n", category=_X,
                     description="A line break (handy in one-line fields).")
