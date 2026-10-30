from __future__ import annotations

from database.connection import Database
from database.repositories.base import BaseRepository


class WarningRepository(BaseRepository):
    def __init__(self, db: Database) -> None:
        super().__init__(db)

    async def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str | None) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO warnings (guild_id, user_id, moderator_id, reason)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, user_id, moderator_id, reason),
        )

    async def get_warnings(self, guild_id: int, user_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM warnings
            WHERE guild_id = ? AND user_id = ?
            ORDER BY created_at DESC
            """,
            (guild_id, user_id),
        )
        return [dict(r) for r in rows]

    async def list_recent(self, guild_id: int, limit: int = 20) -> list[dict]:
        """Most recent warnings across the whole guild (any user), newest
        first. Used to populate the moderation dashboard's landing state
        (server/routes/moderation.py's GET .../warnings/recent) so a
        moderator sees real activity instead of a blank search box. Plain
        `warnings` rows — no join against member data, since this bot
        process may not have that member cached (mirrors get_warnings'
        note that a warned user may have since left the guild)."""
        rows = await self._db.fetchall(
            """
            SELECT * FROM warnings
            WHERE guild_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [dict(r) for r in rows]

    async def remove_warning(self, guild_id: int, warning_id: int, user_id: int | None = None) -> dict | None:
        """Delete one warning and return the deleted row (truthy), or None
        when there is no such warning. With `user_id`, only a warning that
        belongs to that user is deleted (`!delwarn <user> <id>` and the
        dashboard pass it so a typo'd id can't erase someone else's warning)."""
        if user_id is None:
            row = await self._db.fetchone(
                "SELECT * FROM warnings WHERE id = ? AND guild_id = ?",
                (warning_id, guild_id),
            )
        else:
            row = await self._db.fetchone(
                "SELECT * FROM warnings WHERE id = ? AND guild_id = ? AND user_id = ?",
                (warning_id, guild_id, user_id),
            )
        if not row:
            return None
        await self._db.execute(
            "DELETE FROM warnings WHERE id = ? AND guild_id = ?",
            (warning_id, guild_id),
        )
        return dict(row)
