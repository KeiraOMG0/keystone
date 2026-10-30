-- Migration 030: honeypot appeal outcomes, rejoin link, hashed appeal tokens
--
-- honeypot_config.rejoin_invite_url    staff-set (or bot-created) invite shown to people
--                                      whose appeal was approved; NULL = none
-- honeypot_config.invite_in_removal_dm also put that invite in kick / softban DMs (0/1)
-- honeypot_appeals.outcome_dm_sent     NULL = no outcome DM tried yet, 0 = tried and
--                                      failed (usual after a ban: no shared server), 1 = sent
-- honeypot_appeals.rejoined_at         when the person used "Rejoin server" (Discord login
--                                      with guilds.join) after approval; ISO UTC
-- honeypot_appeals.verified_via        'dm_link' | 'discord_login' | NULL (typed user id)
--
-- From this version honeypot_appeals.token holds "sha256:<hex>" of the appeal link token,
-- never the token itself. SQLite has no SHA-256, so rows written before this migration
-- are converted by the bot at startup (HoneypotService.hash_legacy_tokens), before any
-- lookup can happen. Applied exactly once by the migration runner.
ALTER TABLE honeypot_config ADD COLUMN rejoin_invite_url TEXT;
ALTER TABLE honeypot_config ADD COLUMN invite_in_removal_dm INTEGER NOT NULL DEFAULT 0;
ALTER TABLE honeypot_appeals ADD COLUMN outcome_dm_sent INTEGER;
ALTER TABLE honeypot_appeals ADD COLUMN rejoined_at TEXT;
ALTER TABLE honeypot_appeals ADD COLUMN verified_via TEXT;
UPDATE honeypot_appeals SET verified_via = 'dm_link' WHERE verified = 1;
