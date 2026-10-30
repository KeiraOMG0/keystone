"""
Discord snowflake ID stringification for JSON responses crossing the
internal-API -> dashboard-backend -> browser boundary.

THE BUG THIS FIXES: Discord snowflake IDs (channel/role/user/guild/message
IDs) are 64-bit integers that regularly exceed JavaScript's
Number.MAX_SAFE_INTEGER (2**53 - 1 = 9007199254740991). Every route in this
package used to return plain dicts (`-> dict`) with these IDs as raw Python
`int` values. FastAPI's default JSON encoder serializes a Python int as a
bare JSON number literal (e.g. `987654321098765432`), and the instant a
browser's `JSON.parse` (inside `fetch(...).json()`) reads a number literal
larger than 2**53-1, it silently rounds to the nearest representable double
— e.g. 987654321098765432 becomes 987654321098765400. This corruption
happens BEFORE any frontend code runs; it is not fixable client-side.

The fix: convert snowflake-shaped fields to strings (`str(value)`) before
they leave this process as JSON. A JSON string is never subject to
Number.MAX_SAFE_INTEGER — `JSON.parse('"987654321098765432"')` round-trips
exactly. Python has arbitrary-precision ints, so nothing is lost by holding
these as int internally and only stringifying at the JSON boundary.

This module does NOT touch database autoincrement IDs (ticket.id,
warning.id, template.id, panel.id, entry.id, mod_action.id, grant.id) —
those are small (SQLite AUTOINCREMENT, will never remotely approach 2**53)
and are deliberately left as JSON numbers so the frontend's existing
`number`-typed fields for them keep working unchanged.

Usage: call `stringify_snowflakes(obj, keys)` on a dict (or list of dicts)
right before returning it from a route handler, passing the set of key
names in that specific dict shape that are known Discord snowflakes. Only
listed keys are touched; every other key (db ids, strings, bools, nested
dicts/lists not covered by SNOWFLAKE_KEYS) passes through unchanged unless
also processed via `keys=SNOWFLAKE_KEYS` for a generic sweep.
"""

from __future__ import annotations

from typing import Any

# Every key name that has been confirmed (by reading the DB schema / discord.py
# call site producing it) to hold a real Discord snowflake ID anywhere in this
# codebase's dict-shaped API responses. Deliberately does NOT include: id,
# ticket_id, panel_id, entry_id, warning_id, template_id, embed_id,
# position, action_id, grant id ("id" on permission_grants rows) — all of
# those are small DB autoincrement integers, not Discord snowflakes.
SNOWFLAKE_KEYS: frozenset[str] = frozenset(
    {
        "guild_id",
        "channel_id",
        "role_id",
        "user_id",
        "moderator_id",
        "granted_by",
        "created_by",
        "updated_by",
        "actor_user_id",
        "actor_id",
        "opened_by",
        "claimed_by",
        "author_id",
        "message_id",
        "control_message_id",
        "open_category_id",
        "closed_category_id",
        "transcript_channel_id",
        "ping_role_id",
        "staff_ping_role_id",
        "approval_channel_id",
        "unverified_role_id",
        "verified_role_id",
        "unverified_category_id",
        "log_channel_id",
        "join_leave_channel_id",
        "owner_id",
        "category_id",
        "warning_message_id",
        "stats_message_id",
        "reviewed_by",
        # dashboard Roles page (services/role_admin_service.py)
        "top_role_id",
        "source_role_id",
        # Fortnite shop config (fortnite_shop_config.last_message_id)
        "last_message_id",
        # dashboard Birthdays page (birthday_settings)
        "birthday_role_id",
        "announce_channel_id",
        # dashboard Automation page (autorole_rules.trigger_role_id) and
        # template history (template_versions.changed_by, a user id)
        "trigger_role_id",
        "changed_by",
        # user cache (services/user_cache_service.py serialize_departed)
        "departed_by_id",
        # dashboard Music page (cogs/music.py dashboard_state: current track requester)
        "requester_id",
    }
)


def _stringify_value(value: Any) -> Any:
    if value is None:
        return None
    return str(value)


def stringify_snowflakes(obj: Any, keys: frozenset[str] = SNOWFLAKE_KEYS) -> Any:
    """Recursively walks dicts/lists, converting any value whose key is in
    `keys` to a string (None stays None). Returns a NEW structure — does not
    mutate the input in place (safe to call on rows that might be reused /
    cached upstream, e.g. repository dict results).

    Only descends into dicts/lists; leaves everything else (str, bool, int
    values under non-snowflake keys, etc.) untouched.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key in keys and isinstance(value, (int, bool)) and not isinstance(value, bool):
                out[key] = _stringify_value(value)
            elif isinstance(value, (dict, list)):
                out[key] = stringify_snowflakes(value, keys)
            else:
                out[key] = value
        return out
    if isinstance(obj, list):
        return [stringify_snowflakes(item, keys) for item in obj]
    return obj
