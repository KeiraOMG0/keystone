# SPDX-License-Identifier: AGPL-3.0-only
# Python port of BirthdayBot by NoiTheCat (https://github.com/NoiTheCat/BirthdayBot),
# licensed under the GNU AGPL v3.0. See CREDITS.md and LICENSES/AGPL-3.0.txt.
"""Birthday logic shared by the birthday cogs (cogs/birthdays.py and its
sibling cogs) and the dashboard Birthdays page (internal_api/routes/birthdays.py).

* pure date math for the scheduler (local calendar, Feb 29 -> Feb 28),
* the "recent & upcoming" list (/birthday show-nearest and the page),
* export builders (.txt / .csv / .ics) and the BirthdayBot .txt import parser,
* birthday-role validation (same self-assign policy as self-roles: any member
  can set their birthday to today and receive the role),
* BirthdayService: settings + entries, publishing the live `birthdays` topic
  at every mutation point (services/live_events.py, gated on manage_birthdays
  in server/live/hub.py).
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from database.repositories.birthday_repo import BirthdayRepository
from services.live_events import publish as live_publish

# Limits shared by the cog and the dashboard (the /config announce set-message
# modal fields are 1500 chars; /birthdays import refuses files over 2 MB).
ANNOUNCE_MESSAGE_MAX = 1500
IMPORT_MAX_BYTES = 2_000_000
TEST_MEMBERS_MAX = 5
NEAREST_DAYS_BEFORE = 7
NEAREST_DAYS_AFTER = 14
EXPORT_FORMATS = ("plaintext", "csv", "ics")
_EXPORT_EXT = {"plaintext": "txt", "csv": "csv", "ics": "ics"}

HARDCODED_MENTION_RE = re.compile(r"<@!?\d+>")

_MONTH_LENGTHS = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

MONTH_DISPLAY = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def parse_timezone(tz_str: str) -> ZoneInfo | None:
    if not tz_str:
        return None
    candidates = [tz_str]
    # Try to fix common user mistakes: strip trailing slash, normalise casing
    cleaned = "/".join(p.title() for p in tz_str.strip("/").split("/"))
    if cleaned != tz_str:
        candidates.append(cleaned)
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            continue
    return None


def format_birthday(month: int, day: int) -> str:
    return f"{MONTH_DISPLAY[month]}-{day:02d}"


def valid_month_day(month: int, day: int) -> bool:
    """True for a real calendar (month, day); Feb 29 is valid (celebrated on
    Feb 28 in non-leap years)."""
    return 1 <= month <= 12 and 1 <= day <= _MONTH_LENGTHS[month]


def all_timezones() -> list[str]:
    """Every IANA zone name the bot accepts (sorted), for pickers."""
    return sorted(available_timezones())


def hardcoded_mentions(*texts: str | None) -> list[str]:
    """`<@123>`-style user mentions typed into an announcement template (they
    ping a fixed user; `{mention}` is almost always what was meant)."""
    return [m for text in texts if text for m in HARDCODED_MENTION_RE.findall(text)]


EVERYONE_PLACEHOLDER = "%e"
EVERYONE_STAFF_ONLY = (
    "Only full staff (the owner or a staff role) can save an announcement message containing %e "
    "(the @everyone ping). Remove %e or ask a staff member."
)


def everyone_block_reason(
    old_single: str | None,
    old_plural: str | None,
    new_single: str | None,
    new_plural: str | None,
    actor_is_staff: bool,
) -> str | None:
    """`%e` makes the announcement ping @everyone (the one opt-in to the bot's
    global no-@everyone rule). manage_birthdays may edit the messages, but a
    message that CHANGES and contains %e may only be saved by full staff
    (PermissionService.is_staff: OWNER_ID / STAFF_ROLE_IDS), so every saved %e
    text was written by staff. An unchanged message keeps its %e."""
    if actor_is_staff:
        return None
    for old, new in ((old_single, new_single), (old_plural, new_plural)):
        if (new or None) != (old or None) and new and EVERYONE_PLACEHOLDER in new:
            return EVERYONE_STAFF_ONLY
    return None


# ---------------------------------------------------------------------- #
# Pure date math for the scheduler (cogs/birthdays.py). All of it works  #
# in the member's LOCAL calendar (their timezone, else the guild's):     #
#   * a birthday "occurrence" is identified by the local YEAR it falls  #
#     in, and birthdays.last_celebrated stores that local year, so a     #
#     birthday that starts on Dec 31 UTC (UTC+ zones, Jan 1 birthday) or #
#     ends on Jan 1 UTC (UTC- zones, Dec 31 birthday) is still exactly   #
#     one occurrence and never re-announced;                             #
#   * Feb 29 birthdays are celebrated on Feb 28 in non-leap years.       #
# ---------------------------------------------------------------------- #

def is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def effective_birthday(month: int, day: int, year: int) -> date:
    """The calendar date a (month, day) birthday is celebrated on in `year`:
    Feb 29 falls back to Feb 28 in non-leap years."""
    if month == 2 and day == 29 and not is_leap_year(year):
        return date(year, 2, 28)
    return date(year, month, day)


def is_birthday_on(month: int, day: int, local_day: date) -> bool:
    return effective_birthday(month, day, local_day.year) == local_day


def birthday_month_days(local_day: date) -> list[tuple[int, int]]:
    """Stored (month, day) pairs that are celebrated on `local_day`: just the
    date itself, plus Feb 29 on Feb 28 of a non-leap year."""
    pairs = [(local_day.month, local_day.day)]
    if local_day.month == 2 and local_day.day == 28 and not is_leap_year(local_day.year):
        pairs.append((2, 29))
    return pairs


def upcoming_month_days(now_utc: datetime, days: int) -> list[tuple[int, int]]:
    """(month, day) pairs to fetch for a scheduling window of `days` days.
    Starts one day BEFORE today (UTC) because a member in a UTC- zone can
    still be on yesterday's date, and adds Feb 29 when Feb 28 of a non-leap
    year is in the window."""
    out: list[tuple[int, int]] = []
    for offset in range(-1, days + 2):
        for pair in birthday_month_days((now_utc + timedelta(days=offset)).date()):
            if pair not in out:
                out.append(pair)
    return out


def next_celebration(
    month: int, day: int, tz: ZoneInfo, now_utc: datetime, window_days: int = 14
) -> tuple[datetime, int] | None:
    """When to fire the next celebration and which occurrence it is.

    Returns (fire_at_utc, occurrence_year) where occurrence_year is the LOCAL
    year of that birthday. If it is the member's birthday right now (local
    date), fire_at_utc is `now_utc` (fire immediately). Otherwise it is the
    UTC instant of local midnight at the start of the next birthday within
    `window_days`. None if there is no birthday in the window."""
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()
    if is_birthday_on(month, day, today_local):
        return now_utc, today_local.year
    for offset in range(1, window_days + 1):
        d = today_local + timedelta(days=offset)
        if is_birthday_on(month, day, d):
            midnight_local = datetime(d.year, d.month, d.day, tzinfo=tz)
            return midnight_local.astimezone(timezone.utc), d.year
    return None


def current_occurrence_year(month: int, day: int, tz: ZoneInfo, now_utc: datetime) -> int | None:
    """The local year of the birthday occurrence happening right now, or None
    if it is not the member's birthday in their timezone at `now_utc`."""
    today_local = now_utc.astimezone(tz).date()
    return today_local.year if is_birthday_on(month, day, today_local) else None


