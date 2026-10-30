"""
SettingsService: the dashboard Settings page's source of truth. Reads and
writes the `guild_settings` key/value table (database/repositories/
settings_repo.py, migration 024) through an explicit, typed, validated field
allowlist (SETTINGS_SCHEMA below).

SECURITY BOUNDARY: SETTINGS_SCHEMA is the ONLY set of keys this service will
ever read, write, return or apply. It excludes every credential in config.py
(DISCORD_TOKEN, INTERNAL_API_SECRET, DISCORD_CLIENT_SECRET, ...). Unknown keys
in an update are REJECTED (422 field error), never silently written.

How a saved value takes effect (per field, reported honestly in the API):
  * "live" fields (ticket_chat_logging_enabled, ticket_archive_purge_days,
    every antispam_* field): cogs/tickets.py, internal_api/routes/tickets.py
    and cogs/anti_spam.py read them through get_effective() with the short
    TTL cache below, so a save applies within seconds.
  * every other editable field: cogs (cogs/kick.py, cogs/verify.py,
    cogs/moderation.py, cogs/music.py, services/ticket_service.py) read
    `config.X` (most copy it into a module constant at import). main.py calls
    apply_to_config() after migrations and BEFORE any cog is loaded, which
    overwrites those config attributes with the validated saved values, so a
    save applies on the next bot restart. The running process keeps the
    startup value until then; the API reports both (`saved` vs `running`).
  * owner_id: read-only. It is shown (config.OWNER_ID) but can never be
    written through the dashboard and is never applied from the DB.

Validation (this service is the authority; server/routes/settings.py mirrors
the type/range part so obvious mistakes fail fast):
  * bool: only true/false, "true"/"false"/"1"/"0" (also 1/0). Stored rows are
    parsed with the same strict rule; anything else falls back to the default
    and is flagged `invalid_stored` in the GET response.
  * int: integer within [min, max].
  * snowflake: digits; must exist in the guild and be the right kind (role /
    category / text channel). Roles the bot assigns (verified/unverified) must
    be below the bot's top role, not managed, not @everyone, and the bot must
    hold Manage Roles. Log channels must be text channels the bot can post
    embeds in.
  * template: the name must exist in message_templates for the guild.
  * pattern (music_default_search): ^(yt|sc)search<1-99>$.
  * null on write = reset to the .env default (the row is deleted).
Startup apply validates type/range only (the gateway is not connected yet);
existence problems surface as `warning` in the GET response.

Typed storage: guild_settings.value is TEXT; _serialize/_parse_stored are the
only converters.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import discord

from database.repositories.settings_repo import SettingsRepository

import config as _config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettingField:
    key: str                  # guild_settings.key / API field name
    config_attr: str          # attribute on the config module
    type: str                 # "bool" | "int" | "str" | "snowflake"
    group: str                # "verification" | "tickets" | "logging" | "music" | "antispam" | "permissions"
    label: str
    description: str
    live: bool = False        # True = applies within seconds; False = on next bot restart
    kind: str | None = None   # snowflake: "role" | "category" | "text_channel" | "user"; str: "template"
    min: int | None = None
    max: int | None = None
    pattern: str | None = None
    read_only: bool = False
    bot_assigns: bool = False  # role the bot adds/removes itself -> must be manageable by the bot


SETTINGS_SCHEMA: dict[str, SettingField] = {
    f.key: f
    for f in (
        SettingField(
            "unverified_role_id", "UNVERIFIED_ROLE_ID", "snowflake", "verification",
            "Unverified role", "Role assigned to members before they verify.",
            kind="role", bot_assigns=True,
        ),
        SettingField(
            "verified_role_id", "VERIFIED_ROLE_ID", "snowflake", "verification",
            "Verified role", "Role assigned to members once they complete verification.",
            kind="role", bot_assigns=True,
        ),
        SettingField(
            "unverified_category_id", "UNVERIFIED_CATEGORY_ID", "snowflake", "verification",
            "Unverified category", "Channel category where !verify is accepted.",
            kind="category",
        ),
        SettingField(
            "check_interval_minutes", "CHECK_INTERVAL_MINUTES", "int", "verification",
            "Check interval (minutes)", "How often the unverified-member sweep runs (1 to 1440).",
            min=1, max=1440,
        ),
        SettingField(
            "auto_kick", "AUTO_KICK", "bool", "verification",
            "Auto-kick unverified members", "Automatically kick members who never verify.",
        ),
        SettingField(
            "verify_template_name", "VERIFY_TEMPLATE_NAME", "str", "verification",
            "Verify template", "Message template sent when a member is verified.",
            kind="template", min=1, max=100,
        ),
        SettingField(
            "verify_ticket_welcome_template_name", "VERIFY_TICKET_WELCOME_TEMPLATE_NAME", "str", "verification",
            "Verify-ticket welcome template", "Message template posted when a verification ticket opens.",
            kind="template", min=1, max=100,
        ),
        SettingField(
            "log_channel_id", "LOG_CHANNEL_ID", "snowflake", "logging",
            "Moderation log channel", "Text channel timeouts, warns and other mod actions are logged to.",
            kind="text_channel",
        ),
        SettingField(
            "join_leave_channel_id", "JOIN_LEAVE_CHANNEL_ID", "snowflake", "logging",
            "Join/leave log channel", "Text channel member joins, leaves, kicks and bans are logged to.",
            kind="text_channel",
        ),
        SettingField(
            "ticket_chat_logging_enabled", "TICKET_CHAT_LOGGING_ENABLED", "bool", "tickets",
            "Enable ticket chat logging",
            "Capture plain-text message content in open/archived ticket channels. "
            "Attachments and voice messages are never captured regardless of this setting.",
            live=True,
        ),
        SettingField(
            "ticket_archive_purge_days", "TICKET_ARCHIVE_PURGE_DAYS", "int", "tickets",
            "Archived ticket purge (days)",
            "Archived tickets older than this many days are permanently deleted by the daily purge job (1 to 3650).",
            live=True, min=1, max=3650,
        ),
        SettingField(
            "owner_id", "OWNER_ID", "snowflake", "permissions",
            "Owner ID", "Discord user treated as the bot owner. Set OWNER_ID in .env; view-only here.",
            kind="user", read_only=True,
        ),
        SettingField(
            "music_default_search", "MUSIC_DEFAULT_SEARCH", "str", "music",
            "Default search provider",
            "yt-dlp search prefix for non-URL queries: ytsearchN (YouTube) or scsearchN (SoundCloud), N = results (1 to 99).",
            pattern=r"^(yt|sc)search[1-9][0-9]?$",
        ),
        SettingField(
            "music_allow_playlists", "MUSIC_ALLOW_PLAYLISTS", "bool", "music",
            "Allow playlists", "Allow queuing an entire playlist from one link.",
        ),
        SettingField(
            "music_default_volume", "MUSIC_DEFAULT_VOLUME", "int", "music",
            "Default volume", "Starting playback volume (0 to 100).",
            min=0, max=100,
        ),
        SettingField(
            "music_inactivity_timeout", "MUSIC_INACTIVITY_TIMEOUT", "int", "music",
            "Inactivity timeout (seconds)",
            "How long the bot waits in an idle voice channel before disconnecting (30 to 86400).",
            min=30, max=86400,
        ),
        # Anti-spam (cogs/anti_spam.py reads all of these live, cached as one
        # SpamRules under the "antispam_rules" key). A count of 0 = rule off.
        SettingField(
            "antispam_enabled", "ANTISPAM_ENABLED", "bool", "antispam",
            "Enable anti-spam",
            "Automatically delete spam and time out the sender. Staff and members with the "
            "moderate_members or manage_messages_bulk permission are never checked.",
            live=True,
        ),
        SettingField(
            "antispam_flood_messages", "ANTISPAM_FLOOD_MESSAGES", "int", "antispam",
            "Flood: messages",
            "This many messages from one member within the flood window is spam (0 turns the rule off, otherwise 3 to 50).",
            live=True, min=0, max=50,
        ),
        SettingField(
            "antispam_flood_seconds", "ANTISPAM_FLOOD_SECONDS", "int", "antispam",
            "Flood: window (seconds)", "Time window for the flood rule (2 to 60).",
            live=True, min=2, max=60,
        ),
        SettingField(
            "antispam_duplicate_messages", "ANTISPAM_DUPLICATE_MESSAGES", "int", "antispam",
            "Duplicates: copies",
            "The same text this many times within the duplicate window, in any channels, is spam. "
            "Prefix commands and image-only messages don't count (0 turns the rule off, otherwise 2 to 50).",
            live=True, min=0, max=50,
        ),
        SettingField(
            "antispam_duplicate_seconds", "ANTISPAM_DUPLICATE_SECONDS", "int", "antispam",
            "Duplicates: window (seconds)", "Time window for the duplicate rule (5 to 600).",
            live=True, min=5, max=600,
        ),
        SettingField(
            "antispam_mention_limit", "ANTISPAM_MENTION_LIMIT", "int", "antispam",
            "Mentions per message",
            "One message mentioning this many different users/roles is spam (0 turns the rule off, otherwise 2 to 50).",
            live=True, min=0, max=50,
        ),
        SettingField(
            "antispam_timeout_minutes", "ANTISPAM_TIMEOUT_MINUTES", "int", "antispam",
            "Timeout (minutes)",
            "How long a spammer is timed out (0 = only delete the spam, up to 40320 = 28 days).",
            live=True, min=0, max=40320,
        ),
    )
}

# STAFF_ROLE_IDS is intentionally NOT editable here: it is the fixed trust
# anchor behind require_staff (see server/permissions/guards.py), so making it
# editable from a staff-only page would let staff redefine staff.

# Modules known to copy a settings value into a module constant at import.
# They must not be imported before apply_to_config() runs (main.py loads cogs
# afterwards); apply_to_config() warns if one already is.
IMPORT_TIME_COPIES: dict[str, tuple[str, ...]] = {
    "cogs.kick": ("UNVERIFIED_ROLE_ID", "VERIFIED_ROLE_ID", "LOG_CHANNEL_ID", "JOIN_LEAVE_CHANNEL_ID",
                  "CHECK_INTERVAL_MINUTES", "AUTO_KICK"),
    "cogs.verify": ("UNVERIFIED_ROLE_ID", "VERIFIED_ROLE_ID", "VERIFY_TEMPLATE_NAME", "UNVERIFIED_CATEGORY_ID"),
    "cogs.moderation": ("LOG_CHANNEL_ID", "JOIN_LEAVE_CHANNEL_ID"),
    "services.ticket_service": ("VERIFY_TICKET_WELCOME_TEMPLATE_NAME",),
}

_TRUE = {"true", "1"}
_FALSE = {"false", "0"}
_SNOWFLAKE_RE = re.compile(r"^[0-9]{15,21}$")
_TEMPLATE_RE = re.compile(r"^[^\x00-\x1f]+$")

# .env/config values captured before apply_to_config() overrides them, so
# "reset to default" and `source` keep meaning the env value afterwards.
_env_defaults: dict[str, Any] = {}
# What apply_to_config() changed at startup: {key: (env_value, applied_value)}.
_startup_overrides: dict[str, tuple[Any, Any]] = {}
# When apply_to_config() last ran (epoch seconds, i.e. ~ this bot process's
# start). Reported as `applied_at` so the Settings page can tell that a
# restart it requested has happened. None = never applied in this process.
_applied_at: float | None = None


class SettingsValidationError(Exception):
    """Raised by SettingsService.update(); `fields` is {key: message}."""

    def __init__(self, fields: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in fields.items()))
        self.fields = fields


class GuildUnavailableError(Exception):
    """A Discord-backed check was needed but the guild is not cached yet."""


# ---------------------------------------------------------------------------
# Parsing helpers (pure)
# ---------------------------------------------------------------------------

def parse_bool(value: Any) -> bool:
    """Strict bool: True/False, 1/0, "true"/"false"/"1"/"0" (case/space-insensitive)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    raise ValueError("Must be true or false.")


