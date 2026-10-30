-- Migration 028: honeypot catches made through an app
--
-- A member can make an app post for them: a user-installed app (no server
-- authorisation needed while @everyone has "Use External Apps") or a server
-- bot's slash command. The message's author is the app, but the member who ran
-- the command is the one caught. These columns record which app posted it
-- (NULL for a normal message). Applied exactly once by the migration runner.
ALTER TABLE honeypot_events ADD COLUMN via_app_id INTEGER;
ALTER TABLE honeypot_events ADD COLUMN via_app_name TEXT;
