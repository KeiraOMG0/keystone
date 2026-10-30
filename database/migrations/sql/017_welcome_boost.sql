-- Migration 017: Welcome + Boost channel config

CREATE TABLE IF NOT EXISTS welcome_config (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS boost_config (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER,
    enabled    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
