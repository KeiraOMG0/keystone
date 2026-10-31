"""
services/member_hub_service.py - the member hub (/hub) view of ONE signed-in user.

The dashboard backend calls this (internal GET /guilds/{id}/hub/{user_id}) only
with the id its Discord login proved, so everything here is the person's OWN
data plus things any member can already see in Discord:

  - profile: display name, avatar, joined/created dates, roles, booster, timeout
  - standing: warning COUNT and dates (never the reason or the moderator) and an
    active timeout, like Discord's own "Account Standing" page
  - self-roles: every POSTED reaction-role panel and which of its roles they hold
    (toggled through ReactionRoleService.member_toggle, same rules as the buttons)
  - birthday: their own entry plus /birthday show-nearest's list
  - their open tickets (link to the channel), Discord scheduled events, the last
    QOTD, what music is playing
  - server: name, icon, member/online counts, boost tier

Someone who is NOT in the guild gets only their own state: removed by the trap
channel (their honeypot appeal status, from HoneypotService.appeal_status_for_user),
banned, or simply not a member. Never anyone else's data.

Every part is best effort: a feature that is not loaded (cog missing) or fails
is left out (null / []) and named in `unavailable`, so one broken feature never
blanks the hub.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import discord

import config

log = logging.getLogger(__name__)

MAX_EVENTS = 5
MAX_WARNINGS_LISTED = 10
STATUS_MEMBER = "member"
STATUS_TRAP = "trap"  # removed by the honeypot channel: can appeal
STATUS_BANNED = "banned"
STATUS_NOT_MEMBER = "not_member"


class HubError(Exception):
    """A refused self-role toggle: `code` goes on the wire, `message` is for people."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _asset(asset: Any) -> str | None:
    url = getattr(asset, "url", None)
    return str(url) if url else None


def _colour(role: Any) -> str | None:
    value = getattr(getattr(role, "colour", None), "value", 0) or 0
    return f"#{value:06x}" if value else None


def _role(role: Any) -> dict:
    return {"id": str(role.id), "name": role.name, "color": _colour(role)}


def server_summary(guild: discord.Guild, presences: bool = True) -> dict:
    """`presences` = the bot has the Presence intent (else online is unknown, null)."""
    total = guild.member_count if guild.member_count is not None else len(guild.members)
    online = None
    if presences:
        online = sum(1 for m in guild.members if getattr(m, "status", discord.Status.offline) is not discord.Status.offline)
    return {
        "id": str(guild.id),
        "name": guild.name,
        "icon_url": _asset(guild.icon),
        "member_count": total,
        "online_count": online,
        "boost_count": guild.premium_subscription_count or 0,
        "boost_tier": int(guild.premium_tier or 0),
    }


def profile_of(member: discord.Member) -> dict:
    roles = [r for r in reversed(member.roles) if not r.is_default()]
    until = member.timed_out_until
    if until is not None and until <= datetime.now(timezone.utc):
        until = None
    unverified = int(getattr(config, "UNVERIFIED_ROLE_ID", 0) or 0)
    verified = int(getattr(config, "VERIFIED_ROLE_ID", 0) or 0)
    role_ids = {r.id for r in member.roles}
    if verified:
        is_verified: bool | None = verified in role_ids
    elif unverified:
        is_verified = unverified not in role_ids
    else:
        is_verified = None  # this server has no verification roles configured
    return {
        "display_name": member.display_name,
        "joined_at": _iso(member.joined_at),
        "created_at": _iso(member.created_at),
        "roles": [_role(r) for r in roles],
        "top_role_color": _colour(member.top_role) if roles else None,
        "booster_since": _iso(member.premium_since),
        "timed_out_until": _iso(until),
        "verified": is_verified,
    }


