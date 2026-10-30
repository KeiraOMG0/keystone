-- Migration 020: Composite index for per-panel per-user ticket lookups
--
-- The existing idx_tickets_opened_by index is (opened_by, guild_id), which is
-- not panel-scoped. Ticket naming (username collision counting) and other
-- per-panel per-user queries filter on (panel_id, opened_by), so add a
-- dedicated composite index to keep those lookups fast as ticket history grows.

CREATE INDEX IF NOT EXISTS idx_tickets_panel_opened_by
    ON tickets (panel_id, opened_by);
