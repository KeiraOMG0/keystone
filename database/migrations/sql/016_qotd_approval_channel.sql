-- Migration 016: Add approval_channel_id to qotd_config
-- Allows the approval/review channel to be set per-guild via /qotd setapprovalchannel
ALTER TABLE qotd_config ADD COLUMN approval_channel_id INTEGER;
