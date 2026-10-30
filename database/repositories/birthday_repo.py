# SPDX-License-Identifier: AGPL-3.0-only
# Python port of BirthdayBot by NoiTheCat (https://github.com/NoiTheCat/BirthdayBot),
# licensed under the GNU AGPL v3.0. See CREDITS.md and LICENSES/AGPL-3.0.txt.
from datetime import datetime, timezone
from database.repositories.base import BaseRepository


class BirthdayRepository(BaseRepository):

    # ------------------------------------------------------------------ #
    # Settings                                                             #
    # ------------------------------------------------------------------ #

    async def get_settings(self, guild_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM birthday_settings WHERE guild_id = ?", (guild_id,)
        )
        return dict(row) if row else None

    async def get_or_create_settings(self, guild_id: int) -> dict:
        existing = await self.get_settings(guild_id)
        if existing:
            return existing
        await self._db.execute(
            "INSERT OR IGNORE INTO birthday_settings (guild_id) VALUES (?)", (guild_id,)
        )
        return await self.get_settings(guild_id)

    async def update_settings(self, guild_id: int, **kwargs):
        if not kwargs:
            return
        kwargs["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        set_clause = ", ".join(f"{k} = ?" for k in kwargs)
        await self._db.execute(
            f"UPDATE birthday_settings SET {set_clause} WHERE guild_id = ?",
            (*kwargs.values(), guild_id),
        )

    # ------------------------------------------------------------------ #
    # User birthdays                                                       #
    # ------------------------------------------------------------------ #

    async def set_birthday(
        self,
        guild_id: int,
        user_id: int,
        month: int,
        day: int,
        user_timezone: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            """
            INSERT INTO birthdays (guild_id, user_id, birth_month, birth_day, user_timezone, last_celebrated, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                birth_month     = excluded.birth_month,
                birth_day       = excluded.birth_day,
                user_timezone   = COALESCE(excluded.user_timezone, user_timezone),
                last_celebrated = 0,
                updated_at      = excluded.updated_at
            """,
            (guild_id, user_id, month, day, user_timezone, now, now),
        )

    async def set_timezone(self, guild_id: int, user_id: int, tz: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM birthdays WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        if not row:
            return False
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            "UPDATE birthdays SET user_timezone = ?, updated_at = ? WHERE guild_id = ? AND user_id = ?",
            (tz, now, guild_id, user_id),
        )
        return True

    async def clear_timezone(self, guild_id: int, user_id: int) -> bool:
        """Set user_timezone to NULL. False if there is no row or it was already unset."""
        row = await self._db.fetchone(
            "SELECT user_timezone FROM birthdays WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        if not row or row["user_timezone"] is None:
            return False
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            "UPDATE birthdays SET user_timezone = NULL, updated_at = ? WHERE guild_id = ? AND user_id = ?",
            (now, guild_id, user_id),
        )
        return True

    async def remove_birthday(self, guild_id: int, user_id: int) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM birthdays WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        if not row:
            return False
        await self._db.execute(
            "DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        return True

    async def get_birthday(self, guild_id: int, user_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM birthdays WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        return dict(row) if row else None

    async def get_all_for_guild(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM birthdays WHERE guild_id = ? ORDER BY birth_month, birth_day",
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def get_by_month_day(self, guild_id: int, month: int, day: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM birthdays WHERE guild_id = ? AND birth_month = ? AND birth_day = ?",
            (guild_id, month, day),
        )
        return [dict(r) for r in rows]

    async def mark_celebrated(self, guild_id: int, user_id: int, year: int) -> None:
        await self._db.execute(
            "UPDATE birthdays SET last_celebrated = ? WHERE guild_id = ? AND user_id = ?",
            (year, guild_id, user_id),
        )

    async def reset_last_celebrated(self, guild_id: int) -> None:
        await self._db.execute(
            "UPDATE birthdays SET last_celebrated = 0 WHERE guild_id = ?",
            (guild_id,),
        )

    async def reset_last_celebrated_for_date(self, guild_id: int, month: int, day: int) -> None:
        await self._db.execute(
            "UPDATE birthdays SET last_celebrated = 0 WHERE guild_id = ? AND birth_month = ? AND birth_day = ?",
            (guild_id, month, day),
        )

    async def get_upcoming(
        self, guild_id: int, days: int = 14, *, targets: list[tuple[int, int]] | None = None
    ) -> list[dict]:
        """Returns entries whose (month, day) is in `targets`. Callers pass
        services.birthday_service.upcoming_month_days(...) (which also covers
        yesterday for UTC- zones and Feb 29 in non-leap years); without it,
        falls back to the next `days` days (UTC)."""
        if not targets:
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            targets = []
            for offset in range(days + 1):
                d = now + timedelta(days=offset)
                targets.append((d.month, d.day))

        # SQLite doesn't support row-value IN, so build individual OR clauses
        clauses = " OR ".join("(birth_month = ? AND birth_day = ?)" for _ in targets)
        flat_params = [v for pair in targets for v in pair]

        rows = await self._db.fetchall(
            f"""
            SELECT * FROM birthdays
            WHERE guild_id = ? AND ({clauses})
            ORDER BY birth_month, birth_day
            """,
            (guild_id, *flat_params),
        )
        return [dict(r) for r in rows]

    async def import_bulk(self, guild_id: int, entries: list[dict]) -> int:
        """
        Bulk-import birthday entries.
        Each dict must have: user_id, birth_month, birth_day.
        Optionally: user_timezone.
        Returns the number of rows inserted/replaced.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        count = 0
        for e in entries:
            await self._db.execute(
                """
                INSERT INTO birthdays (guild_id, user_id, birth_month, birth_day, user_timezone, last_celebrated, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    birth_month   = excluded.birth_month,
                    birth_day     = excluded.birth_day,
                    user_timezone = COALESCE(excluded.user_timezone, user_timezone),
                    last_celebrated = CASE
                        WHEN birth_month != excluded.birth_month OR birth_day != excluded.birth_day
                        THEN 0
                        ELSE last_celebrated
                    END,
                    updated_at    = excluded.updated_at
                """,
                (guild_id, e["user_id"], e["birth_month"], e["birth_day"],
                 e.get("user_timezone"), now, now),
            )
            count += 1
        return count
