"""Bot audit log (`audit_log` table): writes, reads for the dashboard Audit
page, the live `audit` topic, and retention.

Wire shape of one entry (`serialize_row`, used by both GET /guilds/{id}/audit
and the live `audit` event, so the frontend can treat them identically):

    {"id": int, "guild_id": str, "actor_id": str, "action": str,
     "target_type": str | None, "target_id": str | None,
     "detail": <parsed detail_json or None>, "detail_raw": str | None,
     "created_at": "YYYY-MM-DDTHH:MM:SSZ"}

`detail_raw` is set only when detail_json is not valid JSON. Integers in
`detail` too large for a JS number (every Discord snowflake) become strings.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

from database.repositories.audit_repo import AuditRepository, utc_now_ts
from services.live_events import publish as live_publish

log = logging.getLogger(__name__)

# Retention: rows older than this are deleted, at most once per
# PRUNE_INTERVAL per process, piggybacking on the next write (no scheduler).
# Set AUDIT_RETENTION_DAYS to None to keep everything.
AUDIT_RETENTION_DAYS: int | None = 365
PRUNE_INTERVAL = datetime.timedelta(days=1)
_last_prune: datetime.datetime | None = None

_JS_MAX_SAFE_INT = 2**53 - 1


def _safe_json_value(value: Any) -> Any:
    """Recursively turn ints a browser would round (|n| > 2**53-1) into strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > _JS_MAX_SAFE_INT else value
    if isinstance(value, dict):
        return {str(k): _safe_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(v) for v in value]
    return value


def _iso(created_at: Any) -> str | None:
    """SQLite "YYYY-MM-DD HH:MM:SS" (naive UTC) -> "YYYY-MM-DDTHH:MM:SSZ"."""
    if created_at is None:
        return None
    text = str(created_at).strip()
    if "T" not in text and " " in text:
        text = text.replace(" ", "T", 1)
    if not (text.endswith("Z") or "+" in text[10:]):
        text += "Z"
    return text


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def serialize_row(row: dict) -> dict:
    """One audit_log row as it goes on the wire (see module docstring)."""
    detail: Any = None
    detail_raw: str | None = None
    raw = row.get("detail_json")
    if raw is not None and raw != "":
        try:
            detail = _safe_json_value(json.loads(raw))
        except (TypeError, ValueError):
            detail_raw = str(raw)[:2000]
    return {
        "id": row.get("id"),
        "guild_id": _str_or_none(row.get("guild_id")),
        "actor_id": _str_or_none(row.get("actor_id")),
        "action": row.get("action"),
        "target_type": row.get("target_type"),
        # target_id may be a snowflake (member/role/channel) or a small db id
        # (template, ticket): always a string on the wire.
        "target_id": _str_or_none(row.get("target_id")),
        "detail": detail,
        "detail_raw": detail_raw,
        "created_at": _iso(row.get("created_at")),
    }


class AuditService:
    def __init__(self, audit_repo: AuditRepository):
        self._repo = audit_repo

    async def log(
        self,
        guild_id: int,
        actor_id: int,
        action: str,
        target_type: str = None,
        target_id: int = None,
        detail: dict = None,
    ) -> int:
        created_at = utc_now_ts()
        row_id = await self._repo.log(
            guild_id, actor_id, action, target_type, target_id, detail, created_at=created_at
        )
        # Live `audit` topic (view_audit). Never raises; must not affect the caller.
        # Same shape as GET /audit entries, including the row id (the page
        # dedupes on it).
        live_publish("audit", {
            "kind": "audit",
            "row": serialize_row({
                "id": row_id, "guild_id": guild_id, "actor_id": actor_id, "action": action,
                "target_type": target_type, "target_id": target_id,
                "detail_json": json.dumps(detail, default=str) if detail is not None else None,
                "created_at": created_at,
            }),
        })
        await self._maybe_prune()
        return row_id

    async def _maybe_prune(self) -> None:
        global _last_prune
        if AUDIT_RETENTION_DAYS is None:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        if _last_prune is not None and now - _last_prune < PRUNE_INTERVAL:
            return
        _last_prune = now
        try:
            removed = await self._repo.prune_older_than(now - datetime.timedelta(days=AUDIT_RETENTION_DAYS))
            if removed:
                log.info("audit_log retention: removed %d row(s) older than %d days", removed, AUDIT_RETENTION_DAYS)
        except Exception:  # retention must never break the write that triggered it
            log.exception("audit_log retention prune failed")

    async def get_recent(self, guild_id: int, limit: int = 50) -> list[dict]:
        return await self._repo.get_recent(guild_id, limit)

    async def query(
        self,
        guild_id: int,
        *,
        action: str | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
        target_type: str | None = None,
        since: datetime.datetime | None = None,
        until: datetime.datetime | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> dict:
        """One page, newest first: {"entries": [...], "next_before_id": int | None}.

        Fetches limit+1 rows to know whether another page exists, so
        `next_before_id` is None exactly on the last page.
        """
        rows = await self._repo.query(
            guild_id, action=action, actor_id=actor_id, target_id=target_id, target_type=target_type,
            since=since, until=until, before_id=before_id, limit=limit + 1,
        )
        page = rows[:limit]
        has_more = len(rows) > limit
        return {
            "entries": [serialize_row(r) for r in page],
            "next_before_id": page[-1]["id"] if has_more and page else None,
        }

    async def list_actions(self, guild_id: int) -> list[dict]:
        return await self._repo.distinct_actions(guild_id)
