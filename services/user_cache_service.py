"""
services/user_cache_service.py — remembered profiles of guild members,
including the ones who left, were kicked or were banned.

Why: the dashboard resolves user ids through the live guild (cache /
query_members / fetch_member). Anyone who is no longer in the server used to
come back as null, so moderation history, the audit log, ticket chat logs and
honeypot catches showed raw ids. This store keeps the last known name, avatar
and membership status of every member so internal_api/routes/resolve.py can
still render them after they are gone.

Its OWN SQLite file (config.USER_CACHE_PATH, default `user_cache.db`), like the
message log: NOT bot.db, no migrations; the schema is created with
IF NOT EXISTS and versioned with PRAGMA user_version. Deleting the file is
safe (the next start rebuilds it from the member list; departed users are
then only known again through fetch_user). The file is sensitive (who was
kicked/banned and when) and must never be added to the log rotator.

One row per user: user_id, username, global_name, nick, display_name, the
global and guild avatar hashes plus the display avatar URL at the time,
top-role colour, bot flag, joined_at, first_seen, last_seen (last time seen
as a member), updated_at (last profile refresh), the membership status
(member / left / kicked / banned / unbanned), status_changed_at, departed_at,
and for kicks/bans who did it (departed_by_id) and how (departure_source:
"moderator" = a person, "bot" = this bot on its own, "detected_at_startup" =
noticed missing after the bot was offline, so departed_at is approximate).
Never stored: reasons, messages, roles.

Writes are buffered (each event appends an op; flush() applies them in order
in one transaction; cogs/user_cache.py flushes every few seconds), so a busy
gateway costs one commit per flush. The startup member sweep writes in
chunks with executemany. Everything that touches the file runs in aiosqlite's
worker thread, never on the event loop.

Status precedence: banned > kicked > left. Discord sends the member-remove
and the ban for one ban in no fixed order, so a "left" arriving within
STATUS_PROTECT_SECONDS of a ban/kick does not overwrite it.

Retention: prune() deletes non-members whose departed_at (else updated_at)
is older than USER_CACHE_RETENTION_DAYS (0 = keep forever). Users who are
still banned are never pruned (the daily ban-list sweep would re-add them
anyway).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FLUSH_MAX_OPS = 500
SWEEP_CHUNK = 500
STATUS_PROTECT_SECONDS = 300
STATUSES = ("member", "left", "kicked", "banned", "unbanned")
DEPARTED_STATUSES = ("left", "kicked", "banned", "unbanned")
_RANK = {"member": 0, "unbanned": 0, "left": 1, "kicked": 2, "banned": 3}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id            INTEGER PRIMARY KEY,
    username           TEXT,
    global_name        TEXT,
    nick               TEXT,
    display_name       TEXT,
    avatar_hash        TEXT,               -- global avatar hash (cdn.discordapp.com/avatars/<id>/<hash>)
    guild_avatar_hash  TEXT,               -- per-server avatar hash, if any
    avatar_url         TEXT,               -- display avatar URL when last seen
    color              TEXT,               -- '#rrggbb' top coloured role, NULL = default
    is_bot             INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'member',  -- member | left | kicked | banned | unbanned
    status_changed_at  TEXT,
    joined_at          TEXT,
    first_seen         TEXT NOT NULL,
    last_seen          TEXT,               -- last time seen as a member
    updated_at         TEXT NOT NULL,      -- last profile refresh
    departed_at        TEXT,
    departed_by_id     INTEGER,            -- moderator who kicked/banned (never the reason)
    departure_source   TEXT                -- moderator | bot | detected_at_startup | NULL
);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status, departed_at);
"""

# A present member: full profile, status member, departure fields cleared.
_UPSERT_MEMBER = """
INSERT INTO users
    (user_id, username, global_name, nick, display_name, avatar_hash, guild_avatar_hash, avatar_url,
     color, is_bot, status, status_changed_at, joined_at, first_seen, last_seen, updated_at)
VALUES (:user_id, :username, :global_name, :nick, :display_name, :avatar_hash, :guild_avatar_hash, :avatar_url,
        :color, :is_bot, 'member', :now, :joined_at, :now, :now, :now)
ON CONFLICT(user_id) DO UPDATE SET
    username = excluded.username,
    global_name = excluded.global_name,
    nick = excluded.nick,
    display_name = excluded.display_name,
    avatar_hash = excluded.avatar_hash,
    guild_avatar_hash = excluded.guild_avatar_hash,
    avatar_url = excluded.avatar_url,
    color = excluded.color,
    is_bot = excluded.is_bot,
    status_changed_at = CASE WHEN users.status = 'member' THEN users.status_changed_at ELSE excluded.status_changed_at END,
    status = 'member',
    joined_at = COALESCE(excluded.joined_at, users.joined_at),
    last_seen = excluded.last_seen,
    updated_at = excluded.updated_at,
    departed_at = NULL,
    departed_by_id = NULL,
    departure_source = NULL
"""

