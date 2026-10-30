import datetime
import json

from database.repositories.base import BaseRepository

# audit_log.created_at is stored the way SQLite's CURRENT_TIMESTAMP writes it:
# naive UTC "YYYY-MM-DD HH:MM:SS". Every filter value is formatted the same way
# so plain string comparison is chronological.
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_ts(value: datetime.datetime) -> str:
    """A datetime (aware = converted to UTC, naive = assumed UTC) as a created_at string."""
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value.strftime(_TS_FORMAT)


def utc_now_ts() -> str:
    return format_ts(datetime.datetime.now(datetime.timezone.utc))


class AuditRepository(BaseRepository):

    async def log(
        self,
        guild_id: int,
        actor_id: int,
        action: str,
        target_type: str = None,
        target_id: int = None,
        detail: dict = None,
        created_at: str | None = None,
    ) -> int:
        """Insert one row; returns its id. `created_at` defaults to now (UTC)."""
        detail_json = json.dumps(detail, default=str) if detail is not None else None
        return await self._db.execute_returning_id(
            """
            INSERT INTO audit_log (guild_id, actor_id, action, target_type, target_id, detail_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, actor_id, action, target_type, target_id, detail_json, created_at or utc_now_ts()),
        )

    async def get_recent(self, guild_id: int, limit: int = 50) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM audit_log WHERE guild_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (guild_id, limit),
        )
        return [dict(row) for row in rows]

    async def query(
        self,
        guild_id: int,
        *,
        action: str | None = None,
        actor_id: int | None = None,
        target_id: int | None = None,
        target_type: str | None = None,
        since: datetime.datetime | None = None,
        until: datetime.datetime | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Newest first, keyset-paginated on the autoincrement id.

        id order is insertion order, which is created_at order (every row is
        stamped "now" at insert), so `before_id` = the last id of the previous
        page is a stable cursor even while new rows keep arriving. `since` is
        inclusive, `until` exclusive.
        """
        where = ["guild_id = ?"]
        params: list = [guild_id]
        if action is not None:
            where.append("action = ?")
            params.append(action)
        if actor_id is not None:
            where.append("actor_id = ?")
            params.append(actor_id)
        if target_id is not None:
            where.append("target_id = ?")
            params.append(target_id)
        if target_type is not None:
            where.append("target_type = ?")
            params.append(target_type)
        if since is not None:
            where.append("created_at >= ?")
            params.append(format_ts(since))
        if until is not None:
            where.append("created_at < ?")
            params.append(format_ts(until))
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        params.append(limit)
        rows = await self._db.fetchall(
            f"SELECT * FROM audit_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            tuple(params),
        )
        return [dict(row) for row in rows]

    async def distinct_actions(self, guild_id: int) -> list[dict]:
        """Every action name seen in this guild with its row count, most used first."""
        rows = await self._db.fetchall(
            "SELECT action, COUNT(*) AS count FROM audit_log WHERE guild_id = ? "
            "GROUP BY action ORDER BY count DESC, action",
            (guild_id,),
        )
        return [{"action": r["action"], "count": r["count"]} for r in rows]

    async def prune_older_than(self, cutoff: datetime.datetime) -> int:
        """Delete rows (every guild) created before `cutoff`; returns how many."""
        return await self._db.execute("DELETE FROM audit_log WHERE created_at < ?", (format_ts(cutoff),))
