-- Migration 011: Add staff_ping_role_id to qotd_config
-- Used to ping a staff role in the approval channel when a QOTD needs review.
ALTER TABLE qotd_config ADD COLUMN staff_ping_role_id INTEGER;
