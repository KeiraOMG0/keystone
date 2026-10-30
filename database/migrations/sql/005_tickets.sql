-- Migration 005: Ticket system

CREATE TABLE IF NOT EXISTS ticket_panels (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id             INTEGER NOT NULL,
    channel_id           INTEGER NOT NULL,
    message_id           INTEGER,
    template_id          INTEGER REFERENCES message_templates(id) ON DELETE SET NULL,
    open_category_id     INTEGER,
    closed_category_id   INTEGER,
    transcript_channel_id INTEGER,
    max_open_per_user    INTEGER NOT NULL DEFAULT 1,
    naming_pattern       TEXT    NOT NULL DEFAULT 'ticket-{number}',
    button_label         TEXT    NOT NULL DEFAULT 'Open Ticket',
    button_emoji         TEXT,
    created_by           INTEGER NOT NULL,
    created_at           TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_panel_roles (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id  INTEGER NOT NULL REFERENCES ticket_panels(id) ON DELETE CASCADE,
    role_id   INTEGER NOT NULL,
    role_type TEXT    NOT NULL DEFAULT 'support',   -- support | admin
    UNIQUE (panel_id, role_id)
);

CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    panel_id      INTEGER NOT NULL REFERENCES ticket_panels(id) ON DELETE CASCADE,
    channel_id    INTEGER NOT NULL UNIQUE,
    opened_by     INTEGER NOT NULL,
    claimed_by    INTEGER,
    status        TEXT    NOT NULL DEFAULT 'open',  -- open | closed | archived
    ticket_number INTEGER NOT NULL,
    opened_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at     TEXT,
    UNIQUE (guild_id, panel_id, ticket_number)
);

CREATE INDEX IF NOT EXISTS idx_tickets_guild_status
    ON tickets (guild_id, status);

CREATE INDEX IF NOT EXISTS idx_tickets_opened_by
    ON tickets (opened_by, guild_id);

CREATE INDEX IF NOT EXISTS idx_ticket_panels_guild
    ON ticket_panels (guild_id);
