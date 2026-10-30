"""
Raw-aiosqlite repository for the `guild_settings` key/value table
(database/migrations/sql/024_guild_settings.sql). Follows the same thin,
no-ORM pattern as every other repository in this package (see
database/repositories/guild_repo.py) — no validation/allowlisting here,
that lives one layer up in services/settings_service.py (SETTINGS_SCHEMA).
"""

from __future__ import annotations

from datetime import datetime, timezone

from database.repositories.base import BaseRepository


class SettingsRepository(BaseRepository):
    async def get_all(self, guild_id: int) -> dict[str, str | None]:
        """Every stored row for this guild, as {key: value}. Keys with no
        row at all (never touched via dashboard) simply won't appear —
        callers layer config.py/.env defaults on top for those."""
        rows = await self._db.fetchall(
            "SELECT key, value FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        return {row["key"]: row["value"] for row in rows}

    async def get(self, guild_id: int, key: str) -> str | None:
        row = await self._db.fetchone(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?", (guild_id, key)
        )
        return row["value"] if row else None

    async def set(self, guild_id: int, key: str, value: str | None, updated_by: int) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            """
            INSERT INTO guild_settings (guild_id, key, value, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, key) DO UPDATE SET
                value = excluded.value,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (guild_id, key, value, updated_by, now),
        )

    async def set_many(self, guild_id: int, values: dict[str, str | None], updated_by: int) -> None:
        for key, value in values.items():
            await self.set(guild_id, key, value, updated_by)

    async def delete(self, guild_id: int, key: str) -> None:
        """Remove a row so the field falls back to its .env default ("reset")."""
        await self._db.execute(
            "DELETE FROM guild_settings WHERE guild_id = ? AND key = ?", (guild_id, key)
        )
