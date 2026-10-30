"""
services/honeypot_service.py — the honeypot ("do not type") trap channel.

Behaviour, DB schema and API shapes are specified in docs/honeypot_contract.md.
Both the /honeypot slash commands (cogs/honeypot.py) and the internal API
(internal_api/routes/honeypot.py) call the methods on this class — there is
deliberately no second copy of the logic anywhere.

Robustness rules for the catch path (this runs on a LIVE server):
  - Every Discord call is wrapped individually: a failure in one step (delete,
    DM, ban, log post, embed refresh) is recorded and never stops the others.
  - handle_message() never raises; the cog listener can call it blindly.
  - One action per user per burst (in-flight set + short cooldown).
  - The bot user is the moderator on every mod_actions row it writes.

Apps and webhooks (2026-10-20, after an app spammed the trap unnoticed):
  - A message posted BY AN APP on a member's behalf (it carries
    interaction_metadata: a user-installed app, which needs no server
    authorisation while @everyone has "Use External Apps", or a server bot's
    slash command) is treated as that member posting: the member who ran the
    command is caught (same exemptions, DM, action, event row), and the app is
    recorded on the event (via_app_id / via_app_name).
  - A webhook message or a plain bot message (no member behind it) cannot be
    punished like a person: the message is deleted, a webhook is deleted too
    (needs Manage Webhooks; nothing legitimate posts into the trap), and one
    "Honeypot: app/webhook message removed" log embed plus a
    `honeypot_foreign_message` audit row is written per source per
    CATCH_COOLDOWN. No honeypot_events row (stats are about people). A bot is
    never kicked automatically; the log says to review it.
  - The bot's own messages (the warning and "Caught so far" embeds) are skipped.

Pure helpers (message_flags, is_exempt, compute_stats, catch_to_dict; the
app/embed helpers are in services/message_source.py) are module-level so
they can be unit-tested without Discord.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import discord
from discord.http import Route

import config as _config
from database.repositories.honeypot_repo import HoneypotRepository
from services.live_events import hub as live_hub, publish as live_publish
from services.log_router import build_embed
# Re-exported: these used to live here (tests and older callers import them from this module).
from services.message_source import app_source, describe_app, message_text  # noqa: F401

log = logging.getLogger(__name__)

ACTIONS = ("ban", "softban", "kick")
DEFAULT_CATEGORY_NAME = "⚠️ Do Not Type"
DEFAULT_CHANNEL_NAME = "🚫┃do-not-type"
CONTENT_MAX = 500
REFRESH_MIN_INTERVAL = 10.0   # seconds between edits of the live "Caught so far" embed
FAILED_ACTION_DM_COOLDOWN_HOURS = 1   # after a failed punishment, no repeat DM/appeal row for this long
CATCH_COOLDOWN = 30.0        # seconds after a catch during which the same user is only cleaned up, not re-punished

_INVITE_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]{2,32})/?$", re.IGNORECASE,
)
_LINK_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_INVITE_RE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/", re.IGNORECASE)

# Name scrambling (migration 029): the trap channel and its category get random
# gibberish names every scramble_interval_hours, so spam tools can't learn to skip
# the trap by name. Discord allows 2 renames of a channel per 10 minutes.
SCRAMBLE_DEFAULT_HOURS = 6
SCRAMBLE_MIN_HOURS = 1
SCRAMBLE_MAX_HOURS = 168
SCRAMBLE_MANUAL_COOLDOWN = 600.0      # seconds since the last scramble before a manual one is allowed
SCRAMBLE_RETRY_AFTER_FAILURE = 1800.0  # a failed scheduled scramble is retried after this long
SCRAMBLE_MANUAL_TIMEOUT = 8.0          # dashboard budget (its internal-API client gives up at 10 s)
# Consonants (+ digits / e, y) only, so the gibberish practically never spells a real word.
_SCRAMBLE_CHANNEL_CHARS = "bcdfghjklmnpqrstvwxz23456789"
_SCRAMBLE_WORD_CHARS = "bcdfghjklmnpqrstvwxzey"
_DECORATION_RE = re.compile(r"^(\W*)(.*?)(\W*)$", re.DOTALL)


class HoneypotError(Exception):
    """A predictable failure the caller should surface. `status` is the HTTP status the
    internal API uses; `code` is a stable machine string; `detail` is what goes on the wire."""

    def __init__(self, code: str, message: str | None = None, status: int = 400) -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.detail = message or code


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    """ISO-8601 (or SQLite CURRENT_TIMESTAMP) string -> aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def message_flags(content: str, has_attachment: bool, mentions_everyone: bool) -> dict[str, bool]:
    lowered = content or ""
    return {
        "link": bool(_LINK_RE.search(lowered)),
        "invite": bool(_INVITE_RE.search(lowered)),
        "everyone": mentions_everyone or "@everyone" in lowered or "@here" in lowered,
        "attachment": has_attachment,
    }


def is_exempt(member, permission_service=None) -> bool:
    """True for anyone who must never be punished: bots, staff, and moderators
    (Administrator / Manage Messages / Ban Members). `member` may be a bare User."""
    if getattr(member, "bot", False):
        return True
    owner_id = getattr(_config, "OWNER_ID", 0)
    if owner_id and getattr(member, "id", None) == owner_id:
        return True  # by id: works even for a bare User whose Member lookup failed
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_messages or perms.ban_members):
        return True
    if permission_service is not None and hasattr(member, "roles"):
        try:
            if permission_service.is_staff(member):
                return True
        except Exception:
            log.exception("honeypot: is_staff check failed for %s; treating as exempt to be safe", member)
            return True
    return False


def _pct(n: int, total: int) -> float | None:
    return round(100.0 * n / total, 1) if total else None


def compute_stats(events: list[dict], appeal_counts: dict[str, int], now: datetime | None = None) -> dict:
    """Aggregates honeypot_events rows (see HoneypotRepository.get_all_events_for_stats)
    into the `stats` object of the contract. Empty input yields zeros / nulls."""
    now = now or _now()
    total = len(events)
    created = [parse_ts(e.get("created_at")) for e in events]

    def within(delta: timedelta) -> int:
        cutoff = now - delta
        return sum(1 for c in created if c is not None and c >= cutoff)

    totals = {
        "all": total,
        "last_24h": within(timedelta(hours=24)),
        "last_7d": within(timedelta(days=7)),
        "last_30d": within(timedelta(days=30)),
    }
    by_action = {a: sum(1 for e in events if e.get("action") == a) for a in ACTIONS}
    by_result = {r: sum(1 for e in events if e.get("action_result") == r) for r in ("ok", "partial", "failed")}

    today = now.date()
    days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    per_day = Counter(c.date() for c in created if c is not None)
    timeline = [{"date": d.isoformat(), "count": per_day.get(d, 0)} for d in days]

    by_hour = [0] * 24
    for c in created:
        if c is not None:
            by_hour[c.hour] += 1

    ages: list[float] = []
    for e, c in zip(events, created):
        acct = parse_ts(e.get("account_created_at"))
        if acct is not None and c is not None:
            ages.append(max(0.0, (c - acct).total_seconds() / 86400.0))
    if ages:
        account_age = {
            "avg_days": round(sum(ages) / len(ages), 1),
            "median_days": round(statistics.median(ages), 1),
            "under_7d_pct": _pct(sum(1 for a in ages if a < 7), len(ages)),
            "under_30d_pct": _pct(sum(1 for a in ages if a < 30), len(ages)),
        }
    else:
        account_age = {"avg_days": None, "median_days": None, "under_7d_pct": None, "under_30d_pct": None}

    secs = [e["seconds_since_join"] for e in events if e.get("seconds_since_join") is not None]
    if secs:
        time_to_catch = {
            "median_seconds": round(float(statistics.median(secs)), 1),
            "under_60s_pct": _pct(sum(1 for s in secs if s < 60), len(secs)),
            "under_1h_pct": _pct(sum(1 for s in secs if s < 3600), len(secs)),
        }
    else:
        time_to_catch = {"median_seconds": None, "under_60s_pct": None, "under_1h_pct": None}

    def sig(key: str) -> float | None:
        return _pct(sum(1 for e in events if e.get(key)), total)

    signals = {
        "default_avatar_pct": sig("had_default_avatar"),
        "link_pct": sig("flag_link"),
        "invite_pct": sig("flag_invite"),
        "everyone_pct": sig("flag_everyone"),
        "attachment_pct": sig("flag_attachment"),
        "dm_delivered_pct": sig("dm_sent"),
    }

    normalised = Counter()
    for e in events:
        text = re.sub(r"\s+", " ", (e.get("content") or "").lower()).strip()
        if text:
            normalised[text] += 1
    repeated = [(t, n) for t, n in normalised.items() if n >= 2]
    repeated.sort(key=lambda p: (-p[1], p[0]))
    top_messages = [{"preview": t[:100], "count": n} for t, n in repeated[:5]]

    appeal_total = sum(appeal_counts.values())
    total_bans = by_action["ban"]
    appeals = {
        "total": appeal_total,
        "invited": appeal_counts.get("invited", 0),
        "pending": appeal_counts.get("pending", 0),
        "approved": appeal_counts.get("approved", 0),
        "denied": appeal_counts.get("denied", 0),
        "false_positive_pct": _pct(appeal_counts.get("approved", 0), total_bans),
    }

    valid = [c for c in created if c is not None]
    return {
        "totals": totals,
        "by_action": by_action,
        "by_result": by_result,
        "timeline_30d": timeline,
        "by_hour_utc": by_hour,
        "account_age": account_age,
        "time_to_catch": time_to_catch,
        "signals": signals,
        "top_messages": top_messages,
        "appeals": appeals,
        "first_catch_at": min(valid).isoformat() if valid else None,
        "last_catch_at": max(valid).isoformat() if valid else None,
    }