def should_celebrate(last_celebrated: int | None, occurrence_year: int | None) -> bool:
    """True when this occurrence has not been announced yet. last_celebrated
    is the local occurrence year of the last announcement (0 = never)."""
    if occurrence_year is None:
        return False
    return int(last_celebrated or 0) < occurrence_year


def should_remove_birthday_role(month: int, day: int, tz: ZoneInfo, now_utc: datetime) -> bool:
    """The birthday role comes off as soon as it is no longer the member's
    birthday in their timezone, independent of last_celebrated (the old
    check compared it with the UTC year and left the role on for a year
    when the birthday straddled Dec 31/Jan 1 UTC)."""
    return current_occurrence_year(month, day, tz, now_utc) is None


def days_until_next(month: int, day: int, today: date) -> tuple[date, int]:
    """(next local celebration date on or after `today`, days until it)."""
    for year in (today.year, today.year + 1, today.year + 2):
        d = effective_birthday(month, day, year)
        if d >= today:
            return d, (d - today).days
    raise ValueError("unreachable")  # pragma: no cover


def nearest_birthdays(
    rows: list[dict],
    today: date,
    before: int = NEAREST_DAYS_BEFORE,
    after: int = NEAREST_DAYS_AFTER,
) -> list[dict]:
    """Birthdays in the window [today - before, today + after] (the server's
    local calendar), grouped per celebrated date, nearest-first by offset:
    [{"offset": int, "date": date, "month": m, "day": d, "rows": [...]}].
    Shared by /birthday show-nearest and the dashboard page. Feb 29 shows on
    Feb 28 in non-leap years, like the scheduler."""
    groups: dict[date, dict] = {}
    for offset in range(-before, after + 1):
        d = today + timedelta(days=offset)
        for row in rows:
            if is_birthday_on(row["birth_month"], row["birth_day"], d):
                g = groups.setdefault(d, {"offset": offset, "date": d, "month": d.month, "day": d.day, "rows": []})
                if row not in g["rows"]:
                    g["rows"].append(row)
    return [groups[k] for k in sorted(groups, key=lambda k: groups[k]["offset"])]


