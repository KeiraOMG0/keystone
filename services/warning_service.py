from __future__ import annotations

from typing import Any

from database.repositories.warning_repo import WarningRepository
from services.live_events import publish as live_publish


class WarningService:
    """Warnings (the `warnings` table is the system of record).

    `audit` (an AuditService) is optional so older callers/tests keep working;
    when it is set, add_warning/remove_warning write the `member_warned` /
    `warning_removed` audit_log rows themselves, with the acting moderator, so
    the Discord commands and the dashboard record them identically."""

    def __init__(self, repo: WarningRepository, audit: Any | None = None):
        self._repo = repo
        self._audit = audit

    async def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str | None) -> int:
        warning_id = await self._repo.add_warning(guild_id, user_id, moderator_id, reason)
        live_publish("moderation", {
            "kind": "warn", "guild_id": guild_id, "user_id": user_id,
            "moderator_id": moderator_id, "reason": reason, "warning_id": warning_id,
        })
        if self._audit is not None:
            await self._audit.log(
                guild_id, moderator_id, "member_warned",
                target_type="member", target_id=user_id,
                detail={"warning_id": warning_id, "reason": reason},
            )
        return warning_id

    async def get_warnings(self, guild_id: int, user_id: int) -> list[dict]:
        return await self._repo.get_warnings(guild_id, user_id)

    async def list_recent(self, guild_id: int, limit: int = 20) -> list[dict]:
        return await self._repo.list_recent(guild_id, limit)

    async def remove_warning(
        self,
        guild_id: int,
        warning_id: int,
        *,
        user_id: int | None = None,
        actor_id: int | None = None,
    ) -> dict | None:
        """Delete a warning. Returns the deleted row (truthy) or None.

        `user_id`: only delete it if it belongs to that user. `actor_id`: who
        removed it; recorded in the `warning_removed` audit row (when an
        AuditService was given) and in the live event."""
        removed = await self._repo.remove_warning(guild_id, warning_id, user_id)
        if not removed:
            return None
        row = removed if isinstance(removed, dict) else {}
        owner_id = row.get("user_id", user_id)
        live_publish("moderation", {
            "kind": "warning_removed", "guild_id": guild_id, "warning_id": warning_id,
            "user_id": owner_id, "moderator_id": actor_id,
        })
        if self._audit is not None and actor_id is not None:
            await self._audit.log(
                guild_id, actor_id, "warning_removed",
                target_type="member", target_id=owner_id,
                detail={"warning_id": warning_id, "reason": row.get("reason")},
            )
        return row or {"id": warning_id}
