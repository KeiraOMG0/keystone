"""
GET /api/settings   -> settings report (values, schema, per-field status, picker options)
PUT /api/settings   {values: {key: value|null}} -> report + {"changed": [...]}

Staff-only passthrough of internal_api/routes/settings.py (services.
settings_service.SettingsService is the authority for the allowlist, the
validation and the audit row).

Gating: require_staff, NOT a grantable permission: this page changes
security-relevant config (verification roles, log channels), so gating it on
a grant from the system being configured would let a role escalate itself.

Validation: the body's `values` are checked here first against a mirror of the
bot's type/range rules (_FIELD_RULES); Discord-backed rules (the ID exists,
is the right kind, the bot can assign the role / post in the channel, the
template exists) only exist bot-side. Either way an invalid request is
422 {"detail": {"error": "validation_failed", "message", "fields": {key: msg}}}.
`updated_by` (audit actor) always comes from the session.

Error mapping: internal 400/404/409/422/503-with-detail keep their status and
{"error", "message"} body; a 403 from the bot becomes 409 discord_forbidden
(never confused with a dashboard 403); only an unreachable bot is 502
internal_api_unavailable.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server import config
from server.db.models import Session
from server.permissions.guards import require_staff
from server.services import internal_client
from server.services.internal_client import InternalAPIError

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Mirror of services/settings_service.py SETTINGS_SCHEMA (type + range only).
# Keep in sync with the bot; the bot re-validates everything anyway.
_FIELD_RULES: dict[str, dict[str, Any]] = {
    "unverified_role_id": {"type": "snowflake"},
    "verified_role_id": {"type": "snowflake"},
    "unverified_category_id": {"type": "snowflake"},
    "check_interval_minutes": {"type": "int", "min": 1, "max": 1440},
    "auto_kick": {"type": "bool"},
    "verify_template_name": {"type": "str", "min": 1, "max": 100},
    "verify_ticket_welcome_template_name": {"type": "str", "min": 1, "max": 100},
    "log_channel_id": {"type": "snowflake"},
    "join_leave_channel_id": {"type": "snowflake"},
    "ticket_chat_logging_enabled": {"type": "bool"},
    "ticket_archive_purge_days": {"type": "int", "min": 1, "max": 3650},
    "owner_id": {"type": "snowflake", "read_only": True},
    "music_default_search": {"type": "str", "pattern": r"^(yt|sc)search[1-9][0-9]?$"},
    "music_allow_playlists": {"type": "bool"},
    "music_default_volume": {"type": "int", "min": 0, "max": 100},
    "music_inactivity_timeout": {"type": "int", "min": 30, "max": 86400},
    # off_or_min mirrors services/settings_service.py OFF_OR_MIN (0 = rule off).
    "antispam_enabled": {"type": "bool"},
    "antispam_flood_messages": {"type": "int", "min": 0, "max": 50, "off_or_min": 3},
    "antispam_flood_seconds": {"type": "int", "min": 2, "max": 60},
    "antispam_duplicate_messages": {"type": "int", "min": 0, "max": 50, "off_or_min": 2},
    "antispam_duplicate_seconds": {"type": "int", "min": 5, "max": 600},
    "antispam_mention_limit": {"type": "int", "min": 0, "max": 50, "off_or_min": 2},
    "antispam_timeout_minutes": {"type": "int", "min": 0, "max": 40320},
}


def _check(rule: dict[str, Any], value: Any) -> str | None:
    """Error message for one value, or None. null (reset to default) is always fine."""
    if value is None:
        return None
    kind = rule["type"]
    if kind == "bool":
        if isinstance(value, bool) or (isinstance(value, int) and value in (0, 1)):
            return None
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
            return None
        return "Must be true or false."
    if kind == "int":
        if isinstance(value, bool):
            return "Must be a whole number."
        if isinstance(value, str) and re.fullmatch(r"\s*-?[0-9]+\s*", value):
            value = int(value)
        if not isinstance(value, int):
            return "Must be a whole number."
        if value < rule["min"]:
            return f"Must be at least {rule['min']}."
        if value > rule["max"]:
            return f"Must be at most {rule['max']}."
        if "off_or_min" in rule and 0 < value < rule["off_or_min"]:
            return f"Use 0 to turn this rule off, or at least {rule['off_or_min']}."
        return None
    if kind == "snowflake":
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]{15,21}", str(value).strip()):
            return "Must be a Discord ID (15 to 21 digits)."
        return None
    if not isinstance(value, str):
        return "Must be text."
    s = value.strip()
    if "pattern" in rule:
        if not re.fullmatch(rule["pattern"], s):
            return "Must be ytsearchN or scsearchN, where N is 1 to 99 (e.g. ytsearch7)."
        return None
    if len(s) < rule.get("min", 0):
        return "Can't be empty."
    if len(s) > rule.get("max", 10_000):
        return f"Must be at most {rule['max']} characters."
    return None


def validate_values(values: Any) -> dict[str, str]:
    """{key: message} for every problem in a PUT body's `values` (empty = ok)."""
    if not isinstance(values, dict):
        return {"values": "Must be an object."}
    errors: dict[str, str] = {}
    for key, value in values.items():
        rule = _FIELD_RULES.get(key)
        if rule is None:
            errors[str(key)] = "Unknown setting."
        elif rule.get("read_only"):
            errors[key] = "This setting is read-only here (set it in .env)."
        else:
            msg = _check(rule, value)
            if msg:
                errors[key] = msg
    return errors


def _translate(exc: InternalAPIError) -> HTTPException:
    code = exc.status_code
    detail = exc.detail if isinstance(exc.detail, dict) else None
    if code in (400, 404, 409, 422):
        if detail is None:
            detail = {"error": "invalid_request" if code in (400, 422) else ("not_found" if code == 404 else "conflict")}
        return HTTPException(status_code=code, detail=detail)
    if code == 403:
        if detail is None or detail.get("error") == "forbidden":
            detail = {"error": "discord_forbidden", "message": "Discord refused the change."}
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    if code == 503 and detail is not None:
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": "internal_api_unavailable"})


@router.get("")
async def get_settings(session: Session = Depends(require_staff)) -> dict:
    try:
        return await internal_client.get_settings(config.DASHBOARD_GUILD_ID)
    except InternalAPIError as exc:
        raise _translate(exc) from exc


class UpdateSettingsBody(BaseModel):
    values: dict[str, Any] = {}


@router.put("")
async def update_settings(
    body: UpdateSettingsBody,
    session: Session = Depends(require_staff),
) -> dict:
    errors = validate_values(body.values)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"error": "validation_failed", "message": "Some settings are invalid.", "fields": errors},
        )
    if not body.values:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "message": "No settings to change."},
        )
    try:
        return await internal_client.update_settings(
            config.DASHBOARD_GUILD_ID, body.values, session.discord_user_id
        )
    except InternalAPIError as exc:
        raise _translate(exc) from exc
