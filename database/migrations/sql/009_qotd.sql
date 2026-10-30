-- Migration 009: QOTD (Question of the Day) System

CREATE TABLE IF NOT EXISTS qotd_config (
    guild_id    INTEGER PRIMARY KEY,
    channel_id  INTEGER,
    post_hour   INTEGER NOT NULL DEFAULT 9,   -- UTC hour to post (0-23)
    post_minute INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    model       TEXT    NOT NULL DEFAULT 'llama3.2:3b',
    use_emojis  INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS qotd_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    question    TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'ai',  -- 'ai' | 'fallback'
    posted_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    message_id  INTEGER,
    channel_id  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_qotd_history_guild
    ON qotd_history (guild_id, posted_at);
