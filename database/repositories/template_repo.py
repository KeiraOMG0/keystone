from database.repositories.base import BaseRepository


class TemplateRepository(BaseRepository):

    async def get_by_id(self, template_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM message_templates WHERE id = ?",
            (template_id,),
        )
        return dict(row) if row else None

    async def get_by_name(self, guild_id: int, name: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM message_templates WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return dict(row) if row else None

    async def list_for_guild(self, guild_id: int, category: str = None) -> list[dict]:
        if category is not None:
            rows = await self._db.fetchall(
                "SELECT * FROM message_templates WHERE guild_id = ? AND category = ? ORDER BY name",
                (guild_id, category),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM message_templates WHERE guild_id = ? ORDER BY name",
                (guild_id,),
            )
        return [dict(row) for row in rows]

    async def create(
        self,
        guild_id: int,
        name: str,
        created_by: int,
        description: str = None,
        category: str = None,
        content: str = None,
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO message_templates (guild_id, name, created_by, description, category, content)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, name, created_by, description, category, content),
        )

    async def update(self, template_id: int, updated_by: int, **kwargs) -> None:
        fields = {**kwargs, "updated_by": updated_by, "updated_at": "CURRENT_TIMESTAMP"}
        set_parts = []
        values = []
        for key, val in fields.items():
            if key == "updated_at":
                set_parts.append("updated_at = CURRENT_TIMESTAMP")
            else:
                set_parts.append(f"{key} = ?")
                values.append(val)
        values.append(template_id)
        await self._db.execute(
            f"UPDATE message_templates SET {', '.join(set_parts)} WHERE id = ?",
            tuple(values),
        )

    async def delete(self, guild_id: int, name: str) -> bool:
        row = await self._db.fetchone(
            "SELECT id FROM message_templates WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        if not row:
            return False
        await self._db.execute(
            "DELETE FROM message_templates WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return True

    async def get_full(self, template_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM message_templates WHERE id = ?",
            (template_id,),
        )
        if not row:
            return None
        template = dict(row)

        embed_rows = await self._db.fetchall(
            "SELECT * FROM template_embeds WHERE template_id = ? ORDER BY position",
            (template_id,),
        )
        embeds = []
        for embed_row in embed_rows:
            embed = dict(embed_row)
            field_rows = await self._db.fetchall(
                "SELECT * FROM embed_fields WHERE embed_id = ? ORDER BY position",
                (embed["id"],),
            )
            embed["fields"] = [dict(f) for f in field_rows]
            embeds.append(embed)

        template["embeds"] = embeds
        return template
