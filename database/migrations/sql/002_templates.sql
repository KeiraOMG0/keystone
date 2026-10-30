CREATE TABLE IF NOT EXISTS message_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    description TEXT,
    category    TEXT,
    content     TEXT,
    created_by  INTEGER NOT NULL,
    updated_by  INTEGER,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(guild_id, name)
);

CREATE INDEX IF NOT EXISTS idx_templates_guild ON message_templates(guild_id);
CREATE INDEX IF NOT EXISTS idx_templates_category ON message_templates(guild_id, category);

CREATE TABLE IF NOT EXISTS template_embeds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id     INTEGER NOT NULL REFERENCES message_templates(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL DEFAULT 0,
    title           TEXT,
    description     TEXT,
    color           INTEGER,
    url             TEXT,
    footer_text     TEXT,
    footer_icon_url TEXT,
    thumbnail_url   TEXT,
    image_url       TEXT,
    author_name     TEXT,
    author_icon_url TEXT,
    author_url      TEXT,
    timestamp_mode  TEXT DEFAULT 'none'
);

CREATE INDEX IF NOT EXISTS idx_embeds_template ON template_embeds(template_id);

CREATE TABLE IF NOT EXISTS embed_fields (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    embed_id    INTEGER NOT NULL REFERENCES template_embeds(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    name        TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    inline      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fields_embed ON embed_fields(embed_id);

CREATE TABLE IF NOT EXISTS template_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id     INTEGER NOT NULL REFERENCES message_templates(id) ON DELETE CASCADE,
    version_number  INTEGER NOT NULL,
    snapshot_json   TEXT    NOT NULL,
    changed_by      INTEGER NOT NULL,
    change_summary  TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_versions_template ON template_versions(template_id, version_number);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    actor_id    INTEGER NOT NULL,
    action      TEXT    NOT NULL,
    target_type TEXT,
    target_id   INTEGER,
    detail_json TEXT,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_guild ON audit_log(guild_id, created_at);
