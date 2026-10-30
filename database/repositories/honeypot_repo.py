"""
database/repositories/honeypot_repo.py — honeypot config, catch events and appeals.

Pure storage: no Discord objects, no business rules (those live in
services/honeypot_service.py). Booleans are stored as 0/1 and returned as
bool; `errors` is stored as a JSON array and returned as a list.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from database.connection import Database
from database.repositories.base import BaseRepository

_CONFIG_FIELDS = (
    "enabled", "channel_id", "category_id", "action", "dm_before", "appeal_enabled",
    "delete_message_days", "log_channel_id", "warning_message_id", "stats_message_id",
    "scramble_enabled", "scramble_interval_hours", "last_scrambled_at",  # migration 029
    "rejoin_invite_url", "invite_in_removal_dm",  # migration 030
)
_CONFIG_BOOLS = ("enabled", "dm_before", "appeal_enabled", "scramble_enabled", "invite_in_removal_dm")
TOKEN_HASH_PREFIX = "sha256:"  # honeypot_appeals.token holds this + hex, never the token (migration 030)

_EVENT_BOOLS = (
    "dm_sent", "had_default_avatar", "flag_link", "flag_invite", "flag_everyone", "flag_attachment",
)

APPEAL_STATUSES = ("invited", "pending", "approved", "denied")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_token(token: str) -> str:
    """Stored form of an appeal-link token. The token is 192 random bits
    (secrets.token_urlsafe(24)), so plain SHA-256 is enough: there is nothing to
    brute-force, unlike a password. A leaked bot.db then holds no usable link."""
    return TOKEN_HASH_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


class HoneypotRepository(BaseRepository):
    def __init__(self, db: Database) -> None:
        super().__init__(db)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    async def get_config(self, guild_id: int) -> dict | None:
        row = await self._db.fetchone("SELECT * FROM honeypot_config WHERE guild_id = ?", (guild_id,))
        if row is None:
            return None
        cfg = dict(row)
        for key in _CONFIG_BOOLS:
            cfg[key] = bool(cfg[key])
        return cfg

    async def upsert_config(self, guild_id: int, **fields) -> dict:
        """Creates the row (with column defaults) if missing, then applies `fields`.
        Unknown field names raise ValueError so typos never silently no-op."""
        unknown = set(fields) - set(_CONFIG_FIELDS)
        if unknown:
            raise ValueError(f"Unknown honeypot_config field(s): {sorted(unknown)}")

        await self._db.execute(
            "INSERT OR IGNORE INTO honeypot_config (guild_id, updated_at) VALUES (?, ?)",
            (guild_id, _now()),
        )
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            values = tuple(int(v) if isinstance(v, bool) else v for v in fields.values())
            await self._db.execute(
                f"UPDATE honeypot_config SET {assignments}, updated_at = ? WHERE guild_id = ?",
                (*values, _now(), guild_id),
            )
        cfg = await self.get_config(guild_id)
        assert cfg is not None
        return cfg

    # ------------------------------------------------------------------
    # Events (catches)
    # ------------------------------------------------------------------
    async def add_event(
        self,
        guild_id: int,
        user_id: int,
        action: str,
        *,
        username: str | None = None,
        display_name: str | None = None,
        avatar_url: str | None = None,
        channel_id: int | None = None,
        content: str | None = None,
        action_result: str = "ok",
        errors: list[str] | None = None,
        dm_sent: bool = False,
        account_created_at: str | None = None,
        joined_at: str | None = None,
        seconds_since_join: int | None = None,
        had_default_avatar: bool = False,
        flag_link: bool = False,
        flag_invite: bool = False,
        flag_everyone: bool = False,
        flag_attachment: bool = False,
        created_at: str | None = None,
        via_app_id: int | None = None,
        via_app_name: str | None = None,
        is_test: bool = False,
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO honeypot_events
                (guild_id, user_id, username, display_name, avatar_url, channel_id, content,
                 action, action_result, errors, dm_sent, account_created_at, joined_at,
                 seconds_since_join, had_default_avatar, flag_link, flag_invite, flag_everyone,
                 flag_attachment, created_at, via_app_id, via_app_name, is_test)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id, user_id, username, display_name, avatar_url, channel_id, content,
                action, action_result, json.dumps(errors or []), int(dm_sent), account_created_at,
                joined_at, seconds_since_join, int(had_default_avatar), int(flag_link),
                int(flag_invite), int(flag_everyone), int(flag_attachment), created_at or _now(),
                via_app_id, via_app_name, int(is_test),
            ),
        )

    async def append_event_error(self, event_id: int, error: str) -> None:
        row = await self._db.fetchone("SELECT errors FROM honeypot_events WHERE id = ?", (event_id,))
        if row is None:
            return
        errors = _load_errors(row["errors"])
        errors.append(error)
        await self._db.execute(
            "UPDATE honeypot_events SET errors = ? WHERE id = ?", (json.dumps(errors), event_id),
        )

    async def get_event(self, event_id: int) -> dict | None:
        row = await self._db.fetchone("SELECT * FROM honeypot_events WHERE id = ?", (event_id,))
        return _event_row(row) if row else None

    async def list_events(self, guild_id: int, limit: int = 50, before_id: int | None = None) -> list[dict]:
        """Newest first, real catches only (no Debug-page test cases). `appeal_status` is the
        status of the user's latest appeal (or None)."""
        params: list = [guild_id]
        where = "e.guild_id = ? AND e.is_test = 0"
        if before_id is not None:
            where += " AND e.id < ?"
            params.append(before_id)
        params.append(limit)
        rows = await self._db.fetchall(
            f"""
            SELECT e.*,
                   (SELECT a.status FROM honeypot_appeals a
                     WHERE a.guild_id = e.guild_id AND a.user_id = e.user_id
                     ORDER BY a.id DESC LIMIT 1) AS appeal_status
            FROM honeypot_events e
            WHERE {where}
            ORDER BY e.id DESC
            LIMIT ?
            """,
            tuple(params),
        )
        return [_event_row(r) for r in rows]

    async def get_all_events_for_stats(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT id, user_id, action, action_result, content, dm_sent, account_created_at,
                   seconds_since_join, had_default_avatar, flag_link, flag_invite,
                   flag_everyone, flag_attachment, created_at
            FROM honeypot_events WHERE guild_id = ? AND is_test = 0
            ORDER BY id ASC
            """,
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def count_events(self, guild_id: int) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS n FROM honeypot_events WHERE guild_id = ? AND is_test = 0", (guild_id,),
        )
        return int(row["n"])

    async def get_last_event(self, guild_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM honeypot_events WHERE guild_id = ? AND is_test = 0 ORDER BY id DESC LIMIT 1", (guild_id,),
        )
        return _event_row(row) if row else None

    async def get_latest_ban_event_for_user(self, guild_id: int, user_id: int) -> dict | None:
        row = await self._db.fetchone(
            """
            SELECT * FROM honeypot_events
            WHERE guild_id = ? AND user_id = ? AND action = 'ban'
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, user_id),
        )
        return _event_row(row) if row else None

    async def list_events_for_user(self, guild_id: int, user_id: int, limit: int = 20) -> list[dict]:
        """One user's real catches (no Debug-page test cases), newest first.
        Feeds the dashboard member profile (services/member_profile_service.py)."""
        rows = await self._db.fetchall(
            """
            SELECT * FROM honeypot_events
            WHERE guild_id = ? AND user_id = ? AND is_test = 0
            ORDER BY id DESC LIMIT ?
            """,
            (guild_id, user_id, limit),
        )
        return [_event_row(r) for r in rows]

    async def get_latest_real_appeal_for_user(self, guild_id: int, user_id: int) -> dict | None:
        """Like get_latest_appeal_for_user, but never a Debug-page test case
        (for display on the member profile)."""
        row = await self._db.fetchone(
            """
            SELECT * FROM honeypot_appeals
            WHERE guild_id = ? AND user_id = ? AND is_test = 0
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, user_id),
        )
        return dict(row) if row else None

    async def has_failed_event_since(self, guild_id: int, user_id: int, since_iso: str) -> bool:
        """True if this user already has a catch whose action failed at/after `since_iso`."""
        row = await self._db.fetchone(
            """
            SELECT 1 AS x FROM honeypot_events
            WHERE guild_id = ? AND user_id = ? AND action_result = 'failed' AND created_at >= ?
            LIMIT 1
            """,
            (guild_id, user_id, since_iso),
        )
        return row is not None

    # ------------------------------------------------------------------
    # Appeals
    # ------------------------------------------------------------------
    async def create_appeal(
        self,
        guild_id: int,
        user_id: int,
        token: str,
        *,
        username: str | None = None,
        status: str = "invited",
        catch_id: int | None = None,
        name: str | None = None,
        message: str | None = None,
        submitted_at: str | None = None,
        verified: bool = True,
        verified_via: str | None = "dm_link",
        is_test: bool = False,
    ) -> int:
        """`token` is the raw appeal-link token; only its hash is stored (hash_token)."""
        if status not in APPEAL_STATUSES:
            raise ValueError(f"Invalid appeal status {status!r}")
        return await self._db.execute_returning_id(
            """
            INSERT INTO honeypot_appeals
                (guild_id, user_id, token, username, name, message, status, catch_id,
                 created_at, submitted_at, verified, verified_via, is_test)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, hash_token(token), username, name, message, status, catch_id, _now(), submitted_at,
             int(verified), verified_via if verified else None, int(is_test)),
        )

    # Debug-page test cases (migration 031) ----------------------------
    async def has_real_records(self, guild_id: int, user_id: int) -> bool:
        """True if this user has any real (non-test) catch or appeal."""
        row = await self._db.fetchone(
            """
            SELECT 1 AS x FROM honeypot_events WHERE guild_id = ? AND user_id = ? AND is_test = 0
            UNION ALL
            SELECT 1 FROM honeypot_appeals WHERE guild_id = ? AND user_id = ? AND is_test = 0
            LIMIT 1
            """,
            (guild_id, user_id, guild_id, user_id),
        )
        return row is not None

    async def list_test_cases(self, guild_id: int) -> list[dict]:
        """One row per test catch, with its (latest) test appeal if any."""
        rows = await self._db.fetchall(
            """
            SELECT e.id AS catch_id, e.user_id, e.username, e.created_at,
                   a.id AS appeal_id, a.status, a.verified_via, a.rejoined_at
            FROM honeypot_events e
            LEFT JOIN honeypot_appeals a
              ON a.id = (SELECT MAX(id) FROM honeypot_appeals
                          WHERE guild_id = e.guild_id AND user_id = e.user_id AND is_test = 1)
            WHERE e.guild_id = ? AND e.is_test = 1
            ORDER BY e.id DESC
            """,
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def delete_test_cases(self, guild_id: int) -> dict[str, int]:
        appeals = await self._db.execute("DELETE FROM honeypot_appeals WHERE guild_id = ? AND is_test = 1", (guild_id,))
        events = await self._db.execute("DELETE FROM honeypot_events WHERE guild_id = ? AND is_test = 1", (guild_id,))
        return {"catches": events, "appeals": appeals}

    async def set_outcome_dm(self, appeal_id: int, sent: bool) -> None:
        await self._db.execute(
            "UPDATE honeypot_appeals SET outcome_dm_sent = ? WHERE id = ?", (int(sent), appeal_id),
        )

    async def mark_rejoined(self, appeal_id: int) -> None:
        await self._db.execute("UPDATE honeypot_appeals SET rejoined_at = ? WHERE id = ?", (_now(), appeal_id))

    async def set_appeal_catch(self, appeal_id: int, catch_id: int) -> None:
        await self._db.execute(
            "UPDATE honeypot_appeals SET catch_id = ? WHERE id = ?", (catch_id, appeal_id),
        )

    async def get_appeal(self, appeal_id: int) -> dict | None:
        row = await self._db.fetchone("SELECT * FROM honeypot_appeals WHERE id = ?", (appeal_id,))
        return dict(row) if row else None

    async def get_appeal_by_token(self, guild_id: int, token: str) -> dict | None:
        """Looks up by the HASH of the raw token the person presents (their DM link)."""
        row = await self._db.fetchone(
            "SELECT * FROM honeypot_appeals WHERE guild_id = ? AND token = ?", (guild_id, hash_token(token)),
        )
        return dict(row) if row else None

    async def hash_legacy_tokens(self) -> int:
        """Replaces raw tokens written before migration 030 with their hash. Idempotent."""
        rows = await self._db.fetchall(
            "SELECT id, token FROM honeypot_appeals WHERE token NOT LIKE ?", (TOKEN_HASH_PREFIX + "%",),
        )
        for r in rows:
            await self._db.execute("UPDATE honeypot_appeals SET token = ? WHERE id = ?", (hash_token(r["token"]), r["id"]))
        return len(rows)

    async def get_latest_appeal_for_user(self, guild_id: int, user_id: int) -> dict | None:
        row = await self._db.fetchone(
            """
            SELECT * FROM honeypot_appeals
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, user_id),
        )
        return dict(row) if row else None

    async def list_appeals(self, guild_id: int, status: str | None = None, limit: int = 200) -> list[dict]:
        """Newest first. `status=None` means every status. 'invited' rows have no
        submission yet, so they are only returned when asked for explicitly or via None."""
        if status is None:
            rows = await self._db.fetchall(
                "SELECT * FROM honeypot_appeals WHERE guild_id = ? ORDER BY id DESC LIMIT ?",
                (guild_id, limit),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM honeypot_appeals WHERE guild_id = ? AND status = ? ORDER BY id DESC LIMIT ?",
                (guild_id, status, limit),
            )
        return [dict(r) for r in rows]

    async def count_appeals_by_status(self, guild_id: int) -> dict[str, int]:
        rows = await self._db.fetchall(
            "SELECT status, COUNT(*) AS n FROM honeypot_appeals WHERE guild_id = ? AND is_test = 0 GROUP BY status",
            (guild_id,),
        )
        counts = {s: 0 for s in APPEAL_STATUSES}
        for r in rows:
            counts[r["status"]] = int(r["n"])
        return counts

    async def submit_appeal(self, appeal_id: int, name: str, message: str, verified_via: str = "dm_link") -> bool:
        """invited -> pending. Returns False if the row was not 'invited' (lost a race)."""
        n = await self._db.execute(
            """
            UPDATE honeypot_appeals
            SET status = 'pending', name = ?, message = ?, submitted_at = ?, verified = 1, verified_via = ?
            WHERE id = ? AND status = 'invited'
            """,
            (name, message, _now(), verified_via, appeal_id),
        )
        return n > 0

    async def review_appeal(
        self, appeal_id: int, status: str, reviewer_id: int, note: str | None,
    ) -> bool:
        """pending -> approved/denied. Returns False if the row was not 'pending'."""
        if status not in ("approved", "denied"):
            raise ValueError(f"Invalid review status {status!r}")
        n = await self._db.execute(
            """
            UPDATE honeypot_appeals
            SET status = ?, reviewed_by = ?, reviewed_at = ?, review_note = ?
            WHERE id = ? AND status = 'pending'
            """,
            (status, reviewer_id, _now(), note, appeal_id),
        )
        return n > 0


def _load_errors(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return [str(raw)]
    return [str(v) for v in val] if isinstance(val, list) else [str(val)]


def _event_row(row) -> dict:
    d = dict(row)
    d["errors"] = _load_errors(d.get("errors"))
    for key in _EVENT_BOOLS:
        d[key] = bool(d.get(key))
    return d
