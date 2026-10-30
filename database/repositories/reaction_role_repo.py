from database.repositories.base import BaseRepository


class ReactionRoleRepository(BaseRepository):

    async def create_panel(
        self,
        guild_id: int,
        channel_id: int,
        created_by: int,
        title: str = "React for a role!",
        exclusive: bool = False,
        template_id: int | None = None,
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO reaction_role_panels (guild_id, channel_id, created_by, title, exclusive, template_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, created_by, title, int(exclusive), template_id),
        )

    async def get_panel(self, panel_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM reaction_role_panels WHERE id = ?", (panel_id,)
        )
        return dict(row) if row else None

    async def get_panel_by_message(self, message_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM reaction_role_panels WHERE message_id = ?", (message_id,)
        )
        return dict(row) if row else None

    async def list_panels(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM reaction_role_panels WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def get_all_panels(self) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM reaction_role_panels WHERE message_id IS NOT NULL"
        )
        return [dict(r) for r in rows]

    async def set_message(self, panel_id: int, message_id: int) -> None:
        await self._db.execute(
            "UPDATE reaction_role_panels SET message_id = ? WHERE id = ?",
            (message_id, panel_id),
        )

    async def update_panel(
        self,
        panel_id: int,
        title: str | None = None,
        exclusive: bool | None = None,
    ) -> None:
        """Partial update of panel metadata. Only non-None fields are applied.

        Added for the dashboard self-roles editor (server/routes/reaction_roles.py) —
        the Discord-side /reactionrole commands only ever set title/exclusive at
        creation time via create_panel, so this is the first UPDATE path for them.
        """
        fields: list[str] = []
        params: list = []
        if title is not None:
            fields.append("title = ?")
            params.append(title)
        if exclusive is not None:
            fields.append("exclusive = ?")
            params.append(int(exclusive))
        if not fields:
            return
        params.append(panel_id)
        await self._db.execute(
            f"UPDATE reaction_role_panels SET {', '.join(fields)} WHERE id = ?",
            tuple(params),
        )

    async def delete_panel(self, panel_id: int) -> None:
        await self._db.execute(
            "DELETE FROM reaction_role_panels WHERE id = ?", (panel_id,)
        )

    async def add_entry(
        self,
        panel_id: int,
        emoji: str,
        role_id: int,
        label: str | None = None,
        position: int = 0,
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO reaction_role_entries (panel_id, emoji, role_id, label, position)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (panel_id, emoji) DO UPDATE SET role_id = excluded.role_id, label = excluded.label
            """,
            (panel_id, emoji, role_id, label, position),
        )

    async def remove_entry(self, panel_id: int, emoji: str) -> bool:
        row = await self._db.fetchone(
            "SELECT id FROM reaction_role_entries WHERE panel_id = ? AND emoji = ?",
            (panel_id, emoji),
        )
        if not row:
            return False
        await self._db.execute(
            "DELETE FROM reaction_role_entries WHERE panel_id = ? AND emoji = ?",
            (panel_id, emoji),
        )
        return True

    async def remove_entry_by_id(self, panel_id: int, entry_id: int) -> bool:
        """Same as remove_entry but keyed by entry id — the dashboard editor
        (server/routes/reaction_roles.py) addresses existing rows by id since
        it lists entries with their ids already, rather than round-tripping
        the emoji string back for a delete."""
        row = await self._db.fetchone(
            "SELECT id FROM reaction_role_entries WHERE panel_id = ? AND id = ?",
            (panel_id, entry_id),
        )
        if not row:
            return False
        await self._db.execute(
            "DELETE FROM reaction_role_entries WHERE panel_id = ? AND id = ?",
            (panel_id, entry_id),
        )
        return True

    async def get_entries(self, panel_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM reaction_role_entries WHERE panel_id = ? ORDER BY position",
            (panel_id,),
        )
        return [dict(r) for r in rows]

    async def get_entry_by_emoji(self, panel_id: int, emoji: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM reaction_role_entries WHERE panel_id = ? AND emoji = ?",
            (panel_id, emoji),
        )
        return dict(row) if row else None
