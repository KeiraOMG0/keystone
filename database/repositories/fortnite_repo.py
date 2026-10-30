"""
database/repositories/fortnite_repo.py — raw SQL for the Fortnite item shop
feature (migration 027: fortnite_shop_config, fortnite_wishlist).
"""

from __future__ import annotations

from database.connection import Database
from database.repositories.base import BaseRepository

_BOOL_FIELDS = ("enabled", "include_jam_tracks")
# Fields the config editors (slash commands / dashboard) may write.
EDITABLE_FIELDS = frozenset({"channel_id", "enabled", "include_jam_tracks", "ping_role_id"})
# Fields only the auto-post loop writes (dedupe + status).
_STATUS_FIELDS = frozenset(
    {"last_posted_date", "last_posted_hash", "last_posted_at", "last_message_id", "last_error", "last_error_at"}
)

MAX_WISHLIST_PER_USER = 25


def normalise_query(query: str) -> str:
    return " ".join((query or "").split()).lower()


def _row_to_config(row) -> dict:
    cfg = dict(row)
    for key in _BOOL_FIELDS:
        cfg[key] = bool(cfg.get(key))
    return cfg


class FortniteRepository(BaseRepository):
    def __init__(self, db: Database) -> None:
        super().__init__(db)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    async def get_config(self, guild_id: int) -> dict | None:
        row = await self._db.fetchone("SELECT * FROM fortnite_shop_config WHERE guild_id = ?", (guild_id,))
        return _row_to_config(row) if row else None

    async def all_configs(self) -> list[dict]:
        rows = await self._db.fetchall("SELECT * FROM fortnite_shop_config")
        return [_row_to_config(r) for r in rows]

    async def _ensure_row(self, guild_id: int) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO fortnite_shop_config (guild_id) VALUES (?)", (guild_id,)
        )

    async def update_config(self, guild_id: int, **changes) -> dict:
        """Partial update. A value of None clears that column (e.g. ping_role_id)."""
        unknown = set(changes) - EDITABLE_FIELDS - _STATUS_FIELDS
        if unknown:
            raise ValueError(f"unknown fortnite_shop_config field(s): {sorted(unknown)}")
        await self._ensure_row(guild_id)
        if changes:
            cols, vals = [], []
            for key, value in changes.items():
                cols.append(f"{key} = ?")
                vals.append((1 if value else 0) if key in _BOOL_FIELDS else value)
            cols.append("updated_at = CURRENT_TIMESTAMP")
            vals.append(guild_id)
            await self._db.execute(
                f"UPDATE fortnite_shop_config SET {', '.join(cols)} WHERE guild_id = ?", tuple(vals)
            )
        cfg = await self.get_config(guild_id)
        assert cfg is not None
        return cfg

    # ------------------------------------------------------------------
    # Wishlist
    # ------------------------------------------------------------------
    async def add_wish(self, guild_id: int, user_id: int, query: str) -> bool:
        """True if added, False if it already existed. Raises ValueError when
        the user is at MAX_WISHLIST_PER_USER or the query is empty."""
        q = normalise_query(query)
        if not q:
            raise ValueError("empty")
        count = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM fortnite_wishlist WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        if count and count["n"] >= MAX_WISHLIST_PER_USER:
            raise ValueError("limit")
        changed = await self._db.execute(
            "INSERT OR IGNORE INTO fortnite_wishlist (guild_id, user_id, query) VALUES (?, ?, ?)",
            (guild_id, user_id, q),
        )
        return changed > 0

    async def remove_wish(self, guild_id: int, user_id: int, query: str) -> bool:
        changed = await self._db.execute(
            "DELETE FROM fortnite_wishlist WHERE guild_id = ? AND user_id = ? AND query = ?",
            (guild_id, user_id, normalise_query(query)),
        )
        return changed > 0

    async def list_wishes(self, guild_id: int, user_id: int | None = None) -> list[dict]:
        if user_id is None:
            rows = await self._db.fetchall(
                "SELECT * FROM fortnite_wishlist WHERE guild_id = ? ORDER BY user_id, query", (guild_id,)
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM fortnite_wishlist WHERE guild_id = ? AND user_id = ? ORDER BY query",
                (guild_id, user_id),
            )
        return [dict(r) for r in rows]

    async def count_wishes(self, guild_id: int) -> dict:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS entries, COUNT(DISTINCT user_id) AS users FROM fortnite_wishlist WHERE guild_id = ?",
            (guild_id,),
        )
        return {"entries": row["entries"] if row else 0, "users": row["users"] if row else 0}

    async def mark_notified(self, wish_ids: list[int], shop_date: str) -> None:
        for wid in wish_ids:
            await self._db.execute(
                "UPDATE fortnite_wishlist SET last_notified_date = ? WHERE id = ?", (shop_date, wid)
            )
