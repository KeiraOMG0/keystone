-- Migration 022: Unified moderation-action history
--
-- Covers kick / ban / unban / timeout / untimeout (Discord-side actions that,
-- prior to this migration, were only recorded in the generic `audit_log`
-- table with a JSON detail blob — see database/repositories/audit_repo.py).
-- `audit_log` remains untouched and continues to receive its own entries
-- (ModerationService still calls AuditService.log() for every action, same
-- as the old inline cog code did) — this table exists purely to give the
-- dashboard (and any future !modhistory-style command) a queryable,
-- typed, per-user action history without parsing detail_json.
--
-- `warnings` (018_warnings.sql) is intentionally NOT merged into this table
-- and remains the system of record for warning CRUD (dashboard warning
-- add/remove UI keys off warnings.id). Instead, ModerationService writes a
-- companion row into mod_actions (action_type='warn' / 'warning_removed')
-- whenever it touches warnings, with warning_id stored in extra_data so the
-- two rows can be cross-referenced. This keeps warnings.py's existing
-- CRUD-by-id semantics intact while still surfacing warns in the unified
-- history timeline.
CREATE TABLE IF NOT EXISTS mod_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    moderator_id  INTEGER NOT NULL,
    action_type   TEXT    NOT NULL,  -- 'kick' | 'ban' | 'unban' | 'timeout' | 'untimeout' | 'warn' | 'warning_removed'
    reason        TEXT,
    duration_minutes INTEGER,        -- timeout only; NULL otherwise
    expires_at    TEXT,              -- timeout only (ISO8601 UTC); NULL otherwise
    extra_data    TEXT,              -- optional JSON blob (e.g. {"warning_id": 12})
    created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mod_actions_guild_user
    ON mod_actions (guild_id, user_id);

CREATE INDEX IF NOT EXISTS idx_mod_actions_guild_created
    ON mod_actions (guild_id, created_at DESC);
