-- Migration 010: Add ping_role_id to qotd_config
ALTER TABLE qotd_config ADD COLUMN ping_role_id INTEGER;
