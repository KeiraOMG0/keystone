-- Migration 003: Event bindings
-- Maps guild events (member_join, member_leave, boost) to channels + templates.

CREATE TABLE IF NOT EXISTS event_bindings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    event_type  TEXT    NOT NULL,   -- member_join | member_leave | boost
    channel_id  INTEGER NOT NULL,
    send_dm     INTEGER NOT NULL DEFAULT 0,  -- 1 = also send a DM to the member
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_by  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (guild_id, event_type)
);

CREATE TABLE IF NOT EXISTS event_binding_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_id  INTEGER NOT NULL REFERENCES event_bindings(id) ON DELETE CASCADE,
    template_id INTEGER NOT NULL REFERENCES message_templates(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (binding_id, template_id)
);

CREATE INDEX IF NOT EXISTS idx_event_bindings_guild_type
    ON event_bindings (guild_id, event_type);