def _parse_int(field: SettingField, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Must be a whole number.")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and re.fullmatch(r"\s*-?[0-9]+\s*", value):
        n = int(value)
    else:
        raise ValueError("Must be a whole number.")
    if field.min is not None and n < field.min:
        raise ValueError(f"Must be at least {field.min}.")
    if field.max is not None and n > field.max:
        raise ValueError(f"Must be at most {field.max}.")
    return n


def _parse_snowflake(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Must be a Discord ID.")
    s = str(value).strip() if isinstance(value, (int, str)) else ""
    if not _SNOWFLAKE_RE.match(s):
        raise ValueError("Must be a Discord ID (15 to 21 digits).")
    return int(s)


def _parse_str(field: SettingField, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Must be text.")
    s = value.strip()
    if field.min is not None and len(s) < field.min:
        raise ValueError("Can't be empty." if field.min == 1 else f"Must be at least {field.min} characters.")
    if field.max is not None and len(s) > field.max:
        raise ValueError(f"Must be at most {field.max} characters.")
    if field.pattern and not re.fullmatch(field.pattern, s):
        raise ValueError("Must be ytsearchN or scsearchN, where N is 1 to 99 (e.g. ytsearch7).")
    if field.kind == "template" and not _TEMPLATE_RE.match(s):
        raise ValueError("Contains invalid characters.")
    return s


# Anti-spam counts where 0 means "rule off" but small non-zero values would
# flag normal chat (a flood threshold of 1 = every message is spam). Applied
# by parse_value, so stored rows are held to it too. Mirrored as `off_or_min`
# in server/routes/settings.py.
OFF_OR_MIN: dict[str, int] = {
    "antispam_flood_messages": 3,
    "antispam_duplicate_messages": 2,
    "antispam_mention_limit": 2,
}


def parse_value(field: SettingField, value: Any) -> Any:
    """Type/range/pattern validation for one value. Raises ValueError(msg)."""
    if field.type == "bool":
        return parse_bool(value)
    if field.type == "int":
        n = _parse_int(field, value)
        floor = OFF_OR_MIN.get(field.key)
        if floor is not None and 0 < n < floor:
            raise ValueError(f"Use 0 to turn this rule off, or at least {floor}.")
        return n
    if field.type == "snowflake":
        return _parse_snowflake(value)
    return _parse_str(field, value)


def _serialize(field: SettingField, value: Any) -> str:
    if field.type == "bool":
        return "true" if value else "false"
    return str(value)


def env_default(field: SettingField) -> Any:
    """The .env/config.py value for a field, as it was before any startup override."""
    if field.key in _env_defaults:
        return _env_defaults[field.key]
    return getattr(_config, field.config_attr)


def _parse_stored(field: SettingField, raw: str | None) -> tuple[Any, str | None]:
    """(value, problem). A missing/NULL row is the env default with no problem;
    a malformed row is the env default plus a message."""
    if raw is None:
        return env_default(field), None
    try:
        return parse_value(field, raw), None
    except ValueError as exc:
        return env_default(field), f"Stored value {raw!r} is invalid ({exc}); using the default."


def _api(field: SettingField, value: Any) -> Any:
    """JSON representation: snowflakes as strings (0 = unset -> None)."""
    if field.type == "snowflake":
        return str(value) if value else None
    return value


# ---------------------------------------------------------------------------
# Discord-backed checks (duck-typed so they are unit-testable with fakes)
# ---------------------------------------------------------------------------

def _channel_type(ch: Any) -> Any:
    return getattr(ch, "type", None)


def _is_text(ch: Any) -> bool:
    return _channel_type(ch) in (discord.ChannelType.text, discord.ChannelType.news)


def _is_category(ch: Any) -> bool:
    return _channel_type(ch) == discord.ChannelType.category


def _role_problem(field: SettingField, role: Any, guild: Any) -> str | None:
    if role is None:
        return "No role with this ID in the server."
    is_default = getattr(role, "is_default", None)
    if callable(is_default) and is_default():
        return "@everyone can't be used here."
    if not field.bot_assigns:
        return None
    if getattr(role, "managed", False):
        return "This role is managed by an integration, so the bot can't assign it."
    me = getattr(guild, "me", None)
    if me is not None:
        perms = getattr(me, "guild_permissions", None)
        if perms is not None and not (getattr(perms, "manage_roles", False) or getattr(perms, "administrator", False)):
            return "The bot lacks the Manage Roles permission."
        top = getattr(me, "top_role", None)
        if top is not None and getattr(top, "position", 0) <= getattr(role, "position", 0):
            return "This role is at or above the bot's top role, so the bot can't assign it."
    return None


def _bot_can_post(ch: Any, guild: Any) -> bool:
    me = getattr(guild, "me", None)
    permissions_for = getattr(ch, "permissions_for", None)
    if me is None or not callable(permissions_for):
        return True
    p = permissions_for(me)
    return bool(getattr(p, "view_channel", False) and getattr(p, "send_messages", False)
                and getattr(p, "embed_links", False))


def discord_problem(field: SettingField, value: int, guild: Any) -> str | None:
    """Why a snowflake value is not usable in `guild`, or None."""
    if field.kind == "role":
        return _role_problem(field, guild.get_role(value), guild)
    if field.kind in ("category", "text_channel"):
        ch = guild.get_channel(value)
        if ch is None:
            return "No channel with this ID in the server."
        if field.kind == "category":
            return None if _is_category(ch) else "That channel is not a category."
        if not _is_text(ch):
            return "That channel is not a text channel."
        if not _bot_can_post(ch, guild):
            return "The bot can't post there (needs View Channel, Send Messages and Embed Links)."
    return None


def guild_options(guild: Any, template_names: list[str]) -> dict:
    """Picker options for the Settings page (IDs as strings)."""
    if guild is None:
        return {"roles": [], "categories": [], "text_channels": [], "templates": template_names}
    assign_field = SETTINGS_SCHEMA["verified_role_id"]
    roles = []
    for r in sorted(getattr(guild, "roles", []) or [], key=lambda r: getattr(r, "position", 0), reverse=True):
        if callable(getattr(r, "is_default", None)) and r.is_default():
            continue
        problem = _role_problem(assign_field, r, guild)
        roles.append({"id": str(r.id), "name": r.name, "position": getattr(r, "position", 0),
                      "assignable": problem is None, "problem": problem})
    categories = [{"id": str(c.id), "name": c.name} for c in getattr(guild, "categories", []) or []]
    text_channels = []
    for ch in getattr(guild, "text_channels", []) or []:
        cat = getattr(ch, "category", None)
        text_channels.append({"id": str(ch.id), "name": ch.name, "category": getattr(cat, "name", None),
                              "bot_can_post": _bot_can_post(ch, guild)})
    return {"roles": roles, "categories": categories, "text_channels": text_channels, "templates": template_names}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class SettingsService:
    def __init__(self, repo: SettingsRepository, template_repo: Any = None, audit: Any = None):
        self._repo = repo
        self._templates = template_repo
        self._audit = audit

    # -- reads -------------------------------------------------------------

    async def get_effective(self, guild_id: int) -> dict[str, Any]:
        """{key: saved value} (DB row if valid, else env default), Python types.
        Used by the live ticket settings bridge; never raises on bad rows."""
        stored = await self._repo.get_all(guild_id)
        out: dict[str, Any] = {}
        for key, field in SETTINGS_SCHEMA.items():
            if field.read_only:
                out[key] = getattr(_config, field.config_attr)
            else:
                out[key] = _parse_stored(field, stored.get(key))[0]
        return out

    def schema(self) -> list[dict]:
        return [
            {
                "key": f.key, "type": f.type, "kind": f.kind, "group": f.group, "label": f.label,
                "description": f.description, "live": f.live, "read_only": f.read_only,
                "min": f.min, "max": f.max, "pattern": f.pattern,
            }
            for f in SETTINGS_SCHEMA.values()
        ]

    async def _template_names(self, guild_id: int) -> list[str]:
        if self._templates is None:
            return []
        try:
            rows = await self._templates.list_for_guild(guild_id)
        except Exception:  # noqa: BLE001 - options are best-effort
            log.exception("settings: could not list templates")
            return []
        return sorted({r["name"] for r in rows if r.get("name")})

    async def get_report(self, guild_id: int, guild: Any = None) -> dict:
        """Everything the Settings page needs: values, schema, per-field status
        (saved vs running, pending restart, invalid stored row, live problem)
        and picker options."""
        stored = await self._repo.get_all(guild_id)
        templates = await self._template_names(guild_id)
        values: dict[str, Any] = {}
        fields: dict[str, dict] = {}
        pending: list[str] = []
        for key, field in SETTINGS_SCHEMA.items():
            running = getattr(_config, field.config_attr)
            if field.read_only:
                saved, problem, source = running, None, "env"
            else:
                raw = stored.get(key)
                saved, problem = _parse_stored(field, raw)
                source = "db" if raw is not None and problem is None else "default"
            if field.live:
                running = saved
            warning = None
            if field.type == "snowflake" and saved and guild is not None and field.kind != "user":
                warning = discord_problem(field, saved, guild)
            elif field.kind == "template" and self._templates is not None and saved and saved not in templates:
                warning = f"No template named {saved!r} exists."
            is_pending = not field.live and not field.read_only and saved != running
            if is_pending:
                pending.append(key)
            values[key] = _api(field, saved)
            fields[key] = {
                "saved": _api(field, saved),
                "running": _api(field, running),
                "default": _api(field, env_default(field)),
                "source": source,
                "pending_restart": is_pending,
                "invalid_stored": problem,
                "warning": warning,
            }
        return {
            "values": values,
            "schema": self.schema(),
            "fields": fields,
            "pending_restart": pending,
            "guild_available": guild is not None,
            "options": guild_options(guild, templates),
            # When the running bot applied the saved settings (its start); None
            # if apply_to_config() never ran in this process.
            "applied_at": _applied_at,
        }

    # -- writes ------------------------------------------------------------

    async def validate(self, guild_id: int, updates: dict[str, Any], guild: Any = None) -> dict[str, Any]:
        """Full validation. Returns {key: parsed value or None (= reset)};
        raises SettingsValidationError({key: message}) or GuildUnavailableError."""
        if not isinstance(updates, dict):
            raise SettingsValidationError({"values": "Must be an object."})
        errors: dict[str, str] = {}
        parsed: dict[str, Any] = {}
        for key, value in updates.items():
            field = SETTINGS_SCHEMA.get(key)
            if field is None:
                errors[str(key)] = "Unknown setting."
                continue
            if field.read_only:
                errors[key] = "This setting is read-only here (set it in .env)."
                continue
            if value is None:
                parsed[key] = None
                continue
            try:
                parsed[key] = parse_value(field, value)
            except ValueError as exc:
                errors[key] = str(exc)

        needs_guild = [k for k, v in parsed.items() if v is not None and SETTINGS_SCHEMA[k].type == "snowflake"]
        if needs_guild and guild is None and not errors:
            raise GuildUnavailableError()
        if guild is not None:
            for key in needs_guild:
                problem = discord_problem(SETTINGS_SCHEMA[key], parsed[key], guild)
                if problem:
                    errors[key] = problem

        if self._templates is not None:
            for key, value in parsed.items():
                if value is not None and SETTINGS_SCHEMA[key].kind == "template":
                    if await self._templates.get_by_name(guild_id, value) is None:
                        errors[key] = f"No template named {value!r} exists."

        # Cross-field: the verified and unverified roles must differ.
        if not errors and ("verified_role_id" in parsed or "unverified_role_id" in parsed):
            current = await self.get_effective(guild_id)
            merged = {}
            for k in ("verified_role_id", "unverified_role_id"):
                if k not in parsed:
                    merged[k] = current[k]
                else:
                    merged[k] = parsed[k] if parsed[k] is not None else env_default(SETTINGS_SCHEMA[k])
            if merged["verified_role_id"] and merged["verified_role_id"] == merged["unverified_role_id"]:
                errors["verified_role_id" if "verified_role_id" in parsed else "unverified_role_id"] = (
                    "The verified and unverified roles must be different."
                )

        if errors:
            raise SettingsValidationError(errors)
        return parsed

    async def update(self, guild_id: int, updates: dict[str, Any], updated_by: int, guild: Any = None) -> list[str]:
        """Validate everything first (all-or-nothing), then write only the keys
        that actually change, audit them, and drop the live cache. Returns the
        changed keys."""
        parsed = await self.validate(guild_id, updates, guild)
        stored = await self._repo.get_all(guild_id)
        changes: dict[str, dict] = {}
        for key, new in parsed.items():
            field = SETTINGS_SCHEMA[key]
            raw = stored.get(key)
            old, _ = _parse_stored(field, raw)
            if new is None:
                if raw is None and key not in stored:
                    continue
                await self._repo.delete(guild_id, key)
                changes[key] = {"old": _api(field, old), "new": _api(field, env_default(field)), "reset": True}
                continue
            if raw is not None and _serialize(field, new) == raw:
                continue
            if raw is None and new == old:
                continue  # already the effective (default) value; don't pin it
            await self._repo.set(guild_id, key, _serialize(field, new), updated_by)
            changes[key] = {"old": _api(field, old), "new": _api(field, new)}

        if changes:
            invalidate_cache()
            if self._audit is not None:
                try:
                    await self._audit.log(guild_id, updated_by, "settings_update", target_type="settings", detail=changes)
                except Exception:  # noqa: BLE001 - the save itself succeeded
                    log.exception("settings: failed to write the audit row")
            log.info("Settings changed by %s: %s", updated_by, ", ".join(sorted(changes)))
        return sorted(changes)

    # -- startup -----------------------------------------------------------

    async def apply_to_config(self, guild_id: int) -> dict[str, tuple[Any, Any]]:
        """Called once by main.py after migrations and before any cog loads:
        set config.<ATTR> to each valid saved value (type/range checked; no
        Discord lookups, the gateway is not connected yet). owner_id and other
        read-only fields are never applied. Returns {key: (env, applied)}."""
        for key, field in SETTINGS_SCHEMA.items():
            _env_defaults.setdefault(key, getattr(_config, field.config_attr))

        early = [m for m in IMPORT_TIME_COPIES if m in sys.modules]
        if early:
            log.warning(
                "Settings: %s already imported before settings were applied; their copies of "
                "the overridden config values will be stale until the next restart",
                ", ".join(early),
            )

        stored = await self._repo.get_all(guild_id)
        applied: dict[str, tuple[Any, Any]] = {}
        for key, raw in stored.items():
            field = SETTINGS_SCHEMA.get(key)
            if field is None:
                log.warning("Settings: ignoring unknown stored key %r", key)
                continue
            if field.read_only:
                if raw is not None:
                    log.warning("Settings: ignoring stored %s (read-only; set it in .env)", key)
                continue
            if raw is None:
                continue
            value, problem = _parse_stored(field, raw)
            if problem:
                log.warning("Settings: %s: %s", key, problem)
                continue
            env_value = _env_defaults[key]
            setattr(_config, field.config_attr, value)
            if value != env_value:
                applied[key] = (env_value, value)

        _startup_overrides.clear()
        _startup_overrides.update(applied)
        global _applied_at
        _applied_at = time.time()
        if applied:
            log.info(
                "Settings: applied %d dashboard override(s): %s",
                len(applied), ", ".join(f"{k}={_api(SETTINGS_SCHEMA[k], v)}" for k, (_, v) in sorted(applied.items())),
            )
        else:
            log.info("Settings: no dashboard overrides; using .env values")
        return applied


# ---------------------------------------------------------------------------
# Live-reload cache for the "live" fields. cogs/tickets.py and internal_api/
# routes/tickets.py call get_cached()/set_cached() around get_effective();
# update() invalidates it so a save is visible on the next read.
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS = 5.0
_live_cache: dict[str, tuple[float, Any]] = {}


def get_cached(key: str) -> Any | None:
    entry = _live_cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        return None
    return value


def set_cached(key: str, value: Any) -> None:
    _live_cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, value)


def invalidate_cache() -> None:
    _live_cache.clear()
