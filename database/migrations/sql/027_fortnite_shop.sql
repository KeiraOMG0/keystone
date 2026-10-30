-- Migration 027: Fortnite item shop auto-post config + item wishlist
-- (services/fortnite_shop_service.py, cogs/fortnite_shop.py).
--
-- fortnite_shop_config: one row per guild. last_posted_date (the shop's
-- YYYY-MM-DD from fortnite-api.com's data.date) is the restart-safe dedupe
-- key for the daily auto-post: the loop never posts the same shop date twice.
-- Debug/test posts from the owner-only dashboard page never touch it.
--
-- fortnite_wishlist: members opt in with /fortnite wishlist add <item>; the
-- daily post pings matching members. query is stored lower-cased and trimmed
-- so the UNIQUE constraint is effectively case-insensitive.

CREATE TABLE IF NOT EXISTS fortnite_shop_config (
    guild_id            INTEGER PRIMARY KEY,
    channel_id          INTEGER,
    enabled             INTEGER NOT NULL DEFAULT 0,
    include_jam_tracks  INTEGER NOT NULL DEFAULT 0,
    ping_role_id        INTEGER,
    last_posted_date    TEXT,
    last_posted_hash    TEXT,
    last_posted_at      TEXT,
    last_message_id     INTEGER,
    last_error          TEXT,
    last_error_at       TEXT,
    updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fortnite_wishlist (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id            INTEGER NOT NULL,
    user_id             INTEGER NOT NULL,
    query               TEXT    NOT NULL,
    created_at          TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_notified_date  TEXT,
    UNIQUE (guild_id, user_id, query)
);

CREATE INDEX IF NOT EXISTS idx_fortnite_wishlist_guild ON fortnite_wishlist (guild_id);
