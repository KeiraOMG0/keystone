from database.repositories.base import BaseRepository


class ManagedMessageRepository(BaseRepository):

    async def create(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        template_id: int,
        created_by: int,
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO managed_messages (guild_id, channel_id, message_id, template_id, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, message_id, template_id, created_by),
        )

    async def get_by_message_id(self, message_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM managed_messages WHERE message_id = ?",
            (message_id,),
        )
        return dict(row) if row else None

    async def list_for_template(self, template_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM managed_messages WHERE template_id = ?",
            (template_id,),
        )
        return [dict(r) for r in rows]

    async def list_for_guild(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT mm.*, mt.name AS template_name
            FROM managed_messages mm
            JOIN message_templates mt ON mt.id = mm.template_id
            WHERE mm.guild_id = ?
            ORDER BY mm.created_at DESC
            """,
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def mark_synced(self, message_id: int) -> None:
        await self._db.execute(
            "UPDATE managed_messages SET last_synced_at = CURRENT_TIMESTAMP, sync_failed = 0 WHERE message_id = ?",
            (message_id,),
        )

    async def mark_failed(self, message_id: int) -> None:
        await self._db.execute(
            "UPDATE managed_messages SET sync_failed = 1 WHERE message_id = ?",
            (message_id,),
        )

    async def delete(self, message_id: int) -> bool:
        row = await self._db.fetchone(
            "SELECT id FROM managed_messages WHERE message_id = ?",
            (message_id,),
        )
        if not row:
            return False
        await self._db.execute(
            "DELETE FROM managed_messages WHERE message_id = ?",
            (message_id,),
        )
        return True

    # ------------------------------------------------------------------
    # Template components
    # ------------------------------------------------------------------

    async def get_components(self, template_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM template_components WHERE template_id = ? ORDER BY row_index, position",
            (template_id,),
        )
        return [dict(r) for r in rows]

    async def add_component(
        self,
        template_id: int,
        row_index: int,
        component_type: str,
        position: int = 0,
        **kwargs,
    ) -> int:
        allowed = {"label", "emoji", "style", "custom_id", "url", "placeholder", "options_json"}
        cols = ["template_id", "row_index", "component_type", "position"]
        vals: list = [template_id, row_index, component_type, position]
        for key in allowed:
            if key in kwargs and kwargs[key] is not None:
                cols.append(key)
                vals.append(kwargs[key])
        placeholders = ", ".join("?" for _ in vals)
        col_str = ", ".join(cols)
        return await self._db.execute_returning_id(
            f"INSERT INTO template_components ({col_str}) VALUES ({placeholders})",
            tuple(vals),
        )

    async def delete_components(self, template_id: int) -> None:
        await self._db.execute(
            "DELETE FROM template_components WHERE template_id = ?",
            (template_id,),
        )
