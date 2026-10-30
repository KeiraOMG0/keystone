-- Migration 021: event_bindings audience column
--
-- Structural readiness for future DM/multi-audience support. Only the
-- 'channel' audience is used today (no DM-sending logic is being built in
-- this migration) — this column just gives future work a place to land
-- without another schema change.
--
-- The existing UNIQUE constraint on event_bindings is (guild_id, event_type)
-- (see 003_events.sql). Adding a NOT NULL column with a DEFAULT does not
-- conflict with that constraint for any existing rows, and since only
-- 'channel' audience will ever be written right now, there is no need to
-- widen the UNIQUE constraint to (guild_id, event_type, audience) yet — doing
-- so would require an SQLite table rebuild (ALTER TABLE cannot modify
-- constraints directly) for no functional benefit while audience is
-- single-valued. Revisit the UNIQUE constraint if/when a second audience
-- value is actually introduced.

ALTER TABLE event_bindings ADD COLUMN audience TEXT NOT NULL DEFAULT 'channel';
