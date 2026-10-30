from datetime import datetime, timezone

from database.repositories.base import BaseRepository


class GuildRepository(BaseRepository):
    async def get(self, guild_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        )
        return dict(row) if row else None

    async def get_or_create(self, guild_id: int) -> dict:
        existing = await self.get(guild_id)
        if existing:
            return existing

        await self._db.execute(
            "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,)
        )
        return await self.get(guild_id)

    async def update(self, guild_id: int, **kwargs):
        if not kwargs:
            return

        kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        set_clause = ", ".join(f"{key} = ?" for key in kwargs)
        values = list(kwargs.values()) + [guild_id]

        await self._db.execute(
            f"UPDATE guild_config SET {set_clause} WHERE guild_id = ?",
            tuple(values),
        )
