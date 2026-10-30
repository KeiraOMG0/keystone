from __future__ import annotations

from database.connection import Database
from database.repositories.base import BaseRepository


class QotdRepository(BaseRepository):
    def __init__(self, db: Database) -> None:
        super().__init__(db)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    async def get_config(self, guild_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM qotd_config WHERE guild_id = ?", (guild_id,)
        )
        return dict(row) if row else None

    async def upsert_config(
        self,
        guild_id: int,
        *,
        channel_id: int | None = None,
        post_hour: int | None = None,
        post_minute: int | None = None,
        enabled: bool | None = None,
        model: str | None = None,
        use_emojis: bool | None = None,
        ping_role_id: int | None = None,
        clear_ping_role: bool = False,
        staff_ping_role_id: int | None = None,
        clear_staff_ping_role: bool = False,
        approval_channel_id: int | None = None,
        clear_approval_channel: bool = False,
    ) -> None:
        existing = await self.get_config(guild_id)
        if existing is None:
            await self._db.execute(
                """
                INSERT INTO qotd_config (guild_id, channel_id, post_hour, post_minute, enabled, model, use_emojis, ping_role_id, staff_ping_role_id, approval_channel_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    guild_id,
                    channel_id,
                    post_hour if post_hour is not None else 9,
                    post_minute if post_minute is not None else 0,
                    1 if enabled is not False else 0,
                    model or "llama3.2:3b",
                    1 if use_emojis else 0,
                    ping_role_id,
                    staff_ping_role_id,
                    approval_channel_id,
                ),
            )
        else:
            fields: list[str] = []
            values: list = []
            if channel_id is not None:
                fields.append("channel_id = ?")
                values.append(channel_id)
            if post_hour is not None:
                fields.append("post_hour = ?")
                values.append(post_hour)
            if post_minute is not None:
                fields.append("post_minute = ?")
                values.append(post_minute)
            if enabled is not None:
                fields.append("enabled = ?")
                values.append(1 if enabled else 0)
            if model is not None:
                fields.append("model = ?")
                values.append(model)
            if use_emojis is not None:
                fields.append("use_emojis = ?")
                values.append(1 if use_emojis else 0)
            if ping_role_id is not None:
                fields.append("ping_role_id = ?")
                values.append(ping_role_id)
            elif clear_ping_role:
                fields.append("ping_role_id = ?")
                values.append(None)
            if staff_ping_role_id is not None:
                fields.append("staff_ping_role_id = ?")
                values.append(staff_ping_role_id)
            elif clear_staff_ping_role:
                fields.append("staff_ping_role_id = ?")
                values.append(None)
            if approval_channel_id is not None:
                fields.append("approval_channel_id = ?")
                values.append(approval_channel_id)
            elif clear_approval_channel:
                fields.append("approval_channel_id = ?")
                values.append(None)
            if not fields:
                return
            fields.append("updated_at = CURRENT_TIMESTAMP")
            values.append(guild_id)
            await self._db.execute(
                f"UPDATE qotd_config SET {', '.join(fields)} WHERE guild_id = ?",
                tuple(values),
            )

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    async def add_history(
        self,
        guild_id: int,
        question: str,
        source: str,
        message_id: int | None = None,
        channel_id: int | None = None,
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO qotd_history (guild_id, question, source, message_id, channel_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, question, source, message_id, channel_id),
        )

    async def get_recent_questions(self, guild_id: int, limit: int = 60) -> list[str]:
        rows = await self._db.fetchall(
            """
            SELECT question FROM qotd_history
            WHERE guild_id = ?
            ORDER BY posted_at DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [row["question"] for row in rows]

    async def get_last_posted_at(self, guild_id: int) -> str | None:
        row = await self._db.fetchone(
            "SELECT posted_at FROM qotd_history WHERE guild_id = ? ORDER BY posted_at DESC LIMIT 1",
            (guild_id,),
        )
        return row["posted_at"] if row else None

    async def approval_sent_between(self, guild_id: int, action: str, start: str, end: str) -> bool:
        """True if an audit_log row `action` (services.qotd_service.
        APPROVAL_SENT_ACTION) exists for the guild with start <= created_at
        < end. That row is the scheduler's persisted "today's approval
        request was already sent" dedupe key (no dedicated column exists;
        migrations are frozen for this change)."""
        row = await self._db.fetchone(
            "SELECT 1 FROM audit_log WHERE guild_id = ? AND action = ? AND created_at >= ? AND created_at < ? LIMIT 1",
            (guild_id, action, start, end),
        )
        return row is not None

    async def get_history(self, guild_id: int, limit: int = 10) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT id, question, source, posted_at, message_id, channel_id
            FROM qotd_history
            WHERE guild_id = ?
            ORDER BY posted_at DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [dict(r) for r in rows]
