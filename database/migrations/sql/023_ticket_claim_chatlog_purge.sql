-- Migration 023: Ticket claim-button persistence, archive timestamp, and
-- opt-in text chat-log capture.
--
-- 1. tickets.control_message_id — the ID of the Close/Claim (or Verify &
--    Close/Claim/Close) control message posted inside the ticket channel by
--    TicketService.open_ticket. Previously not stored anywhere, so there was
--    no way to find-and-edit that message later (e.g. to disable the Claim
--    button once a ticket is claimed) outside of the live button-click
--    interaction itself. Nullable — older rows created before this
--    migration simply won't have their control message editable
--    retroactively; the bot re-adds a plain (unclaimed-state) persistent
--    view for them on restart same as before, matching cogs/tickets.py's
--    setup() view-restoration path.
--
-- 2. tickets.archived_at — distinct timestamp for when a ticket was
--    archived, set by TicketService.archive_ticket. Previously archive
--    reused `closed_at` (set at close time, not archive time) as its only
--    timestamp, which would have made the 30-day auto-purge job measure
--    time-since-close rather than time-since-archive for tickets that sit
--    in "closed" for a while before being archived. Added instead of
--    reusing closed_at so the purge job's reference point is unambiguous.
--
-- 3. ticket_messages — plain-text chat-log capture for open/archived
--    tickets, gated behind config.TICKET_CHAT_LOGGING_ENABLED (default
--    False). Explicitly text-only: no attachment URLs, no voice-message
--    metadata are ever written here (see cogs/tickets.py's on_message
--    listener for the capture logic, which strips everything but content).
CREATE TABLE IF NOT EXISTS ticket_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_id  INTEGER NOT NULL,
    content    TEXT    NOT NULL,
    sent_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket
    ON ticket_messages (ticket_id, sent_at);

ALTER TABLE tickets ADD COLUMN control_message_id INTEGER;
ALTER TABLE tickets ADD COLUMN archived_at TEXT;
