CREATE TABLE IF NOT EXISTS birthday_settings (
    guild_id        INTEGER PRIMARY KEY,
    birthday_role_id    INTEGER,
    announce_channel_id INTEGER,
    guild_timezone      TEXT NOT NULL DEFAULT 'UTC',
    announce_message    TEXT,
    announce_message_pl TEXT,
    announce_ping       INTEGER NOT NULL DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS birthdays (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    birth_month     INTEGER NOT NULL CHECK (birth_month BETWEEN 1 AND 12),
    birth_day       INTEGER NOT NULL CHECK (birth_day BETWEEN 1 AND 31),
    user_timezone   TEXT,
    last_celebrated INTEGER NOT NULL DEFAULT 0,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_birthdays_month_day ON birthdays (birth_month, birth_day);
