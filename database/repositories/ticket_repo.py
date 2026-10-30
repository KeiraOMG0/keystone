from database.repositories.base import BaseRepository


# Archived tickets due for the retention purge: the EARLIER of closed_at /
# archived_at is before the cutoff (two `?` = the same cutoff). Covers legacy
# rows with archived_at NULL or backfilled long after closing.
_PURGE_WHERE = (
    "status = 'archived' AND (archived_at IS NOT NULL OR closed_at IS NOT NULL) "
    "AND (COALESCE(archived_at, closed_at) < ? OR COALESCE(closed_at, archived_at) < ?)"
)


class TicketRepository(BaseRepository):

    # ------------------------------------------------------------------
    # Panels
    # ------------------------------------------------------------------

    async def create_panel(
        self,
        guild_id: int,
        channel_id: int,
        created_by: int,
        template_id: int | None = None,
        open_category_id: int | None = None,
        closed_category_id: int | None = None,
        transcript_channel_id: int | None = None,
        max_open_per_user: int = 1,
        naming_pattern: str = "ticket-{number}",
        button_label: str = "Open Ticket",
        button_emoji: str | None = None,
        purpose: str = "support",
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO ticket_panels
                (guild_id, channel_id, created_by, template_id,
                 open_category_id, closed_category_id, transcript_channel_id,
                 max_open_per_user, naming_pattern, button_label, button_emoji, purpose)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id, channel_id, created_by, template_id,
                open_category_id, closed_category_id, transcript_channel_id,
                max_open_per_user, naming_pattern, button_label, button_emoji, purpose,
            ),
        )

    async def get_panel(self, panel_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM ticket_panels WHERE id = ?", (panel_id,)
        )
        return dict(row) if row else None

    async def get_panel_by_message(self, message_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM ticket_panels WHERE message_id = ?", (message_id,)
        )
        return dict(row) if row else None

    async def list_panels(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM ticket_panels WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def get_all_panels(self) -> list[dict]:
        rows = await self._db.fetchall("SELECT * FROM ticket_panels WHERE message_id IS NOT NULL")
        return [dict(r) for r in rows]

    async def set_panel_message(self, panel_id: int, message_id: int) -> None:
        await self._db.execute(
            "UPDATE ticket_panels SET message_id = ? WHERE id = ?",
            (message_id, panel_id),
        )

    async def update_panel(self, panel_id: int, **kwargs) -> None:
        allowed = {
            "template_id", "open_category_id", "closed_category_id",
            "transcript_channel_id", "max_open_per_user", "naming_pattern",
            "button_label", "button_emoji", "purpose",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        set_parts = [f"{k} = ?" for k in fields]
        values = list(fields.values()) + [panel_id]
        await self._db.execute(
            f"UPDATE ticket_panels SET {', '.join(set_parts)} WHERE id = ?",
            tuple(values),
        )

    async def delete_panel(self, panel_id: int) -> None:
        await self._db.execute("DELETE FROM ticket_panels WHERE id = ?", (panel_id,))

    # ------------------------------------------------------------------
    # Panel roles
    # ------------------------------------------------------------------

    async def add_panel_role(self, panel_id: int, role_id: int, role_type: str = "support") -> None:
        await self._db.execute(
            """
            INSERT INTO ticket_panel_roles (panel_id, role_id, role_type)
            VALUES (?, ?, ?)
            ON CONFLICT (panel_id, role_id) DO UPDATE SET role_type = excluded.role_type
            """,
            (panel_id, role_id, role_type),
        )

    async def remove_panel_role(self, panel_id: int, role_id: int) -> None:
        await self._db.execute(
            "DELETE FROM ticket_panel_roles WHERE panel_id = ? AND role_id = ?",
            (panel_id, role_id),
        )

    async def get_panel_roles(self, panel_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM ticket_panel_roles WHERE panel_id = ?", (panel_id,)
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Tickets
    # ------------------------------------------------------------------

    async def next_ticket_number(self, guild_id: int, panel_id: int) -> int:
        row = await self._db.fetchone(
            "SELECT COALESCE(MAX(ticket_number), 0) + 1 AS next FROM tickets WHERE guild_id = ? AND panel_id = ?",
            (guild_id, panel_id),
        )
        return row["next"] if row else 1

    async def create_ticket(
        self,
        guild_id: int,
        panel_id: int,
        channel_id: int,
        opened_by: int,
        ticket_number: int,
    ) -> int:
        return await self._db.execute_returning_id(
            """
            INSERT INTO tickets (guild_id, panel_id, channel_id, opened_by, ticket_number)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, panel_id, channel_id, opened_by, ticket_number),
        )

    async def get_ticket(self, ticket_id: int) -> dict | None:
        row = await self._db.fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        return dict(row) if row else None

    async def get_ticket_by_channel(self, channel_id: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)
        )
        return dict(row) if row else None

    async def count_open_by_user(self, guild_id: int, opened_by: int, panel_id: int) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS cnt FROM tickets WHERE guild_id = ? AND opened_by = ? AND panel_id = ? AND status = 'open'",
            (guild_id, opened_by, panel_id),
        )
        return row["cnt"] if row else 0

    async def count_all_by_user(self, guild_id: int, opened_by: int, panel_id: int) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS cnt FROM tickets WHERE guild_id = ? AND opened_by = ? AND panel_id = ?",
            (guild_id, opened_by, panel_id),
        )
        return row["cnt"] if row else 0

    async def list_unarchived_by_user(self, guild_id: int, opened_by: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM tickets WHERE guild_id = ? AND opened_by = ? AND status != 'archived'",
            (guild_id, opened_by),
        )
        return [dict(r) for r in rows]

    async def set_status(self, ticket_id: int, status: str, claimed_by: int | None = None) -> None:
        if status == "closed":
            await self._db.execute(
                "UPDATE tickets SET status = ?, closed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, ticket_id),
            )
        elif status == "archived":
            # archived_at is distinct from closed_at (set earlier, at close time) so the
            # 30-day auto-purge job (see purge_archived_before) measures time-since-archive,
            # not time-since-close. See migration 023's docstring for the rationale.
            await self._db.execute(
                "UPDATE tickets SET status = ?, archived_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, ticket_id),
            )
        else:
            await self._db.execute(
                "UPDATE tickets SET status = ? WHERE id = ?",
                (status, ticket_id),
            )
        if claimed_by is not None:
            await self._db.execute(
                "UPDATE tickets SET claimed_by = ? WHERE id = ?",
                (claimed_by, ticket_id),
            )

    async def transition_status(self, ticket_id: int, from_statuses: tuple[str, ...], to_status: str) -> bool:
        """Atomically move a ticket to `to_status` only if it is currently in
        one of `from_statuses` (stamping closed_at / archived_at like
        set_status). Returns False when another caller got there first, so a
        double click, the dashboard racing the button, or the delayed delete
        racing the restart-recovery sweep acts exactly once."""
        stamp = {
            "closed": ", closed_at = CURRENT_TIMESTAMP",
            "archived": ", archived_at = CURRENT_TIMESTAMP",
        }.get(to_status, "")
        placeholders = ",".join("?" * len(from_statuses))
        rowcount = await self._db.execute(
            f"UPDATE tickets SET status = ?{stamp} WHERE id = ? AND status IN ({placeholders})",
            (to_status, ticket_id, *from_statuses),
        )
        return bool(rowcount)

    async def claim_ticket(self, ticket_id: int, claimed_by: int) -> bool:
        """Atomically claim a ticket only if it is not already claimed.

        Returns True if this call performed the claim, False if the ticket
        was already claimed by someone (including the same user re-clicking
        a stale button) — callers must treat False as "reject, don't
        silently reassign" rather than retrying.
        """
        rowcount = await self._db.execute(
            "UPDATE tickets SET claimed_by = ? WHERE id = ? AND claimed_by IS NULL",
            (claimed_by, ticket_id),
        )
        return bool(rowcount)

    async def set_control_message_id(self, ticket_id: int, message_id: int) -> None:
        await self._db.execute(
            "UPDATE tickets SET control_message_id = ? WHERE id = ?",
            (message_id, ticket_id),
        )

    async def list_open_tickets(self, guild_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT t.*, p.purpose AS panel_purpose
            FROM tickets t
            LEFT JOIN ticket_panels p ON p.id = t.panel_id
            WHERE t.guild_id = ? AND t.status = 'open'
            ORDER BY t.opened_at
            """,
            (guild_id,),
        )
        return [dict(r) for r in rows]

    async def list_by_status(self, guild_id: int, status: str) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT t.*, p.purpose AS panel_purpose
            FROM tickets t
            LEFT JOIN ticket_panels p ON p.id = t.panel_id
            WHERE t.guild_id = ? AND t.status = ?
            ORDER BY t.opened_at
            """,
            (guild_id, status),
        )
        return [dict(r) for r in rows]

    async def list_all(self, guild_id: int, limit: int = 100) -> list[dict]:
        """All tickets for a guild regardless of status, most recent first,
        capped at `limit` — used by the dashboard's "All" ticket browse view."""
        rows = await self._db.fetchall(
            """
            SELECT t.*, p.purpose AS panel_purpose
            FROM tickets t
            LEFT JOIN ticket_panels p ON p.id = t.panel_id
            WHERE t.guild_id = ?
            ORDER BY t.opened_at DESC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return [dict(r) for r in rows]

    async def delete_ticket(self, ticket_id: int) -> bool:
        """Hard-deletes a ticket row and its ticket_messages. Callers
        (TicketService.delete_ticket) are responsible for only allowing this
        for status='archived' tickets. Returns True if a ticket row was
        actually deleted.

        NOTE: this DB connection does not enable `PRAGMA foreign_keys`, so
        the `ON DELETE CASCADE` on ticket_messages.ticket_id is inert
        (matches the pre-existing behavior of every other FK in this
        codebase, e.g. ticket_panel_roles) — ticket_messages rows are
        deleted explicitly here rather than relying on it.
        """
        await self._db.execute("DELETE FROM ticket_messages WHERE ticket_id = ?", (ticket_id,))
        rowcount = await self._db.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
        return bool(rowcount)

    async def list_archived_before(self, cutoff_iso: str) -> list[dict]:
        """The archived tickets purge_archived_before would delete (id,
        guild_id, channel_id), so the service can remove any channel that
        was archived in place before the rows go."""
        rows = await self._db.fetchall(
            "SELECT id, guild_id, channel_id FROM tickets "
            f"WHERE {_PURGE_WHERE}",
            (cutoff_iso, cutoff_iso),
        )
        return [dict(r) for r in rows]

    async def purge_archived_before(self, cutoff_iso: str) -> list[int]:
        """Deletes every archived ticket closed or archived before
        `cutoff_iso` (an ISO8601 UTC string) and returns the list of deleted
        ticket IDs, for logging. Explicitly deletes ticket_messages first —
        see delete_ticket's note on why FK cascade isn't relied on here.

        The EARLIER of closed_at / archived_at counts (Privacy Policy: "30
        days after it is closed"). Tickets archived before archived_at existed
        have it NULL and were never purged; some had it backfilled long after
        they closed (security audit 2026-10-30)."""
        rows = await self._db.fetchall(f"SELECT id FROM tickets WHERE {_PURGE_WHERE}", (cutoff_iso, cutoff_iso))
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            await self._db.execute(
                f"DELETE FROM ticket_messages WHERE ticket_id IN ({placeholders})",
                tuple(ids),
            )
            await self._db.execute(
                f"DELETE FROM tickets WHERE id IN ({placeholders})",
                tuple(ids),
            )
        return ids

    # ------------------------------------------------------------------
    # Ticket chat-log messages (opt-in, plain text only — see config.py's
    # TICKET_CHAT_LOGGING_ENABLED and migration 023's docstring)
    # ------------------------------------------------------------------

    async def add_ticket_message(self, ticket_id: int, author_id: int, content: str) -> int:
        return await self._db.execute_returning_id(
            "INSERT INTO ticket_messages (ticket_id, author_id, content) VALUES (?, ?, ?)",
            (ticket_id, author_id, content),
        )

    async def list_ticket_messages(self, ticket_id: int) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY sent_at, id",
            (ticket_id,),
        )
        return [dict(r) for r in rows]
