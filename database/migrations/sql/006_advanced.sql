-- Migration 006: Permissions, Scheduling, Autorole

CREATE TABLE IF NOT EXISTS permission_grants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    permission  TEXT    NOT NULL,
    granted_by  INTEGER NOT NULL,
    granted_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (guild_id, role_id, permission)
);

CREATE INDEX IF NOT EXISTS idx_permission_grants_guild
    ON permission_grants (guild_id);

CREATE TABLE IF NOT EXISTS scheduled_sends (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    template_id  INTEGER NOT NULL REFERENCES message_templates(id) ON DELETE CASCADE,
    cron_expr    TEXT    NOT NULL,
    next_run_at  TEXT,
    last_run_at  TEXT,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   INTEGER NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    label        TEXT
);

CREATE INDEX IF NOT EXISTS idx_scheduled_sends_guild
    ON scheduled_sends (guild_id);

CREATE TABLE IF NOT EXISTS autorole_rules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    trigger_type TEXT    NOT NULL,   -- join | boost | role_add
    trigger_role_id INTEGER,         -- for role_add trigger: which role being added triggers this
    role_id      INTEGER NOT NULL,
    delay_seconds INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_by   INTEGER NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (guild_id, trigger_type, trigger_role_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_autorole_rules_guild
    ON autorole_rules (guild_id, trigger_type);
