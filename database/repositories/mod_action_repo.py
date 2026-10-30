from __future__ import annotations

import json

from database.connection import Database
from database.repositories.base import BaseRepository


class ModActionRepository(BaseRepository):
    def __init__(self, db: Database) -> None:
        super().__init__(db)

    async def add_action(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action_type: str,
        reason: str | None = None,
        duration_minutes: int | None = None,
        expires_at: str | None = None,
        extra_data: dict | None = None,
    ) -> int:
        extra_json = json.dumps(extra_data) if extra_data is not None else None
        return await self._db.execute_returning_id(
            """
            INSERT INTO mod_actions
                (guild_id, user_id, moderator_id, action_type, reason,
                 duration_minutes, expires_at, extra_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, moderator_id, action_type, reason,
             duration_minutes, expires_at, extra_json),
        )

    async def get_actions(self, guild_id: int, user_id: int, limit: int = 100) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM mod_actions
            WHERE guild_id = ? AND user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (guild_id, user_id, limit),
        )
        return [_deserialize(dict(r)) for r in rows]

    async def get_recent(self, guild_id: int, limit: int = 50) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM mod_actions
            WHERE guild_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [_deserialize(dict(r)) for r in rows]


def _deserialize(row: dict) -> dict:
    if row.get("extra_data"):
        try:
            row["extra_data"] = json.loads(row["extra_data"])
        except (TypeError, ValueError):
            pass
    return row