# Profile of a Member object (e.g. the one in on_member_remove): every profile
# field, status untouched (insert uses :status).
_TOUCH_MEMBER_PROFILE = """
INSERT INTO users
    (user_id, username, global_name, nick, display_name, avatar_hash, guild_avatar_hash, avatar_url,
     color, is_bot, status, status_changed_at, joined_at, first_seen, updated_at)
VALUES (:user_id, :username, :global_name, :nick, :display_name, :avatar_hash, :guild_avatar_hash, :avatar_url,
        :color, :is_bot, :status, :now, :joined_at, :now, :now)
ON CONFLICT(user_id) DO UPDATE SET
    username = excluded.username,
    global_name = excluded.global_name,
    nick = excluded.nick,
    display_name = excluded.display_name,
    avatar_hash = excluded.avatar_hash,
    guild_avatar_hash = excluded.guild_avatar_hash,
    avatar_url = excluded.avatar_url,
    color = excluded.color,
    is_bot = excluded.is_bot,
    joined_at = COALESCE(excluded.joined_at, users.joined_at),
    updated_at = excluded.updated_at
"""

# Profile of a plain User (on_user_update, ban list, fetch_user): only the
# global fields; nick / guild avatar / colour / joined_at are kept.
_TOUCH_USER_PROFILE = """
INSERT INTO users
    (user_id, username, global_name, display_name, avatar_hash, avatar_url, is_bot,
     status, status_changed_at, first_seen, updated_at)
VALUES (:user_id, :username, :global_name, :display_name, :avatar_hash, :avatar_url, :is_bot,
        :status, :now, :now, :now)
ON CONFLICT(user_id) DO UPDATE SET
    username = excluded.username,
    global_name = excluded.global_name,
    display_name = CASE WHEN users.nick IS NULL THEN excluded.display_name ELSE users.display_name END,
    avatar_hash = excluded.avatar_hash,
    avatar_url = CASE WHEN users.guild_avatar_hash IS NULL THEN excluded.avatar_url ELSE users.avatar_url END,
    is_bot = excluded.is_bot,
    updated_at = excluded.updated_at
"""


def _ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def default_avatar_url(user_id: int) -> str:
    """Discord's default avatar for a (new-username) account."""
    return f"https://cdn.discordapp.com/embed/avatars/{(int(user_id) >> 22) % 6}.png"


def _asset_key(asset: Any) -> str | None:
    return getattr(asset, "key", None) if asset is not None else None


def _asset_url(asset: Any) -> str | None:
    return getattr(asset, "url", None) if asset is not None else None


def is_member_object(obj: Any) -> bool:
    """discord.Member has joined_at + guild; discord.User has neither."""
    return hasattr(obj, "joined_at") and hasattr(obj, "guild")


def profile_from(obj: Any) -> dict[str, Any]:
    """The profile columns for a discord.Member or discord.User (duck-typed)."""
    member = is_member_object(obj)
    name = str(getattr(obj, "name", None) or obj.id)
    global_name = getattr(obj, "global_name", None)
    nick = getattr(obj, "nick", None) if member else None
    display = getattr(obj, "display_name", None) or global_name or name
    colour = getattr(obj, "color", None) if member else None
    colour_value = getattr(colour, "value", 0) if colour is not None else 0
    joined = getattr(obj, "joined_at", None) if member else None
    return {
        "user_id": int(obj.id),
        "username": name,
        "global_name": global_name,
        "nick": nick,
        "display_name": str(display),
        "avatar_hash": _asset_key(getattr(obj, "avatar", None)),
        "guild_avatar_hash": _asset_key(getattr(obj, "guild_avatar", None)) if member else None,
        "avatar_url": _asset_url(getattr(obj, "display_avatar", None)) or default_avatar_url(obj.id),
        "color": f"#{colour_value:06x}" if colour_value else None,
        "is_bot": int(bool(getattr(obj, "bot", False))),
        "joined_at": _ts(joined) if isinstance(joined, datetime) else None,
    }