def catch_to_dict(row: dict) -> dict:
    """honeypot_events row -> `catch` object of the contract."""
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "username": row.get("username"),
        "display_name": row.get("display_name"),
        "avatar_url": row.get("avatar_url"),
        "channel_id": row.get("channel_id"),
        "content": row.get("content"),
        "action": row["action"],
        "action_result": row["action_result"],
        "errors": list(row.get("errors") or []),
        "dm_sent": bool(row.get("dm_sent")),
        "account_created_at": row.get("account_created_at"),
        "joined_at": row.get("joined_at"),
        "seconds_since_join": row.get("seconds_since_join"),
        "had_default_avatar": bool(row.get("had_default_avatar")),
        "flags": {
            "link": bool(row.get("flag_link")),
            "invite": bool(row.get("flag_invite")),
            "everyone": bool(row.get("flag_everyone")),
            "attachment": bool(row.get("flag_attachment")),
        },
        "created_at": row["created_at"],
        "appeal_status": row.get("appeal_status"),
        "via_app": (
            {"id": str(row["via_app_id"]), "name": row.get("via_app_name")}
            if row.get("via_app_id") else None
        ),
    }


def appeal_to_dict(row: dict) -> dict:
    """honeypot_appeals row -> `appeal` object of the contract (token is never exposed)."""
    keys = (
        "id", "user_id", "username", "name", "message", "status", "catch_id", "created_at",
        "submitted_at", "reviewed_by", "reviewed_at", "review_note",
    )
    out = {k: row.get(k) for k in keys}
    # False = filed through the public form with only a typed user id (no DM token).
    out["verified"] = bool(row.get("verified", 1))
    # Migration 030: how it was verified ('dm_link' | 'discord_login' | None), whether the
    # outcome DM reached them (None = not tried), and when they used "Rejoin server".
    out["verified_via"] = row.get("verified_via")
    sent = row.get("outcome_dm_sent")
    out["outcome_dm_sent"] = None if sent is None else bool(sent)
    out["rejoined_at"] = row.get("rejoined_at")
    out["is_test"] = bool(row.get("is_test"))  # Debug-page test case (migration 031)
    return out


def _name_of(obj) -> str | None:
    name = getattr(obj, "name", None)
    return name if isinstance(name, str) else None


def split_decoration(name: str) -> tuple[str, str, str]:
    """'🚫┊do-not-type' -> ('🚫┊', 'do-not-type', ''); '───⋅ Ignore ⋅───' ->
    ('───⋅ ', 'Ignore', ' ⋅───'). Decoration = leading/trailing non-word characters."""
    m = _DECORATION_RE.match(name or "")
    assert m is not None  # the pattern matches any string
    return m.group(1), m.group(2), m.group(3)


def _gibberish_channel_core(rng) -> str:
    groups = rng.choice((1, 2, 2, 3))
    return "-".join(
        "".join(rng.choice(_SCRAMBLE_CHANNEL_CHARS) for _ in range(rng.randint(2, 5))) for _ in range(groups)
    )


def _gibberish_category_core(rng) -> str:
    words = rng.choice((1, 1, 2))
    return " ".join(
        "".join(rng.choice(_SCRAMBLE_WORD_CHARS) for _ in range(rng.randint(4, 6))).capitalize() for _ in range(words)
    )


def scrambled_name(old: str, kind: str, rng) -> str:
    """A new random name for the trap channel (kind 'channel') or its category ('category'),
    keeping the old name's decoration and never returning the same words."""
    prefix, core, suffix = split_decoration(old)
    make = _gibberish_channel_core if kind == "channel" else _gibberish_category_core
    new = make(rng)
    for _ in range(5):
        if new.lower() != core.lower():
            break
        new = make(rng)
    return (prefix + new + suffix)[:100]


def scramble_due(row: dict | None, now: datetime | None = None) -> bool:
    """True when scrambling is on and the interval since the last scramble has passed
    (a config that was never scrambled is due at once)."""
    if not row or not row.get("scramble_enabled") or not row.get("channel_id"):
        return False
    last = parse_ts(row.get("last_scrambled_at"))
    if last is None:
        return True
    hours = row.get("scramble_interval_hours") or SCRAMBLE_DEFAULT_HOURS
    return (now or _now()) - last >= timedelta(hours=hours)


_SCRAMBLE_ERROR_TEXT = {
    "too_soon": "The names were changed less than 10 minutes ago; Discord allows 2 renames per 10 minutes. Try again shortly.",
    "rate_limited": "Discord is rate limiting renames (2 per 10 minutes). Try again in a few minutes.",
    "channel_missing": "The honeypot channel no longer exists. Run setup again.",
}


def scramble_error_text(exc: HoneypotError) -> str:
    """Readable text for a scramble HoneypotError (409 ones carry only their code on the wire)."""
    return _SCRAMBLE_ERROR_TEXT.get(exc.code, exc.detail)


def _next_scramble_at(row: dict) -> str | None:
    if not row.get("scramble_enabled"):
        return None
    last = parse_ts(row.get("last_scrambled_at"))
    if last is None:
        return None  # never scrambled: the next check (within minutes) does it
    hours = row.get("scramble_interval_hours") or SCRAMBLE_DEFAULT_HOURS
    return (last + timedelta(hours=hours)).isoformat()


def _fmt_seconds(secs: int | None) -> str:
    if secs is None:
        return "unknown"
    if secs < 120:
        return f"{secs}s"
    if secs < 7200:
        return f"{secs // 60}m"
    if secs < 172800:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


