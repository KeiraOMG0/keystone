-- Migration 007: Reaction role panels

CREATE TABLE IF NOT EXISTS reaction_role_panels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    template_id INTEGER REFERENCES message_templates(id) ON DELETE SET NULL,
    title       TEXT    NOT NULL DEFAULT 'React for a role!',
    exclusive   INTEGER NOT NULL DEFAULT 0,  -- 1 = user can only hold one role from this panel
    created_by  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reaction_role_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id    INTEGER NOT NULL REFERENCES reaction_role_panels(id) ON DELETE CASCADE,
    emoji       TEXT    NOT NULL,
    role_id     INTEGER NOT NULL,
    label       TEXT,
    position    INTEGER NOT NULL DEFAULT 0,
    UNIQUE (panel_id, emoji)
);

CREATE INDEX IF NOT EXISTS idx_reaction_role_panels_guild
    ON reaction_role_panels (guild_id);

CREATE INDEX IF NOT EXISTS idx_reaction_role_entries_panel
    ON reaction_role_entries (panel_id);
