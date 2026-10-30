from __future__ import annotations

from database.connection import Database
from database.repositories.base import BaseRepository


class WelcomeRepository(BaseRepository):
    def __init__(self, db: Database) -> None:
        super().__init__(db)

    async def get_config(self, guild_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM welcome_config WHERE guild_id = ?", (guild_id,)
        )
        return dict(row) if row else None

    async def upsert_config(
        self,
        guild_id: int,
        *,
        channel_id: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        existing = await self.get_config(guild_id)
        if existing is None:
            await self._db.execute(
                """
                INSERT INTO welcome_config (guild_id, channel_id, enabled, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (guild_id, channel_id, 1 if enabled is not False else 0),
            )
        else:
            fields: list[str] = []
            values: list = []
            if channel_id is not None:
                fields.append("channel_id = ?")
                values.append(channel_id)
            if enabled is not None:
                fields.append("enabled = ?")
                values.append(1 if enabled else 0)
            if not fields:
                return
            fields.append("updated_at = CURRENT_TIMESTAMP")
            values.append(guild_id)
            await self._db.execute(
                f"UPDATE welcome_config SET {', '.join(fields)} WHERE guild_id = ?",
                tuple(values),
            )