class MemberHubService:
    """Built per request by internal_api/routes/member_hub.py from the bot's live
    services; any dependency may be None (that section is then unavailable)."""

    def __init__(
        self,
        *,
        warnings=None,
        reaction_roles=None,
        birthdays=None,
        honeypot=None,
        tickets_repo=None,
        qotd_repo=None,
        music=None,
        presences: bool = True,
    ) -> None:
        self._presences = presences
        self._warnings = warnings
        self._rr = reaction_roles
        self._birthdays = birthdays
        self._honeypot = honeypot
        self._tickets = tickets_repo
        self._qotd = qotd_repo
        self._music = music

    # ------------------------------------------------------------------
    # Whole view
    # ------------------------------------------------------------------

    async def build(self, guild: discord.Guild, user: Any, member: discord.Member | None) -> dict:
        """`user` is a discord.User/Member (or None if Discord doesn't know the id);
        `member` the live guild member or None."""
        unavailable: list[str] = []
        out: dict[str, Any] = {
            "user": {
                "id": str(getattr(user, "id", "") or "") or None,
                "username": getattr(user, "name", None),
                "display_name": getattr(member or user, "display_name", None),
                "avatar_url": _asset(getattr(member or user, "display_avatar", None)),
            },
            "server": server_summary(guild, self._presences),
            "unavailable": unavailable,
        }
        if member is None:
            out.update(await self._outsider(guild, int(getattr(user, "id", 0) or 0), unavailable))
            return out

        out["status"] = STATUS_MEMBER
        out["profile"] = profile_of(member)
        out["standing"] = await self._section("standing", unavailable, self._standing(guild, member, out["profile"]))
        out["self_roles"] = await self._section("self_roles", unavailable, self._self_roles(guild, member), default=[])
        out["birthday"] = await self._section("birthday", unavailable, self._birthday(guild, member))
        out["tickets"] = await self._section("tickets", unavailable, self._open_tickets(guild, member), default=[])
        out["events"] = await self._section("events", unavailable, self._events(guild), default=[])
        out["qotd"] = await self._section("qotd", unavailable, self._latest_qotd(guild))
        out["music"] = await self._section("music", unavailable, self._now_playing(guild))
        return out

    @staticmethod
    async def _section(name: str, unavailable: list[str], coro, default=None):
        try:
            result = await coro
        except _Unavailable:
            unavailable.append(name)
            return default
        except Exception:
            log.exception("member hub: %s section failed", name)
            unavailable.append(name)
            return default
        return default if result is None and default is not None else result

    # ------------------------------------------------------------------
    # Not in the server
    # ------------------------------------------------------------------

    async def _outsider(self, guild: discord.Guild, user_id: int, unavailable: list[str]) -> dict:
        trap = None
        if self._honeypot is None:
            unavailable.append("trap")
        else:
            try:
                data = await self._honeypot.appeal_status_for_user(guild.id, user_id)
                appeal = data.get("appeal")
                if data.get("eligible") or appeal:
                    trap = {
                        "eligible": bool(data.get("eligible")),
                        "appeal": None if not appeal else {
                            "status": appeal.get("status"),
                            "submitted_at": appeal.get("submitted_at"),
                            "reviewed_at": appeal.get("reviewed_at"),
                        },
                    }
            except Exception:
                log.exception("member hub: honeypot appeal status failed")
                unavailable.append("trap")

        banned = None
        try:
            await guild.fetch_ban(discord.Object(id=user_id))
            banned = True
        except discord.NotFound:
            banned = False
        except discord.HTTPException:
            unavailable.append("ban_status")

        if trap is not None:
            status = STATUS_TRAP
        elif banned:
            status = STATUS_BANNED
        else:
            status = STATUS_NOT_MEMBER
        return {"status": status, "trap": trap, "banned": banned}

    # ------------------------------------------------------------------
    # Sections (members)
    # ------------------------------------------------------------------

    async def _standing(self, guild: discord.Guild, member: discord.Member, profile: dict) -> dict:
        if self._warnings is None:
            raise _Unavailable
        rows = await self._warnings.get_warnings(guild.id, member.id)
        # Count and dates only: the reason and the moderator are staff notes.
        warnings = [{"id": r.get("id"), "created_at": r.get("created_at")} for r in rows]
        timed_out = profile.get("timed_out_until")
        level = "timed_out" if timed_out else ("warned" if warnings else "good")
        return {
            "level": level,
            "warning_count": len(warnings),
            "warnings": warnings[:MAX_WARNINGS_LISTED],
            "timed_out_until": timed_out,
        }

    async def _posted_panels(self, guild_id: int) -> list[dict]:
        panels = await self._rr.list_panels(guild_id)
        return [p for p in panels if p.get("message_id")]

    async def _self_roles(self, guild: discord.Guild, member: discord.Member) -> list[dict]:
        if self._rr is None:
            raise _Unavailable
        held = {r.id for r in member.roles}
        out = []
        for panel in await self._posted_panels(guild.id):
            roles = []
            for entry in await self._rr.get_entries(panel["id"]):
                role = guild.get_role(int(entry["role_id"]))
                if role is None:
                    continue
                roles.append({
                    **_role(role),
                    "emoji": entry.get("emoji"),
                    "label": entry.get("label"),
                    "has": role.id in held,
                })
            if roles:
                channel_id = panel.get("channel_id")
                out.append({
                    "panel_id": panel["id"],
                    "title": panel.get("title") or "Roles",
                    "exclusive": bool(panel.get("exclusive")),
                    "channel_id": str(channel_id) if channel_id else None,
                    "jump_url": (
                        f"https://discord.com/channels/{guild.id}/{channel_id}/{panel['message_id']}" if channel_id else None
                    ),
                    "roles": roles,
                })
        return out

    async def _birthday(self, guild: discord.Guild, member: discord.Member) -> dict:
        if self._birthdays is None:
            raise _Unavailable
        from services.birthday_service import format_birthday, nearest_birthdays, parse_timezone

        settings = await self._birthdays.get_settings(guild.id) or {}
        mine = await self._birthdays.get_birthday(guild.id, member.id)
        guild_tz = parse_timezone(settings.get("guild_timezone") or "UTC") or timezone.utc
        today = datetime.now(guild_tz).date()
        groups = nearest_birthdays(await self._birthdays.get_all_for_guild(guild.id), today)
        nearest = []
        for group in groups:
            people = []
            for row in group["rows"]:
                m = guild.get_member(int(row["user_id"]))
                if m is not None:  # like /birthday show-nearest, but departed people are left out
                    people.append({"id": str(m.id), "display_name": m.display_name, "avatar_url": _asset(m.display_avatar)})
            if people:
                nearest.append({
                    "offset": group["offset"], "month": group["month"], "day": group["day"],
                    "label": format_birthday(group["month"], group["day"]), "people": people,
                })
        return {
            "mine": None if not mine else {
                "month": mine["birth_month"], "day": mine["birth_day"],
                "label": format_birthday(mine["birth_month"], mine["birth_day"]),
                "timezone": mine.get("user_timezone"),
            },
            "nearest": nearest,
        }

    async def _open_tickets(self, guild: discord.Guild, member: discord.Member) -> list[dict]:
        if self._tickets is None:
            raise _Unavailable
        rows = await self._tickets.list_unarchived_by_user(guild.id, member.id)
        out = []
        for row in rows:
            if row.get("status") != "open":
                continue
            channel = guild.get_channel(int(row["channel_id"]))
            out.append({
                "id": row["id"],
                "number": row.get("ticket_number"),
                "channel_id": str(row["channel_id"]),
                "channel_name": getattr(channel, "name", None),
                "opened_at": row.get("opened_at"),
                "jump_url": f"https://discord.com/channels/{guild.id}/{row['channel_id']}",
            })
        return out

    async def _events(self, guild: discord.Guild) -> list[dict]:
        events = [
            e for e in getattr(guild, "scheduled_events", []) or []
            if e.status in (discord.EventStatus.scheduled, discord.EventStatus.active)
        ]
        events.sort(key=lambda e: e.start_time)
        out = []
        for e in events[:MAX_EVENTS]:
            channel = getattr(e, "channel", None)
            out.append({
                "id": str(e.id),
                "name": e.name,
                "description": (e.description or "")[:300] or None,
                "start_time": _iso(e.start_time),
                "end_time": _iso(e.end_time),
                "active": e.status is discord.EventStatus.active,
                "location": e.location or (getattr(channel, "name", None) if channel else None),
                "interested": e.user_count,
                "url": e.url,
                "image_url": _asset(e.cover_image),
            })
        return out

    async def _latest_qotd(self, guild: discord.Guild) -> dict | None:
        if self._qotd is None:
            raise _Unavailable
        rows = await self._qotd.get_history(guild.id, limit=1)
        if not rows:
            return None
        row = rows[0]
        jump = None
        if row.get("channel_id") and row.get("message_id"):
            jump = f"https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']}"
        return {"question": row.get("question"), "posted_at": row.get("posted_at"), "jump_url": jump}

    async def _now_playing(self, guild: discord.Guild) -> dict | None:
        if self._music is None:
            raise _Unavailable
        state = self._music.dashboard_state(guild)
        current = state.get("current")
        if not state.get("connected") or not current:
            return None
        vc = state.get("voice_channel") or {}
        return {
            "title": current.get("title"),
            "paused": bool(state.get("paused")),
            "voice_channel": vc.get("name"),
            "listeners": vc.get("listeners"),
            "queue_total": state.get("queue_total", 0),
        }


