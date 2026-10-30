-- Allow guild_timezone to be NULL (unset means use UTC as fallback, not stored)
-- SQLite cannot ALTER COLUMN directly; recreate birthday_settings with nullable guild_timezone
CREATE TABLE birthday_settings_new (
    guild_id            INTEGER PRIMARY KEY,
    birthday_role_id    INTEGER,
    announce_channel_id INTEGER,
    guild_timezone      TEXT,
    announce_message    TEXT,
    announce_message_pl TEXT,
    announce_ping       INTEGER NOT NULL DEFAULT 0,
    add_only            INTEGER NOT NULL DEFAULT 0,
    private_confirms    INTEGER NOT NULL DEFAULT 0,
    ping_user           INTEGER NOT NULL DEFAULT 0,
    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO birthday_settings_new
SELECT
    guild_id, birthday_role_id, announce_channel_id,
    CASE WHEN guild_timezone = 'UTC' THEN NULL ELSE guild_timezone END,
    announce_message, announce_message_pl, announce_ping,
    add_only, private_confirms,
    COALESCE(ping_user, 0),
    created_at, updated_at
FROM birthday_settings;

DROP TABLE birthday_settings;
ALTER TABLE birthday_settings_new RENAME TO birthday_settings;