class UserCacheStore:
    def __init__(self, path: str, retention_days: int) -> None:
        self.path = path
        self.retention_days = retention_days
        self._conn: aiosqlite.Connection | None = None
        self._ops: list[tuple] = []
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(_SCHEMA)
        await self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is None:
            return
        try:
            await self.flush()
        finally:
            await self._conn.close()
            self._conn = None

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    # -- buffered writes (never raise, never await) ------------------------
    def _push(self, op: tuple) -> None:
        # Coalesce: a profile write right after one of the same kind for the
        # same user replaces it (member_update bursts cost one row write).
        if op[0] in ("member", "member_profile", "user_profile") and self._ops:
            last = self._ops[-1]
            if last[0] == op[0] and last[1]["user_id"] == op[1]["user_id"]:
                self._ops[-1] = op
                return
        self._ops.append(op)

    def upsert_member(self, member: Any, at: datetime | None = None) -> None:
        """A current member (join, member update)."""
        self._push(("member", {**profile_from(member), "now": _ts(at or _now())}))

    def touch_profile(self, obj: Any, *, insert_status: str = "left", at: datetime | None = None) -> None:
        """Refresh the profile without changing the membership status (a
        brand-new row gets `insert_status`)."""
        kind = "member_profile" if is_member_object(obj) else "user_profile"
        self._push((kind, {**profile_from(obj), "status": insert_status, "now": _ts(at or _now())}))

    def departed(self, user_id: int, status: str, at: datetime | None = None) -> None:
        """left / kicked / banned. Clears any older attribution."""
        if status not in ("left", "kicked", "banned"):
            raise ValueError(f"bad departure status {status!r}")
        self._ops.append(("depart", int(user_id), status, at or _now()))

    def attribute(self, user_id: int, status: str, moderator_id: int | None, source: str | None) -> None:
        """Upgrade a fresh 'left' to 'kicked' (or annotate a ban) with who did it."""
        if status not in ("kicked", "banned"):
            raise ValueError(f"bad attribution status {status!r}")
        self._ops.append(("attribute", int(user_id), status, moderator_id, source))

    def unbanned(self, user_id: int, at: datetime | None = None) -> None:
        self._ops.append(("unban", int(user_id), at or _now()))

    def pending(self) -> int:
        return len(self._ops)

    async def flush(self) -> int:
        async with self._lock:
            return await self._flush_locked()

    async def _flush_locked(self) -> int:
        if self._conn is None or not self._ops:
            return 0
        ops, self._ops = self._ops, []
        try:
            for op in ops:
                await self._apply(op)
            await self._conn.commit()
        except Exception:
            log.exception("user cache: flush of %d op(s) failed; dropped", len(ops))
            try:
                await self._conn.rollback()
            except Exception:
                pass
            return 0
        return len(ops)

    async def _apply(self, op: tuple) -> None:
        conn = self._conn
        assert conn is not None
        kind = op[0]
        if kind == "member":
            await conn.execute(_UPSERT_MEMBER, op[1])
        elif kind == "member_profile":
            await conn.execute(_TOUCH_MEMBER_PROFILE, op[1])
        elif kind == "user_profile":
            row = {k: op[1][k] for k in (
                "user_id", "username", "global_name", "display_name", "avatar_hash", "avatar_url", "is_bot",
                "status", "now",
            )}
            await conn.execute(_TOUCH_USER_PROFILE, row)
        elif kind == "depart":
            _, user_id, status, at = op
            stamp = _ts(at)
            async with conn.execute("SELECT status, departed_at FROM users WHERE user_id = ?", (user_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                await conn.execute(
                    "INSERT INTO users (user_id, status, status_changed_at, first_seen, updated_at, departed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, status, stamp, stamp, stamp, stamp),
                )
                return
            existing_at = _parse(row["departed_at"])
            if (
                _RANK.get(row["status"], 0) > _RANK[status]
                and existing_at is not None
                and abs((at - existing_at).total_seconds()) <= STATUS_PROTECT_SECONDS
            ):
                return  # e.g. the member-remove of a ban arriving after the ban
            await conn.execute(
                "UPDATE users SET status = ?, status_changed_at = ?, departed_at = ?, "
                "departed_by_id = NULL, departure_source = NULL WHERE user_id = ?",
                (status, stamp, stamp, user_id),
            )
        elif kind == "attribute":
            _, user_id, status, moderator_id, source = op
            allowed = ("left", "kicked") if status == "kicked" else ("banned",)
            marks = ",".join("?" * len(allowed))
            await conn.execute(
                f"UPDATE users SET status = ?, departed_by_id = ?, departure_source = ? "
                f"WHERE user_id = ? AND status IN ({marks})",
                (status, moderator_id, source, user_id, *allowed),
            )
        elif kind == "unban":
            _, user_id, at = op
            await conn.execute(
                "UPDATE users SET status = 'unbanned', status_changed_at = ? WHERE user_id = ? AND status = 'banned'",
                (_ts(at), user_id),
            )

    # -- bulk sweeps -------------------------------------------------------
    async def sweep_members(self, members: Iterable[Any], *, complete: bool, at: datetime | None = None) -> dict:
        """Upsert every current member in chunks. When `complete` (the guild
        member list is fully chunked), rows still marked 'member' that were
        not in the list are marked left (they left while the bot was off)."""
        if self._conn is None:
            return {"members": 0, "marked_left": 0}
        stamp = _ts(at or _now())
        rows: list[dict] = []
        for member in members:
            try:
                rows.append({**profile_from(member), "now": stamp})
            except Exception:
                log.debug("user cache: could not read member %r", getattr(member, "id", None), exc_info=True)
            if len(rows) % SWEEP_CHUNK == 0:
                await asyncio.sleep(0)  # building rows for a big guild must not hog the loop
        marked = 0
        async with self._lock:
            await self._flush_locked()
            try:
                for start in range(0, len(rows), SWEEP_CHUNK):
                    await self._conn.executemany(_UPSERT_MEMBER, rows[start:start + SWEEP_CHUNK])
                    await self._conn.commit()
                if complete:
                    await self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS present (user_id INTEGER PRIMARY KEY)")
                    await self._conn.execute("DELETE FROM temp.present")
                    await self._conn.executemany(
                        "INSERT OR IGNORE INTO temp.present (user_id) VALUES (?)", [(r["user_id"],) for r in rows],
                    )
                    cur = await self._conn.execute(
                        "UPDATE users SET status = 'left', status_changed_at = ?, departed_at = ?, "
                        "departed_by_id = NULL, departure_source = 'detected_at_startup' "
                        "WHERE status = 'member' AND user_id NOT IN (SELECT user_id FROM temp.present)",
                        (stamp, stamp),
                    )
                    marked = cur.rowcount or 0
                    await self._conn.execute("DELETE FROM temp.present")
                    await self._conn.commit()
            except Exception:
                log.exception("user cache: member sweep failed")
                try:
                    await self._conn.rollback()
                except Exception:
                    pass
        return {"members": len(rows), "marked_left": marked}

    async def sweep_bans(self, users: Iterable[Any], *, complete: bool, at: datetime | None = None) -> dict:
        """The guild ban list: banned users get a row (or their status set to
        banned); with `complete`, rows marked banned that are no longer on the
        list become 'unbanned' (unbanned while the bot was off)."""
        if self._conn is None:
            return {"banned": 0, "marked_unbanned": 0}
        stamp = _ts(at or _now())
        rows = []
        for user in users:
            try:
                p = profile_from(user)
            except Exception:
                continue
            rows.append({k: p[k] for k in (
                "user_id", "username", "global_name", "display_name", "avatar_hash", "avatar_url", "is_bot",
            )} | {"status": "banned", "now": stamp})
        unbanned = 0
        async with self._lock:
            await self._flush_locked()
            try:
                for start in range(0, len(rows), SWEEP_CHUNK):
                    chunk = rows[start:start + SWEEP_CHUNK]
                    await self._conn.executemany(_TOUCH_USER_PROFILE, chunk)
                    await self._conn.executemany(
                        "UPDATE users SET status = 'banned', status_changed_at = :now "
                        "WHERE user_id = :user_id AND status != 'banned'",
                        chunk,
                    )
                    await self._conn.commit()
                if complete:
                    await self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS banned_now (user_id INTEGER PRIMARY KEY)")
                    await self._conn.execute("DELETE FROM temp.banned_now")
                    await self._conn.executemany(
                        "INSERT OR IGNORE INTO temp.banned_now (user_id) VALUES (?)", [(r["user_id"],) for r in rows],
                    )
                    cur = await self._conn.execute(
                        "UPDATE users SET status = 'unbanned', status_changed_at = ? "
                        "WHERE status = 'banned' AND user_id NOT IN (SELECT user_id FROM temp.banned_now)",
                        (stamp,),
                    )
                    unbanned = cur.rowcount or 0
                    await self._conn.execute("DELETE FROM temp.banned_now")
                    await self._conn.commit()
            except Exception:
                log.exception("user cache: ban sweep failed")
                try:
                    await self._conn.rollback()
                except Exception:
                    pass
        return {"banned": len(rows), "marked_unbanned": unbanned}

    # -- retention / forget ------------------------------------------------
    async def prune(self, now: datetime | None = None) -> int:
        """Delete departed (not banned) users past the retention. 0 days = never."""
        if self._conn is None or self.retention_days <= 0:
            return 0
        await self.flush()
        cutoff = _ts((now or _now()) - timedelta(days=self.retention_days))
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM users WHERE status NOT IN ('member', 'banned') "
                "AND COALESCE(departed_at, updated_at) < ?",
                (cutoff,),
            )
            removed = cur.rowcount or 0
            await self._conn.commit()
            if removed:
                await self._conn.execute("VACUUM")
        return removed

    async def forget(self, user_id: int) -> bool:
        """Delete one user's row now (and any write still waiting for them).
        A current member is re-added by the next event or sweep."""
        if self._conn is None:
            return False
        user_id = int(user_id)
        async with self._lock:
            self._ops = [op for op in self._ops if _op_user(op) != user_id]
            cur = await self._conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            await self._conn.commit()
            return (cur.rowcount or 0) > 0

    # -- reads -------------------------------------------------------------
    async def get_many(self, user_ids: Iterable[int]) -> dict[int, dict]:
        if self._conn is None:
            return {}
        ids = [int(i) for i in dict.fromkeys(user_ids)]
        if not ids:
            return {}
        await self.flush()
        out: dict[int, dict] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            async with self._conn.execute(f"SELECT * FROM users WHERE user_id IN ({marks})", chunk) as cur:
                for row in await cur.fetchall():
                    out[row["user_id"]] = dict(row)
        return out

    async def get(self, user_id: int) -> dict | None:
        return (await self.get_many([user_id])).get(int(user_id))

    async def stats(self) -> dict:
        if self._conn is None:
            return {"total": 0, "by_status": {}}
        await self.flush()
        async with self._conn.execute("SELECT status, COUNT(*) AS n FROM users GROUP BY status") as cur:
            by_status = {r["status"]: r["n"] for r in await cur.fetchall()}
        return {"total": sum(by_status.values()), "by_status": by_status}


def _op_user(op: tuple) -> int | None:
    if op[0] in ("member", "member_profile", "user_profile"):
        return op[1]["user_id"]
    return op[1]


def _iso(value: str | None) -> str | None:
    """Stored '2026-10-28T12:00:00Z' is already ISO-8601 UTC."""
    return value or None


def serialize_departed(row: dict) -> dict:
    """The resolve-route entry for a user who is NOT in the guild right now.

    `status` is the stored membership status, except a row still marked
    'member' (the bot has not processed the departure yet) answers 'left'.
    """
    status = row.get("status") or "left"
    if status == "member":
        status = "left"
    by = row.get("departed_by_id")
    username = row.get("username") or str(row["user_id"])
    return {
        "id": str(row["user_id"]),
        "username": username,
        "display_name": row.get("display_name") or row.get("global_name") or username,
        "avatar_url": row.get("avatar_url") or default_avatar_url(row["user_id"]),
        "departed": True,
        "status": status,
        "departed_at": _iso(row.get("departed_at")),
        "status_changed_at": _iso(row.get("status_changed_at")),
        "departed_by_id": str(by) if by else None,
        "departure_source": row.get("departure_source"),
        "global_name": row.get("global_name"),
        "nick": row.get("nick"),
        "color": row.get("color"),
        "bot": bool(row.get("is_bot")),
        "joined_at": _iso(row.get("joined_at")),
        "first_seen": _iso(row.get("first_seen")),
        "last_seen": _iso(row.get("last_seen")),
        "cached_at": _iso(row.get("updated_at")),
    }


def needs_refresh(row: dict | None, now: datetime, max_age: timedelta) -> bool:
    """True when a departed user's profile is missing or older than max_age."""
    if row is None or not row.get("username"):
        return True
    updated = _parse(row.get("updated_at"))
    return updated is None or now - updated > max_age


# The running store (set by cogs/user_cache.py), read by the resolve route.
_ACTIVE: UserCacheStore | None = None


def set_active(store: UserCacheStore | None) -> None:
    global _ACTIVE
    _ACTIVE = store


def get_active() -> UserCacheStore | None:
    return _ACTIVE if _ACTIVE is not None and _ACTIVE.is_open else None