# ---------------------------------------------------------------------- #
# Export (/birthdays export and the dashboard download). `member_map`      #
# maps user_id -> a member-like object (discord.Member or anything with  #
# name / global_name / nick / display_name); missing = left the server.  #
# ---------------------------------------------------------------------- #

_ICS_HEADER = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//kick-bot//BirthdayBot//EN\r\nCALSCALE:GREGORIAN\r\n"
_ICS_FOOTER = "END:VCALENDAR\r\n"


def ics_fold(line: str) -> str:
    """Fold a content line at 75 octets (RFC 5545 3.1), never splitting a
    UTF-8 character; continuation lines start with one space."""
    out: list[str] = []
    current, size, limit = "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(current)
            current, size, limit = " ", 1, 75
        current += ch
        size += n
    out.append(current)
    return "\r\n".join(out)


def ics_escape(text: str) -> str:
    """RFC 5545 TEXT escaping (backslash, semicolon, comma, newline)."""
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    )


def build_plaintext(guild_name: str, rows: list[dict], member_map: dict[int, Any]) -> str:
    lines = [f"Birthdays in {guild_name}", ""]
    for row in rows:
        m = member_map.get(row["user_id"])
        date_str = format_birthday(row["birth_month"], row["birth_day"])
        if m:
            global_name = getattr(m, "global_name", None) or ""
            nick = getattr(m, "nick", None) or ""
            nick_part = f" - Nickname: {nick}" if nick else ""
            line = f"● {date_str}: {row['user_id']} {m.name} ({global_name}){nick_part}"
        else:
            line = f"● {date_str}: {row['user_id']}"
        if row.get("user_timezone"):
            line += f" | Time zone: {row['user_timezone']}"
        lines.append(line)
    return "\n".join(lines)


