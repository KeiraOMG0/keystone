-- Migration 029: periodic honeypot name scrambling
--
-- Every scramble_interval_hours the bot renames the honeypot channel and its
-- category to random gibberish (keeping the decoration around the words, e.g.
-- "🚫┊" or "───⋅ ... ⋅───"), so spam tools cannot learn to skip the trap by
-- name. Off by default. last_scrambled_at is the restart-safe schedule anchor
-- (ISO-8601 UTC; NULL = never, so the first scramble runs at the next check
-- after it is switched on). Applied exactly once by the migration runner.
ALTER TABLE honeypot_config ADD COLUMN scramble_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE honeypot_config ADD COLUMN scramble_interval_hours INTEGER NOT NULL DEFAULT 6;
ALTER TABLE honeypot_config ADD COLUMN last_scrambled_at TEXT;
