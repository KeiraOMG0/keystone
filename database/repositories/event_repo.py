from database.repositories.base import BaseRepository


class EventRepository(BaseRepository):

    async def get_binding(self, guild_id: int, event_type: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM event_bindings WHERE guild_id = ? AND event_type = ?",
            (guild_id, event_type),
        )
        return dict(row) if row else None

    async def get_active(self, guild_id: int, event_type: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM event_bindings WHERE guild_id = ? AND event_type = ? AND enabled = 1",
            (guild_id, event_type),
        )
        return dict(row) if row else None

    async def list_bindings(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM event_bindings WHERE guild_id = ? ORDER BY event_type",
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def get_templates_for_binding(self, binding_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT ebt.position, mt.id, mt.name
            FROM event_binding_templates ebt
            JOIN message_templates mt ON mt.id = ebt.template_id
            WHERE ebt.binding_id = ?
            ORDER BY ebt.position
            """,
            (binding_id,),
        )
        return [dict(r) for r in rows]

    async def create_binding(
        self,
        guild_id: int,
        event_type: str,
        channel_id: int,
        created_by: int,
        send_dm: bool = False,
    ) -> int:
        existing = await self.get_binding(guild_id, event_type)
        if existing:
            await self._db.execute(
                "UPDATE event_bindings SET channel_id = ?, send_dm = ?, enabled = 1, created_by = ? WHERE id = ?",
                (channel_id, int(send_dm), created_by, existing["id"]),
            )
            return existing["id"]
        return await self._db.execute_returning_id(
            """
            INSERT INTO event_bindings (guild_id, event_type, channel_id, send_dm, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, event_type, channel_id, int(send_dm), created_by),
        )

    async def set_template(self, binding_id: int, template_id: int, position: int = 0) -> None:
        existing = await self._db.fetchone(
            "SELECT id FROM event_binding_templates WHERE binding_id = ? AND template_id = ?",
            (binding_id, template_id),
        )
        if existing:
            await self._db.execute(
                "UPDATE event_binding_templates SET position = ? WHERE binding_id = ? AND template_id = ?",
                (position, binding_id, template_id),
            )
        else:
            await self._db.execute(
                "INSERT INTO event_binding_templates (binding_id, template_id, position) VALUES (?, ?, ?)",
                (binding_id, template_id, position),
            )

    async def clear_templates(self, binding_id: int) -> None:
        await self._db.execute(
            "DELETE FROM event_binding_templates WHERE binding_id = ?",
            (binding_id,),
        )

    async def delete_binding(self, guild_id: int, event_type: str) -> bool:
        row = await self.get_binding(guild_id, event_type)
        if not row:
            return False
        await self._db.execute(
            "DELETE FROM event_bindings WHERE id = ?",
            (row["id"],),
        )
        return True

    async def set_enabled(self, guild_id: int, event_type: str, enabled: bool) -> bool:
        row = await self.get_binding(guild_id, event_type)
        if not row:
            return False
        await self._db.execute(
            "UPDATE event_bindings SET enabled = ? WHERE id = ?",
            (int(enabled), row["id"]),
        )
        return True