def build_csv(rows: list[dict], member_map: dict[int, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL)
    writer.writerow(["UserId", "Username", "DisplayName", "Nickname", "MonthDayDisp", "Month", "Day", "TimeZone"])
    for row in rows:
        m = member_map.get(row["user_id"])
        writer.writerow([
            row["user_id"],
            m.name if m else "",
            (getattr(m, "global_name", None) or m.display_name) if m else "",
            (getattr(m, "nick", None) or "") if m else "",
            format_birthday(row["birth_month"], row["birth_day"]),
            row["birth_month"],
            row["birth_day"],
            row.get("user_timezone") or "",
        ])
    return buf.getvalue()


def build_ics(guild_id: int, guild_name: str, rows: list[dict], member_map: dict[int, Any]) -> str:
    parts = [_ICS_HEADER]
    for row in rows:
        m = member_map.get(row["user_id"])
        name = m.display_name if m else str(row["user_id"])
        uid = f"{guild_id ^ row['user_id']}@birthdaybot"
        # Year 2000 is a leap year, so a Feb 29 birthday is a valid DTSTART.
        dtstart = f"2000{row['birth_month']:02d}{row['birth_day']:02d}"
        summary = ics_fold(f"SUMMARY:{ics_escape(name)}'s birthday")
        description = ics_fold(f"DESCRIPTION:Birthday of {ics_escape(name)} in {ics_escape(guild_name)}")
        parts.append(
            f"BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\n"
            f"DTSTAMP:20000101T000000Z\r\n"
            f"DTSTART;VALUE=DATE:{dtstart}\r\n"
            f"RRULE:FREQ=YEARLY\r\n"
            f"{summary}\r\n"
            f"{description}\r\n"
            f"END:VEVENT\r\n"
        )
    parts.append(_ICS_FOOTER)
    return "".join(parts)


def build_export(fmt: str, guild_id: int, guild_name: str, rows: list[dict], member_map: dict[int, Any]) -> tuple[str, str]:
    """(filename, content) for one of EXPORT_FORMATS. Raises ValueError otherwise."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"unknown export format {fmt!r}")
    if fmt == "csv":
        content = build_csv(rows, member_map)
    elif fmt == "ics":
        content = build_ics(guild_id, guild_name, rows, member_map)
    else:
        content = build_plaintext(guild_name, rows, member_map)
    return f"birthdaybot-{guild_id}.{_EXPORT_EXT[fmt]}", content


# ---------------------------------------------------------------------- #
# Import of a BirthdayBot / plaintext export ("● Mar-14: <user id> ...")  #
# ---------------------------------------------------------------------- #

_EXPORT_LINE_RE = re.compile(r"●\s+(\w+)-(\d+):\s+(\d+)")
_TZ_RE = re.compile(r"Time zone:\s*(\S+)")


@dataclass
class ImportParse:
    entries: list[dict] = field(default_factory=list)
    # (line number, line text (truncated), reason) for lines that were skipped
    invalid: list[tuple[int, str, str]] = field(default_factory=list)


def parse_export_line_detailed(line: str) -> tuple[dict | None, str | None]:
    """(entry, None) for a valid line, (None, reason) for a bad "●" line,
    (None, None) for a line that is not an entry at all (header, blank)."""
    line = line.strip()
    if not line.startswith("●"):
        return None, None
    m = _EXPORT_LINE_RE.match(line)
    if not m:
        return None, "Not in the 'Mon-DD: user id' format."
    month_abbr, day_str, user_id_str = m.group(1), m.group(2), m.group(3)
    month = _MONTH_NAMES.get(month_abbr.lower())
    if month is None:
        return None, f"Unknown month '{month_abbr}'."
    day = int(day_str)
    if not valid_month_day(month, day):
        return None, f"Day {day} is not valid for {MONTH_DISPLAY[month]}."
    user_id = int(user_id_str)
    if not (0 < user_id < 2**63):
        return None, "Invalid user id."
    tz = None
    tz_match = _TZ_RE.search(line)
    if tz_match and parse_timezone(tz_match.group(1)):
        tz = tz_match.group(1)
    return {"user_id": user_id, "birth_month": month, "birth_day": day, "user_timezone": tz}, None


def parse_import_text(text: str) -> ImportParse:
    out = ImportParse()
    for n, raw in enumerate(text.splitlines(), start=1):
        entry, reason = parse_export_line_detailed(raw)
        if entry is not None:
            out.entries.append(entry)
        elif reason is not None:
            out.invalid.append((n, raw.strip()[:200], reason))
        elif raw.strip() and not raw.strip().startswith("Birthdays"):
            out.invalid.append((n, raw.strip()[:200], "Not a birthday line."))
    return out


# ---------------------------------------------------------------------- #
# Birthday role / announce channel checks (discord objects, duck-typed)   #
# ---------------------------------------------------------------------- #

async def birthday_role_block_reason(role: Any, guild: Any, actor: Any, permission_service: Any) -> str | None:
    """Why `actor` may not make `role` the birthday role, or None. Used by
    /config birthday-role and the dashboard. The birthday role is handed to
    whoever registers today's date, so it follows the self-role policy
    (services/self_role_policy.py): not @everyone / managed, below the bot,
    no moderator permissions, not a staff / bot-permission role, below the
    configuring member's top role (owner exempt)."""
    from services.self_role_policy import configure_block_reason

    if role is None:
        return "That role no longer exists."
    if role.is_default() or role.managed:
        return "That role cannot be used as a birthday role."
    return await configure_block_reason(role, guild, actor, permission_service)


def role_status(guild: Any, role_id: int | None) -> dict:
    """{"id", "name", "exists", "usable"} for /config check and the page."""
    role = guild.get_role(role_id) if role_id else None
    if role is None:
        return {"id": role_id, "name": None, "exists": False, "usable": False}
    usable = (not role.is_default()) and (not role.managed) and guild.me.top_role > role
    return {"id": role.id, "name": role.name, "exists": True, "usable": bool(usable)}


def channel_status(guild: Any, channel_id: int | None) -> dict:
    """{"id", "name", "exists", "can_send"} for /config check and the page."""
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return {"id": channel_id, "name": None, "exists": False, "can_send": False}
    return {
        "id": channel.id, "name": channel.name, "exists": True,
        "can_send": bool(channel.permissions_for(guild.me).send_messages),
    }


class BirthdayService:
    def __init__(self, repo: BirthdayRepository):
        self._repo = repo

    # ------------------------------------------------------------------ #
    # Date / timezone parsing                                              #
    # ------------------------------------------------------------------ #

    def parse_date(self, day_str: str, month_str: str) -> tuple[int | None, int | None, str | None]:
        """Returns (month, day, error). month_str accepts names or numbers."""
        month_lower = month_str.strip().lower()
        if month_lower in _MONTH_NAMES:
            month = _MONTH_NAMES[month_lower]
        elif month_lower.isdigit():
            month = int(month_lower)
            if not 1 <= month <= 12:
                return None, None, "Month must be between 1 and 12."
        else:
            return None, None, f"'{month_str}' is not a valid month."

        if not day_str.strip().isdigit():
            return None, None, "Day must be a number."
        day = int(day_str.strip())
        if day < 1 or day > _MONTH_LENGTHS[month]:
            return None, None, f"Day {day} is not valid for {MONTH_DISPLAY[month]}."

        return month, day, None

    # ------------------------------------------------------------------ #
    # Live updates                                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def notify(guild_id: int, change: str, user_id: int | None = None) -> None:
        """Publish on the live `birthdays` topic (the dashboard page re-fetches).
        Never raises. `change`: entry | settings | bulk | celebrated | timers."""
        data: dict = {"kind": "birthdays", "change": change, "guild_id": guild_id}
        if user_id is not None:
            data["user_id"] = user_id
        live_publish("birthdays", data)

    # ------------------------------------------------------------------ #
    # Settings                                                             #
    # ------------------------------------------------------------------ #

    async def get_settings(self, guild_id: int) -> dict:
        return await self._repo.get_or_create_settings(guild_id)

    async def update_settings(self, guild_id: int, **changes) -> dict:
        """Write several already-validated settings at once (dashboard), then
        publish once. Returns the new settings row."""
        await self._repo.get_or_create_settings(guild_id)
        if changes:
            await self._repo.update_settings(guild_id, **changes)
            self.notify(guild_id, "settings")
        return await self._repo.get_or_create_settings(guild_id)

    async def _set(self, guild_id: int, **changes) -> None:
        await self._repo.get_or_create_settings(guild_id)
        await self._repo.update_settings(guild_id, **changes)
        self.notify(guild_id, "settings")

    async def set_birthday_role(self, guild_id: int, role_id: int | None) -> None:
        await self._set(guild_id, birthday_role_id=role_id)

    async def set_announce_channel(self, guild_id: int, channel_id: int | None) -> None:
        await self._set(guild_id, announce_channel_id=channel_id)

    async def set_guild_timezone(self, guild_id: int, tz_str: str | None) -> str | None:
        if tz_str and parse_timezone(tz_str) is None:
            return f"'{tz_str}' is not a valid timezone."
        await self._set(guild_id, guild_timezone=tz_str)
        return None

    async def set_announce_messages(
        self, guild_id: int, single: str | None, plural: str | None
    ) -> None:
        await self._set(guild_id, announce_message=single, announce_message_pl=plural)

    async def set_announce_ping(self, guild_id: int, enabled: bool) -> None:
        await self._set(guild_id, announce_ping=1 if enabled else 0)

    async def set_add_only(self, guild_id: int, enabled: bool) -> None:
        await self._set(guild_id, add_only=1 if enabled else 0)

    async def set_private_confirms(self, guild_id: int, enabled: bool) -> None:
        await self._set(guild_id, private_confirms=1 if enabled else 0)

    async def set_ping_user(self, guild_id: int, enabled: bool) -> None:
        await self._set(guild_id, ping_user=1 if enabled else 0)

    # ------------------------------------------------------------------ #
    # User birthday operations                                             #
    # ------------------------------------------------------------------ #

    async def set_birthday(
        self,
        guild_id: int,
        user_id: int,
        day_str: str,
        month_str: str,
        tz_str: str | None = None,
        *,
        override: bool = False,
    ) -> tuple[bool, str]:
        """
        Set a birthday. If override=False, enforces add_only (can't edit once set
        unless the caller has already checked staff permissions).
        Returns (success, message). An omitted timezone keeps the stored one.
        """
        month, day, err = self.parse_date(day_str, month_str)
        if err:
            return False, err

        tz_obj = None
        if tz_str:
            tz_obj = parse_timezone(tz_str)
            if tz_obj is None:
                return False, f"'{tz_str}' is not a recognised timezone."

        if not override:
            settings = await self.get_settings(guild_id)
            if settings.get("add_only"):
                existing = await self._repo.get_birthday(guild_id, user_id)
                if existing:
                    return False, (
                        "This server has restricted birthday editing. "
                        "Contact a moderator to update your birthday."
                    )

        await self._repo.set_birthday(
            guild_id, user_id, month, day,
            user_timezone=tz_str if tz_obj else None,
        )
        self.notify(guild_id, "entry", user_id)
        tz_note = f" Timezone set to **{tz_str}**." if tz_obj else ""
        return True, f"Birthday set to **{format_birthday(month, day)}**.{tz_note}"

    async def set_timezone(
        self,
        guild_id: int,
        user_id: int,
        tz_str: str,
        *,
        override: bool = False,
    ) -> tuple[bool, str]:
        if parse_timezone(tz_str) is None:
            return False, f"'{tz_str}' is not a recognised timezone."

        existing = await self._repo.get_birthday(guild_id, user_id)
        if existing is None:
            return False, "You don't have a birthday set in this server yet."

        if not override:
            settings = await self.get_settings(guild_id)
            if settings.get("add_only") and existing.get("user_timezone"):
                return False, (
                    "This server has restricted birthday editing. "
                    "Contact a moderator to update your timezone."
                )

        await self._repo.set_timezone(guild_id, user_id, tz_str)
        self.notify(guild_id, "entry", user_id)
        return True, f"Timezone updated to **{tz_str}**."

    async def clear_timezone(self, guild_id: int, user_id: int) -> bool:
        """Drop a member's own timezone (they then follow the server's)."""
        changed = await self._repo.clear_timezone(guild_id, user_id)
        if changed:
            self.notify(guild_id, "entry", user_id)
        return changed

    async def remove_birthday(
        self, guild_id: int, user_id: int
    ) -> bool:
        removed = await self._repo.remove_birthday(guild_id, user_id)
        if removed:
            self.notify(guild_id, "entry", user_id)
        return removed

    async def mark_celebrated(self, guild_id: int, user_id: int, year: int) -> None:
        await self._repo.mark_celebrated(guild_id, user_id, year)
        self.notify(guild_id, "celebrated", user_id)

    async def reset_today(self, guild_id: int, local_day: date) -> int:
        """Reset last_celebrated for every birthday celebrated on `local_day`
        (incl. Feb 29 on Feb 28 of a non-leap year). Returns how many rows."""
        count = 0
        for month, day in birthday_month_days(local_day):
            count += len(await self._repo.get_by_month_day(guild_id, month, day))
            await self._repo.reset_last_celebrated_for_date(guild_id, month, day)
        self.notify(guild_id, "timers")
        return count

    async def get_birthday(self, guild_id: int, user_id: int) -> dict | None:
        return await self._repo.get_birthday(guild_id, user_id)

    async def get_all_for_guild(self, guild_id: int) -> list[dict]:
        return await self._repo.get_all_for_guild(guild_id)

    async def get_upcoming(self, guild_id: int, days: int = 14) -> list[dict]:
        targets = upcoming_month_days(datetime.now(timezone.utc), days)
        return await self._repo.get_upcoming(guild_id, days, targets=targets)

    # ------------------------------------------------------------------ #
    # Export                                                               #
    # ------------------------------------------------------------------ #

    def build_export_text(self, guild_name: str, rows: list[dict], member_map: dict[int, str]) -> str:
        lines = [f"Birthdays in {guild_name}", ""]
        for row in rows:
            display = member_map.get(row["user_id"], str(row["user_id"]))
            line = f"● {format_birthday(row['birth_month'], row['birth_day'])}: {row['user_id']} {display}"
            if row.get("user_timezone"):
                line += f" | Time zone: {row['user_timezone']}"
            lines.append(line)
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Import from BirthdayBot export text                                  #
    # ------------------------------------------------------------------ #

    def parse_export_line(self, line: str) -> dict | None:
        """One entry, or None (not an entry line, or an invalid one: unknown
        month, impossible day such as Feb-30, bad user id)."""
        entry, _reason = parse_export_line_detailed(line)
        return entry

    async def preview_import(self, guild_id: int, text: str) -> dict:
        """Dry run of import_from_text: what would be added / updated /
        left unchanged, plus every skipped line with the reason."""
        parsed = parse_import_text(text)
        existing = {r["user_id"]: r for r in await self._repo.get_all_for_guild(guild_id)}
        latest: dict[int, dict] = {}
        for e in parsed.entries:  # a later line for the same user wins, like the import
            latest[e["user_id"]] = e
        new = updated = unchanged = 0
        for uid, e in latest.items():
            row = existing.get(uid)
            if row is None:
                new += 1
            elif (row["birth_month"], row["birth_day"]) == (e["birth_month"], e["birth_day"]) and (
                e["user_timezone"] is None or e["user_timezone"] == row.get("user_timezone")
            ):
                unchanged += 1
            else:
                updated += 1
        return {
            "valid": len(parsed.entries),
            "users": len(latest),
            "new": new,
            "updated": updated,
            "unchanged": unchanged,
            "invalid": [{"line": n, "text": t, "reason": r} for n, t, r in parsed.invalid],
            "entries": list(latest.values()),
        }

    async def import_from_text(self, guild_id: int, text: str) -> tuple[int, int]:
        """Returns (imported, skipped). Invalid lines (unknown month, impossible
        day, malformed) are skipped and counted."""
        parsed = parse_import_text(text)
        count = await self._repo.import_bulk(guild_id, parsed.entries)
        if count:
            self.notify(guild_id, "bulk")
        return count, len(parsed.invalid)
