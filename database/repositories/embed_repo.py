from database.repositories.base import BaseRepository


class EmbedRepository(BaseRepository):

    async def get_for_template(self, template_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM template_embeds WHERE template_id = ? ORDER BY position",
            (template_id,),
        )
        return [dict(row) for row in rows]

    async def create(self, template_id: int, position: int = 0, **kwargs) -> int:
        allowed = {
            "title", "description", "color", "url", "footer_text", "footer_icon_url",
            "thumbnail_url", "image_url", "author_name", "author_icon_url", "author_url",
            "timestamp_mode",
        }
        extra = {k: v for k, v in kwargs.items() if k in allowed}
        columns = ["template_id", "position"] + list(extra.keys())
        placeholders = ", ".join("?" * len(columns))
        values = [template_id, position] + list(extra.values())
        return await self._db.execute_returning_id(
            f"INSERT INTO template_embeds ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(values),
        )

    async def update(self, embed_id: int, **kwargs) -> None:
        set_parts = [f"{k} = ?" for k in kwargs]
        values = list(kwargs.values()) + [embed_id]
        await self._db.execute(
            f"UPDATE template_embeds SET {', '.join(set_parts)} WHERE id = ?",
            tuple(values),
        )

    async def delete(self, embed_id: int) -> None:
        await self._db.execute(
            "DELETE FROM template_embeds WHERE id = ?",
            (embed_id,),
        )

    async def get_fields(self, embed_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM embed_fields WHERE embed_id = ? ORDER BY position",
            (embed_id,),
        )
        return [dict(row) for row in rows]

    async def add_field(
        self,
        embed_id: int,
        name: str,
        value: str,
        inline: bool = False,
        position: int = None,
    ) -> int:
        if position is None:
            row = await self._db.fetchone(
                "SELECT COALESCE(MAX(position) + 1, 0) AS next_pos FROM embed_fields WHERE embed_id = ?",
                (embed_id,),
            )
            position = dict(row)["next_pos"]
        return await self._db.execute_returning_id(
            "INSERT INTO embed_fields (embed_id, position, name, value, inline) VALUES (?, ?, ?, ?, ?)",
            (embed_id, position, name, value, 1 if inline else 0),
        )

    async def update_field(self, field_id: int, **kwargs) -> None:
        set_parts = [f"{k} = ?" for k in kwargs]
        values = list(kwargs.values()) + [field_id]
        await self._db.execute(
            f"UPDATE embed_fields SET {', '.join(set_parts)} WHERE id = ?",
            tuple(values),
        )

    async def delete_field(self, field_id: int) -> None:
        await self._db.execute(
            "DELETE FROM embed_fields WHERE id = ?",
            (field_id,),
        )

    async def reorder_fields(self, embed_id: int, field_ids_in_order: list[int]) -> None:
        for position, field_id in enumerate(field_ids_in_order):
            await self._db.execute(
                "UPDATE embed_fields SET position = ? WHERE id = ? AND embed_id = ?",
                (position, field_id, embed_id),
            )
