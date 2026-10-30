from database.repositories.base import BaseRepository


class PermissionRepository(BaseRepository):

    async def grant(self, guild_id: int, role_id: int, permission: str, granted_by: int) -> None:
        await self._db.execute(
            """
            INSERT INTO permission_grants (guild_id, role_id, permission, granted_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (guild_id, role_id, permission) DO UPDATE SET granted_by = excluded.granted_by, granted_at = CURRENT_TIMESTAMP
            """,
            (guild_id, role_id, permission, granted_by),
        )

    async def revoke(self, guild_id: int, role_id: int, permission: str) -> bool:
        row = await self._db.fetchone(
            "SELECT id FROM permission_grants WHERE guild_id = ? AND role_id = ? AND permission = ?",
            (guild_id, role_id, permission),
        )
        if not row:
            return False
        await self._db.execute(
            "DELETE FROM permission_grants WHERE guild_id = ? AND role_id = ? AND permission = ?",
            (guild_id, role_id, permission),
        )
        return True

    async def list_grants(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM permission_grants WHERE guild_id = ? ORDER BY permission, role_id",
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def get_role_permissions(self, guild_id: int, role_id: int) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT permission FROM permission_grants WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        return [r["permission"] for r in rows]

    async def has_permission(self, guild_id: int, role_ids: list[int], permission: str) -> bool:
        if not role_ids:
            return False
        placeholders = ",".join("?" for _ in role_ids)
        row = await self._db.fetchone(
            f"SELECT id FROM permission_grants WHERE guild_id = ? AND role_id IN ({placeholders}) AND permission = ?",
            (guild_id, *role_ids, permission),
        )
        return row is not None

    async def has_any_permission(self, guild_id: int, role_ids: list[int]) -> bool:
        """True if any of these roles holds at least one grant (i.e. counts as staff-by-grant)."""
        if not role_ids:
            return False
        placeholders = ",".join("?" for _ in role_ids)
        row = await self._db.fetchone(
            f"SELECT id FROM permission_grants WHERE guild_id = ? AND role_id IN ({placeholders}) LIMIT 1",
            (guild_id, *role_ids),
        )
        return row is not None


class ScheduleRepository(BaseRepository):

    async def create(
        self,
        guild_id: int,
        channel_id: int,
        template_id: int,
        cron_expr: str,
        created_by: int,
        label: str | None = None,
        next_run_at: str | None = None,
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO scheduled_sends (guild_id, channel_id, template_id, cron_expr, created_by, label, next_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, template_id, cron_expr, created_by, label, next_run_at),
        )

    async def get(self, schedule_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM scheduled_sends WHERE id = ?", (schedule_id,)
        )
        return dict(row) if row else None

    async def list_for_guild(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT ss.*, mt.name AS template_name
            FROM scheduled_sends ss
            JOIN message_templates mt ON mt.id = ss.template_id
            WHERE ss.guild_id = ?
            ORDER BY ss.id
            """,
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def list_due(self, now_iso: str) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM scheduled_sends WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?",
            (now_iso,),
        )
        return [dict(r) for r in rows]

    async def set_next_run(self, schedule_id: int, next_run_at: str) -> None:
        await self._db.execute(
            "UPDATE scheduled_sends SET next_run_at = ?, last_run_at = CURRENT_TIMESTAMP WHERE id = ?",
            (next_run_at, schedule_id),
        )

    async def set_enabled(self, schedule_id: int, enabled: bool) -> bool:
        row = await self._db.fetchone(
            "SELECT id FROM scheduled_sends WHERE id = ?", (schedule_id,)
        )
        if not row:
            return False
        await self._db.execute(
            "UPDATE scheduled_sends SET enabled = ? WHERE id = ?",
            (int(enabled), schedule_id),
        )
        return True

    async def delete(self, guild_id: int, schedule_id: int) -> bool:
        row = await self._db.fetchone(
            "SELECT id FROM scheduled_sends WHERE id = ? AND guild_id = ?",
            (schedule_id, guild_id),
        )
        if not row:
            return False
        await self._db.execute("DELETE FROM scheduled_sends WHERE id = ?", (schedule_id,))
        return True


class AutoroleRepository(BaseRepository):

    async def add(
        self,
        guild_id: int,
        trigger_type: str,
        role_id: int,
        created_by: int,
        delay_seconds: int = 0,
        trigger_role_id: int | None = None,
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO autorole_rules (guild_id, trigger_type, trigger_role_id, role_id, delay_seconds, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, trigger_type, trigger_role_id, role_id)
            DO UPDATE SET delay_seconds = excluded.delay_seconds, enabled = 1
            """,
            (guild_id, trigger_type, trigger_role_id, role_id, delay_seconds, created_by),
        )

    async def remove(self, guild_id: int, rule_id: int) -> bool:
        row = await self._db.fetchone(
            "SELECT id FROM autorole_rules WHERE id = ? AND guild_id = ?",
            (rule_id, guild_id),
        )
        if not row:
            return False
        await self._db.execute("DELETE FROM autorole_rules WHERE id = ?", (rule_id,))
        return True

    async def list_for_guild(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM autorole_rules WHERE guild_id = ? ORDER BY trigger_type, id",
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def list_for_trigger(self, guild_id: int, trigger_type: str, trigger_role_id: int | None = None) -> list[dict]:
        if trigger_role_id is not None:
            rows = await self._db.fetchall(
                "SELECT * FROM autorole_rules WHERE guild_id = ? AND trigger_type = ? AND trigger_role_id = ? AND enabled = 1",
                (guild_id, trigger_type, trigger_role_id),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM autorole_rules WHERE guild_id = ? AND trigger_type = ? AND trigger_role_id IS NULL AND enabled = 1",
                (guild_id, trigger_type),
            )
        return [dict(r) for r in rows]
