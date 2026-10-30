-- Migration 025: Honeypot (trap channel for hacked accounts / spam bots)
--
-- A public "do not type" channel that @everyone can see and post in. Anyone
-- who posts there (other than staff/bots) is punished automatically by
-- services/honeypot_service.py. See docs/honeypot_contract.md for the full
-- behaviour + API contract shared with the dashboard.
--
-- Three tables:
--   honeypot_config  - one row per guild: the channel/category, the action to
--                      apply, toggles, and the two live embed message ids.
--   honeypot_events  - one row per catch (content truncated to 500 chars plus
--                      account facts used by the stats page).
--   honeypot_appeals - ban appeals. Rows start as 'invited' (created when the
--                      DM carrying the appeal link is sent) and move to
--                      'pending' when the person submits the form, then
--                      'approved' / 'denied' when staff review it.
--
-- Snowflake columns are INTEGER (bot convention). All timestamps are
-- ISO-8601 UTC strings written by the application.
CREATE TABLE IF NOT EXISTS honeypot_config (
    guild_id             INTEGER PRIMARY KEY,
    enabled              INTEGER NOT NULL DEFAULT 1,
    channel_id           INTEGER,
    category_id          INTEGER,
    action               TEXT    NOT NULL DEFAULT 'ban',   -- 'ban' | 'softban' | 'kick'
    dm_before            INTEGER NOT NULL DEFAULT 1,
    appeal_enabled       INTEGER NOT NULL DEFAULT 1,
    delete_message_days  INTEGER NOT NULL DEFAULT 1,       -- 0-7
    log_channel_id       INTEGER,                          -- NULL = use the moderation-log channel
    warning_message_id   INTEGER,                          -- the "do not type" embed in the trap channel
    stats_message_id     INTEGER,                          -- the live "Caught so far" embed
    updated_at           TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS honeypot_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id            INTEGER NOT NULL,
    user_id             INTEGER NOT NULL,
    username            TEXT,
    display_name        TEXT,
    avatar_url          TEXT,
    channel_id          INTEGER,
    content             TEXT,                              -- truncated to 500 chars
    action              TEXT    NOT NULL,                  -- 'ban' | 'softban' | 'kick'
    action_result       TEXT    NOT NULL DEFAULT 'ok',     -- 'ok' | 'partial' | 'failed'
    errors              TEXT,                              -- JSON array of strings
    dm_sent             INTEGER NOT NULL DEFAULT 0,
    account_created_at  TEXT,
    joined_at           TEXT,
    seconds_since_join  INTEGER,
    had_default_avatar  INTEGER NOT NULL DEFAULT 0,
    flag_link           INTEGER NOT NULL DEFAULT 0,
    flag_invite         INTEGER NOT NULL DEFAULT 0,
    flag_everyone       INTEGER NOT NULL DEFAULT 0,
    flag_attachment     INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_honeypot_events_guild_created
    ON honeypot_events (guild_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_honeypot_events_guild_user
    ON honeypot_events (guild_id, user_id);

CREATE TABLE IF NOT EXISTS honeypot_appeals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    token         TEXT    NOT NULL UNIQUE,
    username      TEXT,
    name          TEXT,                                    -- what the person typed in the form
    message       TEXT,
    status        TEXT    NOT NULL DEFAULT 'invited',      -- 'invited' | 'pending' | 'approved' | 'denied'
    catch_id      INTEGER,                                 -- honeypot_events.id this appeal is about
    created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    submitted_at  TEXT,
    reviewed_by   INTEGER,
    reviewed_at   TEXT,
    review_note   TEXT
);

CREATE INDEX IF NOT EXISTS idx_honeypot_appeals_guild_status
    ON honeypot_appeals (guild_id, status);

CREATE INDEX IF NOT EXISTS idx_honeypot_appeals_guild_user
    ON honeypot_appeals (guild_id, user_id);