class HoneypotService:
    def __init__(self, bot, repo: HoneypotRepository, moderation_service, audit=None) -> None:
        self._bot = bot
        self._repo = repo
        self._moderation = moderation_service
        self._audit = audit
        self._cfg_cache: dict[int, dict | None] = {}
        self._inflight: set[tuple[int, int]] = set()
        self._recent: dict[tuple[int, int], float] = {}
        self._last_refresh: dict[int, float] = {}
        self._pending_refresh: dict[int, asyncio.Task] = {}
        self._appeal_lock = asyncio.Lock()
        self._scramble_lock = asyncio.Lock()
        self._scramble_retry_at: dict[int, float] = {}  # guild -> monotonic time a failed scheduled scramble may retry

    # ------------------------------------------------------------------
    # Live `honeypot` websocket topic. Every call is failure-proof: a broken
    # websocket must never break the catch path or an appeal review.
    # ------------------------------------------------------------------
    @staticmethod
    def _live(kind: str, **data) -> None:
        live_publish("honeypot", {"kind": kind, **data})

    async def _live_stats(self, guild_id: int) -> None:
        if not live_hub.has_subscribers("honeypot"):
            return  # nobody watching: skip the DB read
        try:
            self._live("stats", guild_id=guild_id, stats=await self.get_stats(guild_id))
        except Exception:
            log.debug("honeypot: live stats publish failed", exc_info=True)

    async def _live_appeal(self, kind: str, guild_id: int, appeal_id: int) -> None:
        if not live_hub.has_subscribers("honeypot"):
            return
        try:
            row = await self._repo.get_appeal(appeal_id)
            self._live(kind, guild_id=guild_id, appeal_id=appeal_id, appeal=appeal_to_dict(row) if row else None)
            await self._live_stats(guild_id)
        except Exception:
            log.debug("honeypot: live appeal publish failed", exc_info=True)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    async def _cached_row(self, guild_id: int) -> dict | None:
        if guild_id not in self._cfg_cache:
            self._cfg_cache[guild_id] = await self._repo.get_config(guild_id)
        return self._cfg_cache[guild_id]

    def _invalidate(self, guild_id: int) -> None:
        self._cfg_cache.pop(guild_id, None)

    def _channel_exists(self, guild_id: int, channel_id: int | None) -> bool:
        if not channel_id:
            return False
        guild = self._bot.get_guild(guild_id)
        return guild is not None and guild.get_channel(channel_id) is not None

    async def get_config(self, guild_id: int) -> dict:
        row = await self._repo.get_config(guild_id)
        base = {
            "configured": False, "enabled": False, "channel_id": None, "category_id": None,
            "channel_exists": False, "action": "ban", "dm_before": True, "appeal_enabled": True,
            "delete_message_days": 1, "log_channel_id": None, "warning_message_id": None,
            "stats_message_id": None, "appeal_base_url": _config.APPEAL_BASE_URL,
            # name scrambling (migration 029); channel_name / category_name are the live Discord names
            "scramble_enabled": False, "scramble_interval_hours": SCRAMBLE_DEFAULT_HOURS,
            "last_scrambled_at": None, "next_scramble_at": None, "channel_name": None, "category_name": None,
            # rejoin link (migration 030): shown on approved appeals; optionally in kick/softban DMs
            "rejoin_invite_url": None, "invite_in_removal_dm": False,
        }
        if row is None or not row.get("channel_id"):
            if row is not None:
                # Partially-configured row (e.g. channel deleted and IDs cleared): keep the user's settings.
                for k in ("enabled", "action", "dm_before", "appeal_enabled", "delete_message_days", "log_channel_id",
                          "scramble_enabled", "scramble_interval_hours", "last_scrambled_at",
                          "rejoin_invite_url", "invite_in_removal_dm"):
                    base[k] = row.get(k, base[k])
            return base
        for k in (
            "enabled", "channel_id", "category_id", "action", "dm_before", "appeal_enabled",
            "delete_message_days", "log_channel_id", "warning_message_id", "stats_message_id",
            "scramble_enabled", "scramble_interval_hours", "last_scrambled_at",
            "rejoin_invite_url", "invite_in_removal_dm",
        ):
            base[k] = row.get(k, base[k])
        base["next_scramble_at"] = _next_scramble_at(row)
        base["configured"] = True
        base["channel_exists"] = self._channel_exists(guild_id, row["channel_id"])
        guild = self._bot.get_guild(guild_id)
        channel, category = self._trap_objects(guild, row) if guild is not None else (None, None)
        base["channel_name"] = _name_of(channel)
        base["category_name"] = _name_of(category)
        return base

    @staticmethod
    def _trap_objects(guild, row: dict | None):
        """(trap text channel, the category it is in) from the live cache; either may be None."""
        channel = guild.get_channel(row["channel_id"]) if row and row.get("channel_id") else None
        if not isinstance(channel, discord.TextChannel):
            return None, None
        category = getattr(channel, "category", None)
        return channel, category if isinstance(category, discord.CategoryChannel) else None

    async def update_config(self, guild: discord.Guild, changes: dict) -> dict:
        row = await self._repo.get_config(guild.id)
        if row is None or not row.get("channel_id"):
            raise HoneypotError("not_configured", "The honeypot has not been set up yet. Run setup first.")

        clean: dict = {}
        for key, value in changes.items():
            if key in ("enabled", "dm_before", "appeal_enabled", "scramble_enabled", "invite_in_removal_dm"):
                if not isinstance(value, bool):
                    raise HoneypotError("invalid", f"{key} must be a boolean.")
                clean[key] = value
            elif key == "action":
                if value not in ACTIONS:
                    raise HoneypotError("invalid", f"action must be one of {', '.join(ACTIONS)}.")
                clean[key] = value
            elif key == "delete_message_days":
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 7:
                    raise HoneypotError("invalid", "delete_message_days must be an integer from 0 to 7.")
                clean[key] = value
            elif key == "rejoin_invite_url":
                clean[key] = await self._validated_invite_url(guild, value)
            elif key == "scramble_interval_hours":
                if (isinstance(value, bool) or not isinstance(value, int)
                        or not SCRAMBLE_MIN_HOURS <= value <= SCRAMBLE_MAX_HOURS):
                    raise HoneypotError(
                        "invalid",
                        f"scramble_interval_hours must be an integer from {SCRAMBLE_MIN_HOURS} to {SCRAMBLE_MAX_HOURS}.",
                    )
                clean[key] = value
            elif key == "log_channel_id":
                if value in (None, 0):
                    clean[key] = None
                else:
                    ch = guild.get_channel(int(value))
                    if not isinstance(ch, discord.TextChannel):
                        raise HoneypotError("invalid", "log_channel_id must be a text channel in this server.")
                    clean[key] = ch.id
            else:
                raise HoneypotError("invalid", f"Unknown setting {key!r}.")

        if clean.get("enabled") and not self._channel_exists(guild.id, row["channel_id"]):
            raise HoneypotError(
                "channel_missing",
                "The honeypot channel no longer exists, so it can't be enabled. Run setup again.",
            )

        await self._repo.upsert_config(guild.id, **clean)
        self._invalidate(guild.id)
        if clean.keys() & {"action", "enabled"}:
            await self._safe_refresh(guild)
        cfg_out = await self.get_config(guild.id)
        self._live("config_changed", guild_id=guild.id, config=cfg_out)
        return cfg_out

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    async def setup(
        self,
        guild: discord.Guild,
        actor_id: int,
        *,
        category_name: str | None = None,
        channel_name: str | None = None,
        position_top: bool = True,
        action: str | None = None,
        dm_before: bool | None = None,
        appeal_enabled: bool | None = None,
    ) -> tuple[dict, list[str]]:
        category_name = (category_name or DEFAULT_CATEGORY_NAME).strip()
        channel_name = (channel_name or DEFAULT_CHANNEL_NAME).strip()
        if not 1 <= len(category_name) <= 100 or not 1 <= len(channel_name) <= 100:
            raise HoneypotError("invalid", "category_name and channel_name must be 1-100 characters.")
        if action is not None and action not in ACTIONS:
            raise HoneypotError("invalid", f"action must be one of {', '.join(ACTIONS)}.")

        existing = await self._repo.get_config(guild.id)
        if existing and existing.get("channel_id") and self._channel_exists(guild.id, existing["channel_id"]):
            raise HoneypotError("already_configured", status=409)

        warnings: list[str] = []
        me = guild.me
        if me is not None:
            perms = me.guild_permissions
            if not perms.manage_channels:
                raise HoneypotError(
                    "missing_permissions",
                    "The bot is missing the Manage Channels permission, so it can't create the honeypot channel.",
                    status=502,
                )
            if not perms.ban_members:
                warnings.append("The bot is missing Ban Members - bans/softbans will fail until that is granted.")
            if not perms.kick_members and not perms.ban_members:
                warnings.append("The bot is missing Kick Members - kicks will fail.")
            if not perms.manage_messages:
                warnings.append("The bot is missing Manage Messages - it may not be able to delete trap messages.")

        if existing and existing.get("category_id") and guild.get_channel(existing["category_id"]) is not None:
            warnings.append("The previous honeypot category still exists; you may want to delete it.")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
            ),
        }
        category = None
        channel = None
        try:
            cat_kwargs: dict = {"overwrites": overwrites, "reason": "Honeypot setup"}
            if position_top:
                cat_kwargs["position"] = 0
            category = await guild.create_category(category_name, **cat_kwargs)
            channel = await guild.create_text_channel(
                channel_name, category=category, overwrites=overwrites,
                topic="Trap channel for hacked accounts and bots. Do NOT type here.",
                reason="Honeypot setup",
            )
        except discord.Forbidden as exc:
            await self._cleanup_failed_setup(category, channel)
            raise HoneypotError(
                "missing_permissions",
                "Discord refused: the bot needs the Manage Channels permission to create the honeypot.",
                status=502,
            ) from exc
        except discord.HTTPException as exc:
            await self._cleanup_failed_setup(category, channel)
            raise HoneypotError("discord_error", f"Discord error while creating the honeypot: {exc}", status=502) from exc

        fields: dict = {
            "enabled": True, "channel_id": channel.id, "category_id": category.id,
            "warning_message_id": None, "stats_message_id": None,
        }
        if action is not None:
            fields["action"] = action
        if dm_before is not None:
            fields["dm_before"] = bool(dm_before)
        if appeal_enabled is not None:
            fields["appeal_enabled"] = bool(appeal_enabled)
        await self._repo.upsert_config(guild.id, **fields)
        self._invalidate(guild.id)

        cfg = await self.get_config(guild.id)
        try:
            warn_msg = await channel.send(embed=self._build_warning_embed())
            fields_msg: dict = {"warning_message_id": warn_msg.id}
            count, last = await self._catch_summary(guild.id)
            stats_msg = await channel.send(embed=self._build_caught_embed(cfg, count, last))
            fields_msg["stats_message_id"] = stats_msg.id
            await self._repo.upsert_config(guild.id, **fields_msg)
        except discord.HTTPException as exc:
            warnings.append(f"Channel created, but posting the embeds failed: {exc}")
            log.warning("honeypot: posting setup embeds failed: %s", exc)
            # Keep whatever ids were stored above; refresh can repost the stats embed later.
        self._invalidate(guild.id)
        self._last_refresh[guild.id] = time.monotonic()
        log.info("honeypot: set up by %s in guild %s (channel %s)", actor_id, guild.id, channel.id)
        cfg_out = await self.get_config(guild.id)
        self._live("config_changed", guild_id=guild.id, config=cfg_out)
        return cfg_out, warnings

    async def _cleanup_failed_setup(self, category, channel) -> None:
        for obj in (channel, category):
            if obj is not None:
                try:
                    await obj.delete(reason="Honeypot setup failed; rolling back")
                except Exception:
                    log.warning("honeypot: could not roll back %s after failed setup", obj)

    # ------------------------------------------------------------------
    # Embeds
    # ------------------------------------------------------------------
    @staticmethod
    def _build_warning_embed() -> discord.Embed:
        embed = discord.Embed(
            title="🚫 Please do NOT type in this channel",
            description=(
                "**This channel is a trap for hacked accounts and bots.**\n"
                "Do **NOT** type here - you will be removed automatically.\n\n"
                "Regular members never need to post in this channel. "
                "If you can read this, you are safe: just head to another channel and enjoy the server."
            ),
            color=discord.Color.red(),
        )
        embed.set_footer(text="This is an automatic anti-spam measure.")
        return embed

    def _build_caught_embed(self, cfg: dict, count: int, last: datetime | None) -> discord.Embed:
        embed = discord.Embed(title="🪤 Caught so far", color=discord.Color.dark_gold())
        embed.add_field(name="Accounts caught", value=str(count), inline=True)
        embed.add_field(
            name="Last catch",
            value=f"<t:{int(last.timestamp())}:R>" if last else "Nobody yet",
            inline=True,
        )
        state = cfg["action"].capitalize() if cfg.get("enabled") else "Disabled"
        embed.add_field(name="Action in force", value=state, inline=True)
        embed.timestamp = _now()
        return embed

    async def _catch_summary(self, guild_id: int) -> tuple[int, datetime | None]:
        count = await self._repo.count_events(guild_id)
        last = await self._repo.get_last_event(guild_id)
        return count, parse_ts(last["created_at"]) if last else None

    async def _safe_refresh(self, guild: discord.Guild) -> None:
        try:
            await self.refresh_stats_message(guild)
        except Exception:
            log.exception("honeypot: stats refresh failed")

    async def refresh_stats_message(self, guild: discord.Guild, *, force: bool = False) -> str | None:
        """Edits the live 'Caught so far' embed. Debounced to one edit per REFRESH_MIN_INTERVAL:
        a call inside the window schedules a single delayed edit that picks up the latest state.
        Returns an error string on failure, else None."""
        if not force:
            elapsed = time.monotonic() - self._last_refresh.get(guild.id, 0.0)
            if elapsed < REFRESH_MIN_INTERVAL:
                task = self._pending_refresh.get(guild.id)
                if task is None or task.done():
                    self._pending_refresh[guild.id] = asyncio.create_task(
                        self._delayed_refresh(guild, REFRESH_MIN_INTERVAL - elapsed)
                    )
                return None
        return await self._do_refresh(guild)

    async def _delayed_refresh(self, guild: discord.Guild, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
            await self._do_refresh(guild)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("honeypot: delayed stats refresh failed")

    async def _do_refresh(self, guild: discord.Guild) -> str | None:
        self._last_refresh[guild.id] = time.monotonic()
        try:
            row = await self._repo.get_config(guild.id)
            if row is None or not row.get("channel_id"):
                return None
            channel = guild.get_channel(row["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                return None
            cfg = await self.get_config(guild.id)
            count, last = await self._catch_summary(guild.id)
            embed = self._build_caught_embed(cfg, count, last)
            if row.get("stats_message_id"):
                try:
                    msg = await channel.fetch_message(row["stats_message_id"])
                    await msg.edit(embed=embed)
                    return None
                except discord.NotFound:
                    pass  # deleted by hand: repost below
            new_msg = await channel.send(embed=embed)
            await self._repo.upsert_config(guild.id, stats_message_id=new_msg.id)
            self._invalidate(guild.id)
            return None
        except discord.Forbidden as exc:
            return f"stats_refresh_forbidden: {exc}"
        except Exception as exc:
            log.exception("honeypot: refreshing stats embed failed")
            return f"stats_refresh_failed: {exc}"

    async def close(self) -> None:
        for task in list(self._pending_refresh.values()):
            task.cancel()
        self._pending_refresh.clear()

    # ------------------------------------------------------------------
    # Catch path
    # ------------------------------------------------------------------
    async def handle_message(self, message: discord.Message, permission_service=None) -> None:
        """Entry point for the on_message listener. Never raises."""
        try:
            await self._handle_message(message, permission_service)
        except Exception:
            log.exception("honeypot: unexpected error handling message %s", getattr(message, "id", "?"))

    async def _handle_message(self, message: discord.Message, permission_service) -> None:
        guild = message.guild
        if guild is None:
            return
        me_id = getattr(getattr(self._bot, "user", None), "id", None)
        if me_id is not None and getattr(message.author, "id", None) == me_id:
            return  # our own warning / "Caught so far" embeds
        cfg = await self._cached_row(guild.id)
        if not cfg or not cfg["enabled"] or not cfg["channel_id"] or message.channel.id != cfg["channel_id"]:
            return

        via_app = app_source(message)
        if via_app is not None:
            # An app posted because a member ran its command: that member is caught.
            author = via_app["invoker"]
            if me_id is not None and getattr(author, "id", None) == me_id:
                return
        elif message.webhook_id is not None or getattr(message.author, "bot", False):
            await self._handle_foreign(guild, message, cfg)
            return
        else:
            author = message.author

        if not isinstance(author, discord.Member):
            resolved = guild.get_member(author.id)
            if resolved is None:
                try:
                    resolved = await guild.fetch_member(author.id)
                except discord.NotFound:
                    resolved = None  # genuinely not in the guild (e.g. spammed then left)
                except Exception:
                    log.exception("honeypot: member lookup failed for %s; not punishing", author.id)
                    return  # can't tell whether they are staff: fail closed
            author = resolved or author
        if is_exempt(author, permission_service):
            return
        if permission_service is not None and isinstance(author, discord.Member):
            # Staff defined by dashboard/DB permission grants (not just OWNER_ID/STAFF_ROLE_IDS).
            try:
                if await permission_service.has_any_grant(author):
                    return
            except Exception:
                log.exception("honeypot: grant check failed for %s; treating as exempt to be safe", author.id)
                return

        key = (guild.id, author.id)
        now = time.monotonic()
        self._recent = {k: t for k, t in self._recent.items() if now - t < CATCH_COOLDOWN}
        if key in self._inflight or key in self._recent:
            # Part of a burst we're already acting on: just tidy up.
            await self._delete_quietly(message)
            return

        self._inflight.add(key)
        try:
            await self._catch(guild, message, author, cfg, via_app=via_app)
        finally:
            self._inflight.discard(key)
            self._recent[key] = time.monotonic()

    @staticmethod
    async def _delete_quietly(message: discord.Message) -> str | None:
        try:
            await message.delete()
            return None
        except discord.NotFound:
            return None
        except discord.Forbidden:
            return "delete_forbidden: bot lacks Manage Messages in the honeypot channel"
        except Exception as exc:
            return f"delete_failed: {exc}"

    async def _handle_foreign(self, guild: discord.Guild, message: discord.Message, cfg: dict) -> None:
        """A webhook or a bot posting on its own in the trap: nobody to punish.
        Delete the message (and the webhook); log once per source per cooldown."""
        is_webhook = message.webhook_id is not None
        source_id = message.webhook_id if is_webhook else getattr(message.author, "id", 0)
        errors: list[str] = []
        err = await self._delete_quietly(message)
        if err:
            errors.append(err)

        key = (guild.id, -int(source_id or 0))  # negative: never collides with a user key
        now = time.monotonic()
        if now - self._recent.get(key, -CATCH_COOLDOWN) < CATCH_COOLDOWN:
            return  # same burst: already deleted, already logged
        self._recent[key] = now

        webhook_deleted = None
        if is_webhook:
            webhook_deleted, werr = await self._delete_webhook(message.channel, message.webhook_id)
            if werr:
                errors.append(werr)

        name = getattr(message.author, "name", None) or "unknown"
        kind = "webhook" if is_webhook else "bot"
        content = message_text(message)[:CONTENT_MAX]
        log.warning(
            "honeypot: removed %s message from %s (%s) in guild %s; webhook_deleted=%s errors=%s",
            kind, name, source_id, guild.id, webhook_deleted, errors,
        )
        if self._audit is not None:
            bot_id = getattr(getattr(self._bot, "user", None), "id", None) or 0
            try:
                await self._audit.log(
                    guild.id, bot_id,
                    "honeypot_foreign_message", target_type=kind, target_id=source_id,
                    detail={"source": kind, "name": name, "channel_id": message.channel.id,
                            "webhook_deleted": webhook_deleted, "errors": errors, "content": content[:200]},
                )
            except Exception:
                log.exception("honeypot: writing the foreign-message audit row failed")

        log_channel = self._resolve_log_channel(guild, cfg)
        if log_channel is None:
            return
        try:
            embed = discord.Embed(
                title=f"Honeypot: {kind} message removed",
                color=discord.Color.orange(),
                timestamp=_now(),
            )
            embed.add_field(name="Posted by", value=f"{discord.utils.escape_markdown(name)} (`{source_id}`)", inline=False)
            if is_webhook:
                if webhook_deleted:
                    note = "The webhook was deleted."
                elif webhook_deleted is False:
                    note = "The webhook was not found in this channel (already gone, or it belongs to an app)."
                else:
                    note = "Could not delete the webhook; remove it in Channel settings > Integrations."
            else:
                note = ("This is a bot in the server, so it was not kicked automatically. If you did not add it, "
                        "remove it in Server settings > Integrations.")
            embed.add_field(name="Action", value=note, inline=False)
            shown = content.replace("`", "'")[:900] or "(no text)"
            embed.add_field(name="Message", value=f"```{shown}```", inline=False)
            if errors:
                embed.add_field(name="Errors", value="\n".join(errors)[:1000], inline=False)
            await log_channel.send(embed=embed)
        except Exception:
            log.exception("honeypot: posting the foreign-message log failed")

    @staticmethod
    async def _delete_webhook(channel, webhook_id: int) -> tuple[bool | None, str | None]:
        """(deleted?, error). False = no such webhook in the channel."""
        try:
            for hook in await channel.webhooks():
                if hook.id == webhook_id:
                    await hook.delete(reason="Honeypot: webhook posted in the honeypot channel")
                    return True, None
            return False, None
        except discord.Forbidden:
            return None, "webhook_delete_forbidden: bot lacks Manage Webhooks"
        except Exception as exc:
            return None, f"webhook_delete_failed: {exc}"

    async def _catch(self, guild: discord.Guild, message: discord.Message, author, cfg: dict, via_app: dict | None = None) -> None:
        errors: list[str] = []
        action = cfg["action"] if cfg["action"] in ACTIONS else "ban"
        channel = message.channel
        raw_content = (message_text(message) if via_app else message.content) or ""
        content = raw_content[:CONTENT_MAX]

        # --- 1. record facts, delete the message -------------------------
        flags = message_flags(raw_content, bool(message.attachments), bool(message.mention_everyone))
        joined_at = getattr(author, "joined_at", None)
        created_at = getattr(author, "created_at", None)
        seconds_since_join = None
        if joined_at is not None:
            seconds_since_join = max(0, int((_now() - joined_at).total_seconds()))
        try:
            avatar_url = author.display_avatar.url
        except Exception:
            avatar_url = None
        had_default_avatar = getattr(author, "avatar", None) is None

        err = await self._delete_quietly(message)
        if err:
            errors.append(err)

        # --- 2. DM before acting ----------------------------------------
        dm_sent = False
        appeal_id: int | None = None
        # If punishing this user already failed within the last hour (e.g. the bot cannot ban
        # them), don't DM them / open another appeal row every cooldown: at most one per hour.
        suppress_dm = False
        try:
            since = (_now() - timedelta(hours=FAILED_ACTION_DM_COOLDOWN_HOURS)).isoformat()
            suppress_dm = await self._repo.has_failed_event_since(guild.id, author.id, since)
        except Exception:
            log.exception("honeypot: failed-action lookup failed")
        if cfg["dm_before"] and not suppress_dm:
            appeal_url = None
            if action == "ban" and cfg["appeal_enabled"]:
                try:
                    token = secrets.token_urlsafe(24)
                    appeal_id = await self._repo.create_appeal(
                        guild.id, author.id, token, username=getattr(author, "name", None),
                    )
                    appeal_url = f"{_config.APPEAL_BASE_URL}/appeal/{token}"
                except Exception as exc:
                    log.exception("honeypot: creating appeal row failed")
                    errors.append(f"appeal_create_failed: {exc}")
            invite_url = (
                cfg.get("rejoin_invite_url") if cfg.get("invite_in_removal_dm") and action in ("kick", "softban") else None
            )
            try:
                await author.send(embed=self._build_dm_embed(guild, channel, action, appeal_url, invite_url))
                dm_sent = True
            except (discord.Forbidden, discord.NotFound):
                pass  # closed DMs / gone: normal, recorded as dm_sent=false
            except Exception as exc:
                errors.append(f"dm_failed: {exc}")

        # --- 3. apply the action ----------------------------------------
        action_ok, action_errors, soft_ok = await self._apply_action(guild, author, action, cfg, channel, via_app)
        errors.extend(action_errors)
        if action == "softban" and action_ok and not soft_ok:
            # The pre-action DM has already gone out; make the still-banned state impossible to miss.
            errors.insert(0, "softban unban failed - user is STILL BANNED, unban manually")
        if not action_ok:
            result = "failed"
        elif errors:
            result = "partial"
        else:
            result = "ok"

        # --- 4. persist the event ---------------------------------------
        event_id: int | None = None
        try:
            event_id = await self._repo.add_event(
                guild.id, author.id, action,
                username=getattr(author, "name", None),
                display_name=getattr(author, "display_name", None),
                avatar_url=avatar_url,
                channel_id=channel.id,
                content=content,
                action_result=result,
                errors=errors,
                dm_sent=dm_sent,
                account_created_at=created_at.isoformat() if created_at else None,
                joined_at=joined_at.isoformat() if joined_at else None,
                seconds_since_join=seconds_since_join,
                had_default_avatar=had_default_avatar,
                flag_link=flags["link"], flag_invite=flags["invite"],
                flag_everyone=flags["everyone"], flag_attachment=flags["attachment"],
                via_app_id=via_app["app_id"] if via_app else None,
                via_app_name=via_app["app_name"] if via_app else None,
            )
            if appeal_id is not None:
                await self._repo.set_appeal_catch(appeal_id, event_id)
        except Exception:
            log.exception("honeypot: recording catch event failed (user %s)", author.id)

        if event_id is not None and live_hub.has_subscribers("honeypot"):
            try:
                self._live("catch_created", guild_id=guild.id, catch=catch_to_dict({
                    "id": event_id, "user_id": author.id, "username": getattr(author, "name", None),
                    "display_name": getattr(author, "display_name", None), "avatar_url": avatar_url,
                    "channel_id": channel.id, "content": content, "action": action, "action_result": result,
                    "errors": errors, "dm_sent": dm_sent,
                    "account_created_at": created_at.isoformat() if created_at else None,
                    "joined_at": joined_at.isoformat() if joined_at else None,
                    "seconds_since_join": seconds_since_join, "had_default_avatar": had_default_avatar,
                    "flag_link": flags["link"], "flag_invite": flags["invite"],
                    "flag_everyone": flags["everyone"], "flag_attachment": flags["attachment"],
                    "created_at": _now().isoformat(),
                    "appeal_status": "invited" if appeal_id is not None else None,
                    "via_app_id": via_app["app_id"] if via_app else None,
                    "via_app_name": via_app["app_name"] if via_app else None,
                }))
                await self._live_stats(guild.id)
            except Exception:
                log.debug("honeypot: live catch publish failed", exc_info=True)

        # --- 5. log post, 6. embed refresh ------------------------------
        log_err = await self._post_catch_log(
            guild, cfg, author, action, result, errors, content, flags, seconds_since_join,
            created_at, dm_sent, channel, via_app,
        )
        if log_err and event_id is not None:
            try:
                await self._repo.append_event_error(event_id, log_err)
            except Exception:
                log.exception("honeypot: could not append log error to event %s", event_id)
        await self._safe_refresh(guild)
        log.info("honeypot: caught user %s in guild %s action=%s result=%s via_app=%s",
                 author.id, guild.id, action, result, describe_app(via_app))

    async def _apply_action(self, guild, author, action: str, cfg: dict, channel, via_app: dict | None = None) -> tuple[bool, list[str], bool]:
        """Returns (punishment_applied, errors, softban_unban_ok)."""
        errors: list[str] = []
        moderator = self._bot.user
        # Must keep starting with "Honeypot": approve_appeal checks the ban reason.
        reason = f"Honeypot: posted in #{getattr(channel, 'name', 'honeypot')}"
        if via_app:
            reason += f" via app {via_app.get('app_name') or via_app.get('app_id')}"
        member = author if isinstance(author, discord.Member) else None
        days = int(cfg["delete_message_days"])

        if action == "kick":
            if member is None:
                errors.append("kick_failed: user is no longer in the server")
                return False, errors, True
            try:
                await self._moderation.kick(member, guild, moderator, reason)
                return True, errors, True
            except discord.Forbidden:
                errors.append("kick_forbidden: bot lacks Kick Members or the user outranks the bot")
            except Exception as exc:
                errors.append(f"kick_failed: {exc}")
            return False, errors, True

        if action == "softban":
            days = max(1, days)
        try:
            await self._moderation.ban(
                guild, author.id, moderator, reason, member=member, delete_message_days=days,
            )
        except discord.Forbidden:
            errors.append("ban_forbidden: bot lacks Ban Members or the user outranks the bot")
            return False, errors, True
        except Exception as exc:
            errors.append(f"ban_failed: {exc}")
            return False, errors, True

        if action == "softban":
            try:
                await self._moderation.unban(
                    guild, author.id, moderator,
                    f"Honeypot softban: unbanning after cleanup ({reason})",
                )
            except Exception as exc:
                errors.append(f"softban_unban_failed: {exc}")
                return True, errors, False
        return True, errors, True

    def _build_dm_embed(
        self, guild, channel, action: str, appeal_url: str | None, invite_url: str | None = None,
    ) -> discord.Embed:
        verb = {"ban": "banned from", "softban": "removed from", "kick": "removed from"}[action]
        lines = [
            f"You were **{verb} {guild.name}** because a message was posted in "
            f"#{getattr(channel, 'name', 'the honeypot channel')}, a channel that exists only as a trap for "
            "hacked accounts and spam bots. Nobody looked at your account - this is fully automatic.",
            "",
            "**If your account was hacked**, please secure it now:",
            "- Change your Discord password",
            "- Turn on two-factor authentication (2FA)",
            "- Under Settings > Authorized Apps, revoke anything you don't recognise",
        ]
        if action == "softban":
            lines += ["", "Your recent messages were cleaned up. Once your account is secure you may be able to "
                          "rejoin; if you cannot, please contact the staff."]
        elif action == "kick":
            lines += ["", "Once your account is secure you are welcome to rejoin."]
        if invite_url and action in ("kick", "softban"):
            lines += [f"Rejoin link (after securing your account): {invite_url}"]
        if appeal_url:
            lines += ["", f"If this was a mistake, you can appeal here: {appeal_url}"]
        return discord.Embed(
            title=f"You were removed from {guild.name}",
            description="\n".join(lines),
            color=discord.Color.red(),
        )

    def _resolve_log_channel(self, guild: discord.Guild, cfg: dict):
        """log_channel_id if set, else the moderation-log channel (same config attr
        services/log_router.py's 'moderation' category uses, read live)."""
        for cid in (cfg.get("log_channel_id"), getattr(_config, "LOG_CHANNEL_ID", 0)):
            if cid:
                ch = guild.get_channel(cid)
                if isinstance(ch, discord.TextChannel):
                    return ch
        return None

    async def _post_catch_log(
        self, guild, cfg, author, action, result, errors, content, flags,
        seconds_since_join, created_at, dm_sent, channel, via_app=None,
    ) -> str | None:
        log_channel = self._resolve_log_channel(guild, cfg)
        if log_channel is None:
            return "log_post_skipped: no log channel configured"
        try:
            embed = build_embed(
                "honeypot_catch", member=author, actor=self._bot.user,
                reason=f"Posted in #{getattr(channel, 'name', 'honeypot')}",
            )
            embed.title = "Honeypot catch"
            embed.color = discord.Color.red() if result == "failed" else discord.Color.orange()
            still_banned = any(e.startswith("softban unban failed") for e in errors)
            if still_banned:
                embed.title = "Honeypot catch - USER STILL BANNED"
                embed.color = discord.Color.red()
                embed.description = (
                    (embed.description + "\n\n" if embed.description else "")
                    + "**Softban's unban step failed - this user is STILL BANNED. Unban manually.**"
                )
            age = "unknown"
            if created_at is not None:
                age = f"{max(0, (_now() - created_at).days)}d old"
            embed.add_field(name="Action", value=f"{action} ({result})", inline=True)
            embed.add_field(name="Account", value=age, inline=True)
            embed.add_field(name="Joined", value=f"{_fmt_seconds(seconds_since_join)} before", inline=True)
            embed.add_field(name="DM delivered", value="yes" if dm_sent else "no", inline=True)
            active = [k for k, v in flags.items() if v]
            embed.add_field(name="Signals", value=", ".join(active) or "none", inline=True)
            if via_app:
                embed.add_field(name="Posted via app", value=discord.utils.escape_markdown(describe_app(via_app)), inline=False)
            shown = content.replace("`", "'")[:900] or "(no text)"
            embed.add_field(name="Message", value=f"```{shown}```", inline=False)
            if errors:
                embed.add_field(name="Errors", value="\n".join(errors)[:1000], inline=False)
            await log_channel.send(embed=embed)
            return None
        except Exception as exc:
            log.exception("honeypot: posting catch log failed")
            return f"log_post_failed: {exc}"

    # ------------------------------------------------------------------
    # Channel deletion
    # ------------------------------------------------------------------
    async def handle_channel_deleted(self, channel) -> None:
        """Never raises. If someone deletes the trap channel/category by hand, drop the stale message ids
        (config then reports channel_exists:false) and tell the log channel."""
        try:
            guild = getattr(channel, "guild", None)
            if guild is None:
                return
            row = await self._repo.get_config(guild.id)
            if row is None:
                return
            if channel.id == row.get("channel_id"):
                await self._repo.upsert_config(guild.id, warning_message_id=None, stats_message_id=None)
                self._invalidate(guild.id)
                log.warning("honeypot: trap channel %s was deleted in guild %s; run setup again", channel.id, guild.id)
                cfg = await self.get_config(guild.id)
                log_channel = self._resolve_log_channel(guild, cfg)
                if log_channel is not None:
                    try:
                        await log_channel.send(embed=discord.Embed(
                            title="Honeypot channel deleted",
                            description="The honeypot channel was deleted, so it is inactive. "
                                        "Run `/honeypot setup` (or use the dashboard) to recreate it.",
                            color=discord.Color.orange(),
                        ))
                    except Exception:
                        log.warning("honeypot: could not announce channel deletion")
            elif channel.id == row.get("category_id"):
                log.warning("honeypot: category %s was deleted in guild %s", channel.id, guild.id)
        except Exception:
            log.exception("honeypot: error handling channel deletion")

    # ------------------------------------------------------------------
    # Name scrambling (migration 029)
    # ------------------------------------------------------------------
    async def scramble_names(
        self,
        guild: discord.Guild,
        *,
        actor_id: int | None = None,
        trigger: str = "manual",
        timeout: float | None = None,
    ) -> dict:
        """Renames the trap channel and its category to random gibberish (decoration kept).
        The category is only renamed when the trap is the only channel in it. `trigger`
        is "manual" (refused within SCRAMBLE_MANUAL_COOLDOWN of the last scramble) or
        "schedule". `timeout` bounds the whole call (Discord's rename rate limit can make
        a rename wait for minutes). Returns {config, channel: {old, new}, category:
        {old, new}|None, warnings}; raises HoneypotError if the channel was not renamed."""
        async with self._scramble_lock:
            row = await self._repo.get_config(guild.id)
            if row is None or not row.get("channel_id"):
                raise HoneypotError("not_configured", "The honeypot has not been set up yet.")
            channel, category = self._trap_objects(guild, row)
            if channel is None:
                raise HoneypotError("channel_missing", status=409)
            if trigger == "manual":
                last = parse_ts(row.get("last_scrambled_at"))
                if last is not None and (_now() - last).total_seconds() < SCRAMBLE_MANUAL_COOLDOWN:
                    raise HoneypotError("too_soon", status=409)

            rng = secrets.SystemRandom()
            deadline = time.monotonic() + timeout if timeout else None
            reason = "Honeypot: scheduled name scramble" if trigger == "schedule" else "Honeypot: name scramble"
            warnings: list[str] = []

            old_channel = channel.name
            new_channel = scrambled_name(old_channel, "channel", rng)
            renamed, err = await self._rename(channel, new_channel, reason, deadline)
            if err is not None:
                raise err
            # Discord may normalise the name (e.g. lowercase); report what it stored when it says.
            result: dict = {"channel": {"old": old_channel, "new": _name_of(renamed) or new_channel}, "category": None}

            if category is None:
                warnings.append("The honeypot channel is not in a category, so only the channel was renamed.")
            elif any(c.id != channel.id for c in category.channels):
                warnings.append(
                    f"The category {category.name!r} also holds other channels, so it was not renamed."
                )
            else:
                old_category = category.name
                new_category = scrambled_name(old_category, "category", rng)
                renamed_cat, cat_err = await self._rename(category, new_category, reason, deadline)
                if cat_err is not None:
                    warnings.append(f"The channel was renamed but the category was not: {scramble_error_text(cat_err)}")
                else:
                    result["category"] = {"old": old_category, "new": _name_of(renamed_cat) or new_category}

            await self._repo.upsert_config(guild.id, last_scrambled_at=_now().isoformat())
            self._invalidate(guild.id)
            self._scramble_retry_at.pop(guild.id, None)

        bot_id = getattr(getattr(self._bot, "user", None), "id", 0) or 0
        log.info("honeypot: names scrambled (%s) in guild %s: %s", trigger, guild.id, result)
        if self._audit is not None:
            try:
                await self._audit.log(
                    guild.id, actor_id or bot_id, "honeypot_scramble", target_type="channel", target_id=channel.id,
                    detail={"trigger": trigger, **result, "warnings": warnings},
                )
            except Exception:
                log.exception("honeypot: writing the scramble audit row failed")
        cfg_out = await self.get_config(guild.id)
        self._live("config_changed", guild_id=guild.id, config=cfg_out)
        return {"config": cfg_out, **result, "warnings": warnings}

    @staticmethod
    async def _rename(obj, name: str, reason: str, deadline: float | None):
        """Returns (edited object or None, None) or (None, HoneypotError). Never raises."""
        try:
            coro = obj.edit(name=name, reason=reason)
            if deadline is None:
                edited = await coro
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    coro.close()
                    raise asyncio.TimeoutError
                edited = await asyncio.wait_for(coro, remaining)
            return edited, None
        except asyncio.TimeoutError:
            return None, HoneypotError("rate_limited", status=409)
        except discord.Forbidden:
            return None, HoneypotError(
                "missing_permissions", "The bot needs the Manage Channels permission to rename the honeypot.",
                status=403,
            )
        except discord.HTTPException as exc:
            return None, HoneypotError("discord_error", f"Discord refused the rename: {exc}", status=502)

    async def scramble_if_due(self, guild: discord.Guild) -> bool:
        """Scheduler entry point (cogs/honeypot.py, every few minutes). Never raises.
        A failure is logged once and retried after SCRAMBLE_RETRY_AFTER_FAILURE."""
        try:
            if time.monotonic() < self._scramble_retry_at.get(guild.id, 0.0):
                return False
            row = await self._repo.get_config(guild.id)
            if not scramble_due(row):
                return False
            if self._trap_objects(guild, row)[0] is None:
                return False  # trap channel gone: nothing to rename until setup runs again
            await self.scramble_names(guild, trigger="schedule")
            return True
        except HoneypotError as exc:
            self._scramble_retry_at[guild.id] = time.monotonic() + SCRAMBLE_RETRY_AFTER_FAILURE
            log.warning("honeypot: scheduled name scramble failed in guild %s: %s (%s)", guild.id, exc.code, exc.detail)
        except Exception:
            self._scramble_retry_at[guild.id] = time.monotonic() + SCRAMBLE_RETRY_AFTER_FAILURE
            log.exception("honeypot: scheduled name scramble failed in guild %s", guild.id)
        return False

    # ------------------------------------------------------------------
    # Read APIs
    # ------------------------------------------------------------------
    async def get_catches(self, guild_id: int, limit: int = 50, before_id: int | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        rows = await self._repo.list_events(guild_id, limit, before_id)
        return [catch_to_dict(r) for r in rows]

    async def get_stats(self, guild_id: int) -> dict:
        events = await self._repo.get_all_events_for_stats(guild_id)
        counts = await self._repo.count_appeals_by_status(guild_id)
        return compute_stats(events, counts)

    async def list_appeals(self, guild_id: int, status: str = "all") -> list[dict]:
        if status not in ("all", "invited", "pending", "approved", "denied"):
            raise HoneypotError("invalid", "status must be one of pending, approved, denied, invited, all.")
        rows = await self._repo.list_appeals(guild_id, None if status == "all" else status)
        return [appeal_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Appeals
    # ------------------------------------------------------------------
    async def hash_legacy_tokens(self) -> int:
        """Startup (cogs/honeypot.py setup): rows from before migration 030 still hold the
        raw link token; replace it with its hash. Idempotent."""
        n = await self._repo.hash_legacy_tokens()
        if n:
            log.info("honeypot: hashed %d stored appeal token(s)", n)
        return n

    async def lookup_appeal(self, guild_id: int, token: str) -> dict:
        """The DM link proves who they are, so it may show their own appeal's outcome."""
        appeal = await self._repo.get_appeal_by_token(guild_id, token) if token else None
        if appeal is None:
            raise HoneypotError("invalid_token", status=404)
        return {
            "user_id": appeal["user_id"], "username": appeal["username"], "status": appeal["status"],
            **await self._outcome_fields(guild_id, appeal),
        }

    async def _outcome_fields(self, guild_id: int, appeal: dict) -> dict:
        """What the PERSON may see about their own appeal (never the reviewer or the staff note)."""
        approved = appeal["status"] == "approved"
        cfg = await self._repo.get_config(guild_id) if approved else None
        return {
            "submitted_at": appeal.get("submitted_at"),
            "reviewed_at": appeal.get("reviewed_at"),
            "rejoin_invite_url": (cfg or {}).get("rejoin_invite_url") if approved else None,
            "rejoined": bool(appeal.get("rejoined_at")),
        }

    @staticmethod
    def _covers(appeal: dict | None, ban_event: dict) -> bool:
        """A submitted appeal covers the catch it was made for; a newer catch allows a new one."""
        return (
            appeal is not None
            and appeal["status"] != "invited"
            and (appeal["catch_id"] is None or appeal["catch_id"] >= ban_event["id"])
        )

    async def appeal_status_for_user(self, guild_id: int, user_id: int) -> dict:
        """For someone whose identity Discord login has PROVEN (dashboard appeal sign-in):
        their own latest appeal and whether they can file one now. Only ever called with
        the signed-in user's id, so it is not an oracle for who was banned."""
        ban_event = await self._repo.get_latest_ban_event_for_user(guild_id, user_id)
        appeal = await self._repo.get_latest_appeal_for_user(guild_id, user_id)
        eligible = (
            ban_event is not None and ban_event["action_result"] != "failed" and not self._covers(appeal, ban_event)
        )
        shown = appeal if appeal is not None and appeal["status"] != "invited" else None
        return {
            "user_id": user_id,
            "eligible": eligible,
            "appeal": (
                {"status": shown["status"], **await self._outcome_fields(guild_id, shown)} if shown else None
            ),
        }

    async def submit_verified_appeal(
        self, guild_id: int, user_id: int, username: str | None, name: str, message: str,
    ) -> dict:
        """Appeal filed after Discord login: identity is proven, so errors can be honest
        (404 not_eligible, 409 already_submitted). Uses their 'invited' DM row if it is
        for the latest catch, else creates a verified row."""
        name = (name or "").strip()
        message = (message or "").strip()
        if not 1 <= len(name) <= 100 or len(message) > 1500:
            raise HoneypotError("invalid", "name must be 1-100 characters and message at most 1500.")
        async with self._appeal_lock:
            ban_event = await self._repo.get_latest_ban_event_for_user(guild_id, user_id)
            if ban_event is None or ban_event["action_result"] == "failed":
                raise HoneypotError("not_eligible", status=404)
            existing = await self._repo.get_latest_appeal_for_user(guild_id, user_id)
            if self._covers(existing, ban_event):
                raise HoneypotError("already_submitted", status=409)
            if (existing is not None and existing["status"] == "invited"
                    and (existing["catch_id"] is None or existing["catch_id"] >= ban_event["id"])):
                if not await self._repo.submit_appeal(existing["id"], name, message, verified_via="discord_login"):
                    raise HoneypotError("already_submitted", status=409)
                appeal_id = existing["id"]
            else:
                appeal_id = await self._repo.create_appeal(
                    guild_id, user_id, secrets.token_urlsafe(24), username=username or ban_event.get("username"),
                    status="pending", catch_id=ban_event["id"], name=name, message=message,
                    submitted_at=_now().isoformat(), verified=True, verified_via="discord_login",
                    is_test=bool(ban_event.get("is_test")),  # an appeal of a Debug-page test catch is a test too
                )
        await self._live_appeal("appeal_submitted", guild_id, appeal_id)
        return {"ok": True, "status": "pending"}

    async def rejoin(self, guild: discord.Guild, user_id: int, access_token: str) -> dict:
        """Adds an approved appellant back to the server with their own OAuth access token
        (scope guilds.join), via PUT /guilds/{id}/members/{user}. The token is used once
        and never stored; the dashboard revokes it right after this call. Needs the bot's
        Create Invite permission. -> {"result": "joined" | "already_member"}"""
        appeal = await self._repo.get_latest_appeal_for_user(guild.id, user_id)
        if appeal is None or appeal["status"] != "approved":
            raise HoneypotError("not_approved", status=409)
        try:  # never re-add someone who is banned again (or whose unban failed)
            await guild.fetch_ban(discord.Object(id=user_id))
            raise HoneypotError("still_banned", status=409)
        except discord.NotFound:
            pass
        except discord.Forbidden as exc:
            raise HoneypotError("missing_permissions", "The bot needs Ban Members to check the ban.", status=403) from exc
        except discord.HTTPException as exc:
            raise HoneypotError("discord_error", f"Discord error while checking the ban: {exc}", status=502) from exc

        route = Route("PUT", "/guilds/{guild_id}/members/{user_id}", guild_id=guild.id, user_id=user_id)
        try:
            data = await self._bot.http.request(route, json={"access_token": access_token})
        except discord.HTTPException as exc:
            # Decide on Discord's JSON error code first, whatever the HTTP status was.
            code = getattr(exc, "code", 0)
            mapped = {40007: "still_banned", 30001: "too_many_guilds", 50025: "login_expired"}.get(code)
            if mapped:
                raise HoneypotError(mapped, status=409) from exc
            if isinstance(exc, discord.Forbidden):
                # The person only sees "couldn't add you"; staff need this in the bot log.
                log.warning("honeypot: rejoin of %s refused by Discord (code %s): the bot needs Create Invite", user_id, code)
                raise HoneypotError(
                    "missing_permissions", "The bot needs the Create Invite permission to add members.", status=403,
                ) from exc
            raise HoneypotError("discord_error", f"Discord error while adding the member: {exc}", status=502) from exc

        joined = bool(data)  # 201 + member object; 204 (empty) = already a member
        if joined:
            await self._repo.mark_rejoined(appeal["id"])
            try:
                user = self._bot.get_user(user_id) or await self._bot.fetch_user(user_id)
                await user.send(embed=discord.Embed(
                    title=f"Welcome back to {guild.name}",
                    description="You were added back to the server after your appeal. Please make sure your "
                                "account is secured (new password, 2FA on, unknown apps removed).",
                    color=discord.Color.green(),
                ))
            except Exception:
                pass  # best-effort
        if self._audit is not None:
            bot_id = getattr(getattr(self._bot, "user", None), "id", 0) or 0
            try:
                await self._audit.log(
                    guild.id, bot_id, "honeypot_appeal_rejoin", target_type="user", target_id=user_id,
                    detail={"appeal_id": appeal["id"], "result": "joined" if joined else "already_member"},
                )
            except Exception:
                log.exception("honeypot: writing the rejoin audit row failed")
        if joined:
            await self._live_appeal("appeal_rejoined", guild.id, appeal["id"])
        return {"result": "joined" if joined else "already_member"}

    # ------------------------------------------------------------------
    # Appeal-flow test cases (owner-only Debug page, migration 031)
    # ------------------------------------------------------------------
    TEST_CASE_STATES = ("eligible", "pending", "approved")

    async def create_test_case(self, guild: discord.Guild, user_id: int, state: str, owner_id: int) -> dict:
        """Records a TEST honeypot ban for `user_id` (nobody is banned, nothing is sent) so the
        real appeal sign-in / review / rejoin can be tried with an alt account:
          eligible -> test catch only (they can sign in and file an appeal)
          pending  -> + a pending test appeal (practise approving it in the Appeals tab)
          approved -> + an approved test appeal (go straight to "Rejoin server")
        Rows are is_test = 1: out of stats/feed/counts, labelled Test, removed by delete_test_cases.
        Refused for anyone with real honeypot records, the bot and the owner themself."""
        if state not in self.TEST_CASE_STATES:
            raise HoneypotError("invalid", f"state must be one of {', '.join(self.TEST_CASE_STATES)}.")
        bot_id = getattr(getattr(self._bot, "user", None), "id", None)
        if user_id in (bot_id, owner_id):
            raise HoneypotError("invalid", "Use another account (an alt), not the bot or yourself.")
        if await self._repo.has_real_records(guild.id, user_id):
            raise HoneypotError("invalid", "That account has real honeypot records; test cases are only for other accounts.")
        username = None
        try:
            user = self._bot.get_user(user_id) or await self._bot.fetch_user(user_id)
            username = _name_of(user)
        except discord.NotFound as exc:
            raise HoneypotError("invalid", "No Discord account has that ID.") from exc
        except discord.HTTPException:
            pass  # name is cosmetic
        async with self._appeal_lock:
            row = await self._repo.get_config(guild.id) or {}
            catch_id = await self._repo.add_event(
                guild.id, user_id, "ban", username=username, channel_id=row.get("channel_id"),
                content="[TEST] appeal-flow test case from the Debug page", is_test=True,
            )
            appeal_id = None
            if state != "eligible":
                now = _now().isoformat()
                appeal_id = await self._repo.create_appeal(
                    guild.id, user_id, secrets.token_urlsafe(24), username=username,
                    status="pending", catch_id=catch_id, name="Test appeal",
                    message="Created on the Debug page to test the appeal flow.", submitted_at=now,
                    verified=True, verified_via="discord_login", is_test=True,
                )
                if state == "approved":
                    await self._repo.review_appeal(appeal_id, "approved", owner_id, "Test case (Debug page)")
        await self._audit_test("honeypot_test_case_created", guild.id, owner_id, user_id,
                               {"state": state, "catch_id": catch_id, "appeal_id": appeal_id})
        if appeal_id is not None:
            await self._live_appeal("appeal_submitted", guild.id, appeal_id)
        return {"catch_id": catch_id, "appeal_id": appeal_id, "state": state, "username": username}

    async def list_test_cases(self, guild_id: int) -> list[dict]:
        return await self._repo.list_test_cases(guild_id)

    async def delete_test_cases(self, guild_id: int, owner_id: int) -> dict:
        """Removes every test catch/appeal. Never touches real records or Discord (a test
        account that rejoined stays in the server)."""
        async with self._appeal_lock:
            counts = await self._repo.delete_test_cases(guild_id)
        await self._audit_test("honeypot_test_cases_deleted", guild_id, owner_id, None, counts)
        self._live("appeals_changed", guild_id=guild_id)  # open dashboards re-fetch their appeal lists
        return counts

    async def _audit_test(self, action: str, guild_id: int, actor_id: int, user_id: int | None, detail: dict) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.log(guild_id, actor_id, action, target_type="user" if user_id else None,
                                  target_id=user_id, detail=detail)
        except Exception:
            log.exception("honeypot: writing the %s audit row failed", action)

    async def _validated_invite_url(self, guild: discord.Guild, value) -> str | None:
        """None/"" clears it. Otherwise a discord.gg / discord.com/invite link to THIS server
        that never expires; stored as https://discord.gg/<code>."""
        if value in (None, ""):
            return None
        m = _INVITE_URL_RE.match(value.strip()) if isinstance(value, str) else None
        if m is None:
            raise HoneypotError("invalid", "rejoin_invite_url must be a discord.gg or discord.com/invite link.")
        code = m.group(1)
        try:
            invite = await self._bot.fetch_invite(code, with_counts=False, with_expiration=True)
        except discord.NotFound as exc:
            raise HoneypotError("invalid", "That invite does not exist or has expired.") from exc
        except discord.HTTPException as exc:
            raise HoneypotError("discord_error", f"Discord error while checking the invite: {exc}", status=502) from exc
        if getattr(getattr(invite, "guild", None), "id", None) != guild.id:
            raise HoneypotError("invalid", "That invite is for a different server.")
        if getattr(invite, "expires_at", None) is not None:
            raise HoneypotError("invalid", "That invite expires. Use a permanent invite (or let the bot create one).")
        return f"https://discord.gg/{code}"

    async def create_rejoin_invite(self, guild: discord.Guild, channel_id: int, actor_id: int) -> dict:
        """Creates a permanent, unlimited invite to `channel_id` and stores it as the rejoin link."""
        row = await self._repo.get_config(guild.id)
        if row is None or not row.get("channel_id"):
            raise HoneypotError("not_configured", "The honeypot has not been set up yet. Run setup first.")
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            raise HoneypotError("invalid", "channel_id must be a text channel in this server.")
        try:
            invite = await channel.create_invite(
                max_age=0, max_uses=0, unique=False, reason="Honeypot: rejoin link for approved appeals",
            )
        except discord.Forbidden as exc:
            raise HoneypotError(
                "missing_permissions", "The bot needs Create Invite in that channel.", status=403,
            ) from exc
        except discord.HTTPException as exc:
            raise HoneypotError("discord_error", f"Discord error while creating the invite: {exc}", status=502) from exc
        url = f"https://discord.gg/{invite.code}"
        await self._repo.upsert_config(guild.id, rejoin_invite_url=url)
        self._invalidate(guild.id)
        if self._audit is not None:
            try:
                await self._audit.log(guild.id, actor_id, "honeypot_rejoin_invite", target_type="channel",
                                      target_id=channel.id, detail={"url": url})
            except Exception:
                log.exception("honeypot: writing the invite audit row failed")
        cfg_out = await self.get_config(guild.id)
        self._live("config_changed", guild_id=guild.id, config=cfg_out)
        return cfg_out

    async def submit_appeal(
        self, guild_id: int, name: str, message: str, token: str | None = None, user_id: int | None = None,
    ) -> dict:
        # NOT exposed since 2026-10-29: appeals require Discord sign-in, so the only
        # submission path is submit_verified_appeal. The internal /appeals/submit route
        # and the public POST /api/public/appeals body handling were removed; don't
        # wire this back up to an API without the owner's say-so.
        name = (name or "").strip()
        message = (message or "").strip()
        if not 1 <= len(name) <= 100 or len(message) > 1500:
            raise HoneypotError("invalid", "name must be 1-100 characters and message at most 1500.")

        if token:
            appeal = await self._repo.get_appeal_by_token(guild_id, token)
            if appeal is None:
                raise HoneypotError("not_eligible", status=404)
            if appeal["status"] != "invited":
                raise HoneypotError("already_submitted", status=409)
            if not await self._repo.submit_appeal(appeal["id"], name, message):
                raise HoneypotError("already_submitted", status=409)
            await self._live_appeal("appeal_submitted", guild_id, appeal["id"])
            return {"ok": True, "status": "pending"}

        # No token: anyone can type any snowflake, so this path NEVER reveals state.
        # It always returns the same {"ok": True} whether or not a row was created;
        # a row (verified=0, shown to staff as "Unverified") is created only when a
        # honeypot ban is on record and no submitted appeal already covers it.
        async with self._appeal_lock:
            await self._submit_unverified_appeal(guild_id, user_id, name, message)
        return {"ok": True}

    async def _submit_unverified_appeal(self, guild_id: int, user_id: int | None, name: str, message: str) -> None:
        if user_id is None:
            return
        ban_event = await self._repo.get_latest_ban_event_for_user(guild_id, user_id)
        if ban_event is None or ban_event["action_result"] == "failed":
            return
        existing = await self._repo.get_latest_appeal_for_user(guild_id, user_id)
        # An appeal covers the catch it was created for; a newer catch allows a fresh appeal.
        # An untouched 'invited' row does NOT block: it belongs to the DM link, and we must not
        # consume it (that would let a stranger burn the real user's token) - a separate
        # unverified row is created instead.
        covers = (
            existing is not None
            and existing["status"] != "invited"
            and (existing["catch_id"] is None or existing["catch_id"] >= ban_event["id"])
        )
        if covers:
            return
        new_id = await self._repo.create_appeal(
            guild_id, user_id, secrets.token_urlsafe(24), username=ban_event.get("username"),
            status="pending", catch_id=ban_event["id"], name=name, message=message,
            submitted_at=_now().isoformat(), verified=False, is_test=bool(ban_event.get("is_test")),
        )
        await self._live_appeal("appeal_submitted", guild_id, new_id)

    async def _get_appeal_for(self, guild_id: int, appeal_id: int) -> dict:
        appeal = await self._repo.get_appeal(appeal_id)
        if appeal is None or appeal["guild_id"] != guild_id:
            raise HoneypotError("not_found", "Appeal not found.", status=404)
        return appeal

    async def approve_appeal(
        self, guild: discord.Guild, appeal_id: int, reviewer_id: int, note: str | None = None,
    ) -> dict:
        appeal = await self._get_appeal_for(guild.id, appeal_id)
        if appeal["status"] != "pending":
            raise HoneypotError("not_pending", "Only pending appeals can be approved.", status=409)

        reason = f"Honeypot appeal approved by {reviewer_id}" + (f": {note}" if note else "")
        unbanned = True
        # Only ever lift a HONEYPOT ban: if the user has since been banned for another reason
        # (manual ban, raid, ...) approving an honeypot appeal must not undo that.
        currently_banned = True
        try:
            ban_entry = await guild.fetch_ban(discord.Object(id=appeal["user_id"]))
        except discord.NotFound:
            currently_banned = False
        except discord.Forbidden as exc:
            raise HoneypotError(
                "missing_permissions", "The bot is missing the Ban Members permission needed to check the ban.",
                status=502,
            ) from exc
        except discord.HTTPException as exc:
            raise HoneypotError("discord_error", f"Discord error while checking the ban: {exc}", status=502) from exc
        else:
            if not (getattr(ban_entry, "reason", None) or "").startswith("Honeypot"):
                raise HoneypotError(
                    "not_a_honeypot_ban",
                    "This user is currently banned for a different reason (not the honeypot), so the appeal "
                    "was not approved and nobody was unbanned. Review the ban manually.",
                    status=409,
                )
        try:
            if currently_banned:
                await self._moderation.unban(guild, appeal["user_id"], discord.Object(id=reviewer_id), reason)
            else:
                unbanned = False  # not banned any more: still counts as approved
        except discord.NotFound:
            unbanned = False  # lifted between the check and the unban: still counts as approved
        except discord.Forbidden as exc:
            raise HoneypotError(
                "missing_permissions", "The bot is missing the Ban Members permission needed to unban.", status=502,
            ) from exc
        except discord.HTTPException as exc:
            raise HoneypotError("discord_error", f"Discord error while unbanning: {exc}", status=502) from exc

        if not await self._repo.review_appeal(appeal_id, "approved", reviewer_id, note):
            raise HoneypotError("not_pending", "Only pending appeals can be approved.", status=409)

        cfg = await self._repo.get_config(guild.id) or {}
        invite_url = cfg.get("rejoin_invite_url")
        text = ("A moderator reviewed your appeal and approved it, so you can rejoin the server. "
                "Please make sure your account is secured (new password, 2FA on) first.")
        if invite_url:
            text += f"\n\nRejoin here: {invite_url}"
        text += f"\n\nYou can also open your appeal page ({_config.APPEAL_BASE_URL}/appeal) to rejoin with one click."
        dm_sent = await self._outcome_dm(
            appeal, discord.Embed(title=f"Your appeal was approved - {guild.name}", description=text,
                                  color=discord.Color.green()),
        )
        await self._live_appeal("appeal_approved", guild.id, appeal_id)
        return {"ok": True, "unbanned": unbanned, "dm_sent": dm_sent}

    async def deny_appeal(self, guild_id: int, appeal_id: int, reviewer_id: int, note: str | None = None) -> dict:
        appeal = await self._get_appeal_for(guild_id, appeal_id)
        if appeal["status"] != "pending" or not await self._repo.review_appeal(appeal_id, "denied", reviewer_id, note):
            raise HoneypotError("not_pending", "Only pending appeals can be denied.", status=409)
        guild = self._bot.get_guild(guild_id)
        dm_sent = await self._outcome_dm(appeal, discord.Embed(
            title=f"Your appeal was reviewed - {getattr(guild, 'name', 'the server')}",
            description="A moderator reviewed your appeal and did not approve it, so the ban stays in place.",
            color=discord.Color.dark_grey(),
        ))
        await self._live_appeal("appeal_denied", guild_id, appeal_id)
        return {"ok": True, "dm_sent": dm_sent}

    async def _outcome_dm(self, appeal: dict, embed: discord.Embed) -> bool:
        """Best-effort DM of an appeal outcome; the result is stored on the appeal.
        After a ban it usually fails: Discord only lets a bot DM people it shares a
        server with. The appeal page shows the outcome either way."""
        sent = False
        try:
            user = self._bot.get_user(appeal["user_id"]) or await self._bot.fetch_user(appeal["user_id"])
            await user.send(embed=embed)
            sent = True
        except Exception:
            pass
        try:
            await self._repo.set_outcome_dm(appeal["id"], sent)
        except Exception:
            log.exception("honeypot: recording the outcome DM result failed")
        return sent
