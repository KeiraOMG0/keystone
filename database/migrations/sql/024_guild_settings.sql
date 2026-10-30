-- Migration 024: Dashboard-editable guild settings (key/value store).
--
-- Backs the new admin-only Settings dashboard page. Before this migration,
-- every operational setting (channel/role/category IDs, toggles,
-- thresholds) lived ONLY in .env, read once into config.py module-level
-- constants at process startup — changing e.g. LOG_CHANNEL_ID meant editing
-- .env and restarting the bot process. There is no existing settings table;
-- guild_config (001_initial.sql) has a couple of unrelated columns
-- (log_channel_id, audit_channel_id, timezone) that were never actually
-- wired up to anything reading them (grep confirms zero call sites), so we
-- do not reuse/extend it — a fresh table keeps this migration's intent
-- self-contained and avoids resurrecting dead columns with unclear
-- semantics.
--
-- Schema choice: key/value rather than typed columns. Rationale — the set
-- of dashboard-editable settings will grow over time (see
-- services/settings_service.py's SETTINGS_SCHEMA for the current
-- allowlist), and a key/value table means adding a new editable setting
-- later never requires another migration + ALTER TABLE, just a new entry in
-- that Python-side allowlist. `value` is always stored as TEXT (bot-side
-- code parses to int/bool per SETTINGS_SCHEMA's declared type); this
-- mirrors how config.py itself parses every os.environ value (always a
-- string) into int/bool today, so the parsing burden simply moves from
-- os.environ to this table without changing shape.
--
-- guild_id is included (not a singleton row) for forward-compatibility with
-- a hypothetical future multi-guild deployment, matching guild_config's own
-- guild_id-keyed shape, even though today's deployment is single-guild
-- (server/config.py's DASHBOARD_GUILD_ID).
--
-- Live-reload scope (see services/settings_service.py + config.py's bridge
-- for the full explanation): settings read fresh from config.* at the
-- point of use (e.g. cogs/tickets.py's TICKET_CHAT_LOGGING_ENABLED /
-- TICKET_ARCHIVE_PURGE_DAYS checks) become live the moment this table is
-- updated, via a short in-memory TTL cache. Settings that other cogs copy
-- into module-level constants at import time (cogs/kick.py, cogs/verify.py,
-- cogs/moderation.py, services/permission_service.py all do this) are NOT
-- retroactively made live by this migration alone — those still take effect
-- on next bot restart. This is a deliberate, documented v1 scope limit, not
-- an oversight: rewriting every cog's module-constant pattern to re-query
-- per-use was judged too invasive for this pass.
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id   INTEGER NOT NULL,
    key        TEXT    NOT NULL,
    value      TEXT,                     -- NULL = explicitly unset; falls back to config.py/.env default
    updated_by INTEGER,                  -- Discord user id of the dashboard admin who last changed it; NULL if never touched via dashboard
    updated_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, key)
);