class _Unavailable(Exception):
    """A section's feature is not loaded."""


async def member_toggle(rr_service: Any, guild: discord.Guild, member: discord.Member, panel_id: int,
                        role_id: int, assign: bool) -> dict:
    """Give or take a self-role from the hub. Same rules as the panel button
    (cogs/reaction_roles.py on_interaction): the panel must belong to this guild
    and be posted, the role must still be on it and exist, and a GRANT re-checks
    self_role_policy.grant_block_reason (staff / granted / dangerous roles are
    never handed out). An exclusive panel drops the member's other roles from it.
    -> {"result": "added" | "removed" | "unchanged", "role_id", "removed_role_ids"}."""
    panel = await rr_service.get_panel(panel_id)
    if not panel or panel.get("guild_id") != guild.id or not panel.get("message_id"):
        raise HubError("panel_not_found", "That role panel no longer exists.", 404)
    entries = await rr_service.get_entries(panel_id)
    if not any(int(e["role_id"]) == role_id for e in entries):
        raise HubError("role_not_on_panel", "That role is no longer on this panel.", 404)
    role = guild.get_role(role_id)
    if role is None:
        raise HubError("role_not_found", "That role no longer exists.", 404)

    has_role = role in member.roles
    if assign == has_role:
        return {"result": "unchanged", "role_id": str(role_id), "removed_role_ids": []}

    reason = "Member hub (self-role)"
    removed: list[int] = []
    try:
        if not assign:
            await member.remove_roles(role, reason=reason)
            return {"result": "removed", "role_id": str(role_id), "removed_role_ids": [str(role_id)]}
        block = await rr_service.click_block_reason(guild, role)
        if block:
            log.warning("member hub: refused to grant role %s via panel %s to %s: %s", role.id, panel_id, member.id, block)
            raise HubError("role_blocked", "This role can't be self-assigned right now. Ask a staff member.", 403)
        if panel.get("exclusive"):
            panel_role_ids = {int(e["role_id"]) for e in entries}
            others = [r for r in member.roles if r.id in panel_role_ids and r.id != role_id]
            if others:
                await member.remove_roles(*others, reason=f"{reason}, exclusive panel")
                removed = [str(r.id) for r in others]
        await member.add_roles(role, reason=reason)
    except discord.Forbidden:
        raise HubError("discord_forbidden", "The bot isn't allowed to manage that role.", 409)
    except discord.HTTPException:
        raise HubError("discord_error", "Discord didn't accept the change. Try again.", 502)
    return {"result": "added", "role_id": str(role_id), "removed_role_ids": removed}
