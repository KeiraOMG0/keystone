-- Migration 004: Managed messages and template components

CREATE TABLE IF NOT EXISTS managed_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER NOT NULL UNIQUE,
    template_id     INTEGER NOT NULL REFERENCES message_templates(id) ON DELETE CASCADE,
    last_synced_at  TEXT,
    sync_failed     INTEGER NOT NULL DEFAULT 0,
    created_by      INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_managed_messages_template
    ON managed_messages (template_id);

CREATE INDEX IF NOT EXISTS idx_managed_messages_guild
    ON managed_messages (guild_id);

CREATE TABLE IF NOT EXISTS template_components (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id  INTEGER NOT NULL REFERENCES message_templates(id) ON DELETE CASCADE,
    row_index    INTEGER NOT NULL DEFAULT 0,
    component_type TEXT NOT NULL,   -- button | select
    label        TEXT,
    emoji        TEXT,
    style        INTEGER,           -- discord ButtonStyle int (1-5)
    custom_id    TEXT,
    url          TEXT,
    placeholder  TEXT,              -- select placeholder text
    options_json TEXT,              -- JSON array for select options
    position     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_template_components_template
    ON template_components (template_id);
