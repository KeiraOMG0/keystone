# Honeypot feature — shared contract

Both the bot-side work and the dashboard-side work build against THIS file. If you
must deviate, note it in your final report so the other side can adapt.

## Behaviour (what the feature does)

A public "honeypot" category + channel that `@everyone` can see and type in. Hacked
accounts and spam bots post into every channel they can find; anyone who posts in the
honeypot channel (other than staff) is punished automatically. Since 2026-10-20 that includes a member who makes an
APP post there (see "Apps, webhooks and bots" below); webhook and bot posts are removed.

Per catch, in this order:
1. Record the message content (truncated 500 chars) and account facts, then delete the message.
2. If `dm_before`: DM the user BEFORE acting (best-effort; failure is fine and recorded as `dm_sent=false`).
   - For `action=ban` and `appeal_enabled`: DM includes an appeal link `{APPEAL_BASE_URL}/appeal/{token}`
     (token = `secrets.token_urlsafe(24)`, stored on an `honeypot_appeals` row with status `invited`).
   - DM text explains: what happened, that this is automatic, that if their account was hacked they
     should secure it (change password, enable 2FA, revoke unknown authorised apps), and (ban) the appeal link.
3. Apply the action via the existing `services.moderation_service.ModerationService` (moderator = the bot user,
   reason `"Honeypot: posted in #<channel>"`):
   - `ban`     → ban, delete_message_days = `delete_message_days`
   - `softban` → ban with delete_message_days = `delete_message_days` (min 1 for softban), then immediately unban
   - `kick`    → kick
4. Insert a `honeypot_events` row (with `action_result` ok/partial/failed and `errors`).
5. Post a "Honeypot catch" embed to the log channel (`log_channel_id` if set, else the moderation-log
   channel resolved through `services/log_router.py`).
6. Refresh the live "Caught so far" embed in the honeypot channel (debounced, ≥10s between edits).

### Apps, webhooks and bots (2026-10-20)
Order of checks for a message in the trap channel:
1. This bot's own messages (warning / "Caught so far" embeds): ignored.
2. App reply (`message.interaction_metadata.user` is set; user-installed app or a server bot's slash command):
   a catch of THAT member, exactly as above (exemptions, DM, action, event row). Text = content + embed
   title/description/fields. Event row gets `via_app_id` / `via_app_name` (migration 028); ban reason
   `Honeypot: posted in #<channel> via app <name>` (keeps the `Honeypot` prefix for appeal approval).
3. Webhook message (no interaction metadata): delete it and the webhook (Manage Webhooks), no event row.
4. Bot posting on its own: delete it, never kick, no event row.
For 3 and 4: one log embed "Honeypot: webhook/bot message removed" and one `honeypot_foreign_message` audit row
per source per 30 s cooldown; later messages are just deleted.

### Name scrambling (2026-10-28, migration 029)
When `scramble_enabled`, every `scramble_interval_hours` (1-168, default 6; off by default) the bot renames the trap
channel and its CURRENT category to random gibberish, keeping the decoration around the words (leading/trailing
non-word characters: `🚫┊do-not-type` -> `🚫┊xq7-vlrm`, `───⋅ Ignore ⋅───` -> `───⋅ Qvex ⋅───`), so spam tools
cannot learn to skip the trap by name. The category is renamed only if the trap is its only channel. Schedule
anchor `last_scrambled_at` (restart-safe; NULL = due at the next 5-minute check). Manual scrambles are refused within
10 minutes of the last one (`too_soon`; Discord allows 2 renames per channel per 10 minutes). Each scramble writes a
`honeypot_scramble` audit row and publishes `config_changed`. Implemented in `HoneypotService.scramble_names` /
`scramble_if_due`; details in ARCHITECTURE section 19.

Exemptions (never punished; for an app reply these apply to the member who ran it): members with any configured staff role
(`STAFF_ROLE_IDS`/`PermissionService.is_staff`), members with Administrator / Manage Messages / Ban Members.
Guard against double-processing: an in-flight per-user set/lock so a burst of messages from one user
acts exactly once. Only act in the configured guild.

`APPEAL_BASE_URL` = new env var, default `https://dash.idlesys.xyz`.

## Actions
`action` ∈ `ban | softban | kick`. Default `ban`. Defaults: `dm_before=true`, `appeal_enabled=true`,
`delete_message_days=1` (0–7).

## Database — migration 025 `honeypot` (+ 026 appeal `verified`, + 028 `honeypot_events.via_app_id INTEGER`, `via_app_name TEXT`, + 029 `honeypot_config.scramble_enabled INTEGER DEFAULT 0`, `scramble_interval_hours INTEGER DEFAULT 6`, `last_scrambled_at TEXT`, + 030 `honeypot_config.rejoin_invite_url TEXT`, `invite_in_removal_dm INTEGER DEFAULT 0`, `honeypot_appeals.outcome_dm_sent INTEGER`, `rejoined_at TEXT`, `verified_via TEXT`; from 030 `honeypot_appeals.token` holds `sha256:<hex>` of the link token, never the token; + 031 `is_test` on events and appeals for Debug-page appeal-flow test cases, excluded from stats/feed/counts)
Register in `main.py` MIGRATIONS as `(25, "honeypot", ".../025_honeypot.sql")`. Tables:
- `honeypot_config(guild_id PK, enabled, channel_id, category_id, action, dm_before, appeal_enabled,
  delete_message_days, log_channel_id, warning_message_id, stats_message_id, updated_at)`
- `honeypot_events(id, guild_id, user_id, username, display_name, avatar_url, channel_id, content, action,
  action_result, errors(JSON text), dm_sent, account_created_at, joined_at, seconds_since_join,
  had_default_avatar, flag_link, flag_invite, flag_everyone, flag_attachment, created_at)`
- `honeypot_appeals(id, guild_id, user_id, token UNIQUE, username, name, message, status
  ('invited'|'pending'|'approved'|'denied'), catch_id, created_at, submitted_at, reviewed_by, reviewed_at, review_note, verified)`
(`verified` INTEGER NOT NULL DEFAULT 1, migration 026: 0 = filed with only a typed user id, no DM token.)
Snowflake columns are INTEGER in SQLite (bot convention). Timestamps ISO-8601 UTC strings.

## Internal API (bot process, `internal_api/routes/honeypot.py`, guarded by `require_internal_secret`)
All under `/guilds/{guild_id}/honeypot`. Snowflakes go out as STRINGS (use `stringify_snowflakes`
and check `internal_api/snowflakes.py` — add any new key names to its allowlist rather than
hand-stringifying).

| Method | Path | Body / query | Response |
|---|---|---|---|
| GET | `/config` | – | `{configured, enabled, channel_id, category_id, channel_exists, action, dm_before, appeal_enabled, delete_message_days, log_channel_id, warning_message_id, stats_message_id, appeal_base_url, scramble_enabled, scramble_interval_hours, last_scrambled_at, next_scramble_at, channel_name, category_name}` (when unconfigured: `configured:false` + defaults; `channel_name`/`category_name` are the live Discord names or null) |
| PUT | `/config` | partial `{enabled?, action?, dm_before?, appeal_enabled?, delete_message_days?, log_channel_id?, scramble_enabled?, scramble_interval_hours? (1-168)}` | config (same shape). 400 on invalid values |
| POST | `/scramble` | `{actor_id}` | `{config, channel:{old,new}, category:{old,new}\|null, warnings:[str]}`; 409 `too_soon` / `rate_limited` / `channel_missing`, 403 when Discord refuses (Manage Channels), 400 not configured |
| POST | `/setup` | `{actor_id, category_name?, channel_name?, position_top?=true, action?, dm_before?, appeal_enabled?}` | `{config, warnings:[str]}`; creates category+channel (`@everyone` view+send, no attachments/embeds needed), posts the warning embed + the "Caught so far" embed, stores config, `enabled=true`. 409 `{"detail":"already_configured"}` if a configured channel still exists. Defaults: category `⚠️ Do Not Type`, channel `🚫┃do-not-type`. Bot needs Manage Channels — surface Forbidden as 502-ish with a clear detail |
| GET | `/catches` | `?limit=50&before_id=` | `{catches:[catch]}` newest first |
| GET | `/stats` | – | stats object below |
| GET | `/appeals` | `?status=pending\|approved\|denied\|invited\|all` (default `all`) | `{appeals:[appeal]}` newest first |
| POST | `/appeals/{appeal_id}/approve` | `{reviewer_id, note?}` | `{ok, unbanned, dm_sent}` — unbans via ModerationService (404 appeal, 409 if not `pending`); treats "user not banned" (NotFound on `fetch_ban`) as success with `unbanned:false`; if the user is banned but the ban reason does not start with "Honeypot" → 409 `{"detail":"not_a_honeypot_ban"}` (nobody unbanned, appeal stays pending); best-effort DM telling them they can rejoin |
| POST | `/appeals/{appeal_id}/deny` | `{reviewer_id, note?}` | `{ok}` |
| GET | `/appeals/lookup/{token}` | – | `{user_id, username, status, submitted_at, reviewed_at, rejoin_invite_url (approved only), rejoined}`; 404 `{"detail":"invalid_token"}` (looked up by the token's SHA-256) |
| GET | `/appeals/for-user/{user_id}` | – | `{user_id, eligible, appeal: {status, submitted_at, reviewed_at, rejoin_invite_url, rejoined}\|null}`. ONLY called by the dashboard for the id its Discord sign-in proved |
| POST | `/appeals/submit-verified` | `{user_id, username?, name, message}` | `{ok, status:"pending"}`; 404 `not_eligible`, 409 `already_submitted` (identity proven, so honest errors) |
| POST | `/appeals/rejoin` | `{user_id, access_token}` (guilds.join token, used once, never stored) | `{result: "joined"\|"already_member"}`; 409 `not_approved`/`still_banned`/`too_many_guilds`/`login_expired`, 403 missing Create Invite |
| POST | `/invite` | `{actor_id, channel_id}` | config; creates a permanent invite and stores it as `rejoin_invite_url` |
| ~~POST~~ | ~~`/appeals/submit`~~ | REMOVED 2026-10-29 (appeals require Discord sign-in; use `/appeals/submit-verified`). Historical behaviour: | With a token: must match an `invited` appeal → `{ok:true, status:"pending"}` (verified); unknown token → 404 `not_eligible`, already used → 409 `already_submitted`. **Without a token** (only `user_id`): ALWAYS `200 {"ok":true}` in every case (not banned, banned, already submitted, garbage id) so it cannot enumerate who was banned. A row is created only if the user has a non-failed `ban` honeypot_events row and no already-submitted appeal covers it; it is stored `status=pending, verified=0` (an untouched `invited` DM row is NOT consumed). |

`catch` = `{id, user_id, username, display_name, avatar_url, channel_id, content, action, action_result, errors:[str], dm_sent, account_created_at, joined_at, seconds_since_join, had_default_avatar, flags:{link,invite,everyone,attachment}, created_at, appeal_status|null, via_app:{id (string), name}|null}`

`appeal` = `{id, user_id, username, name, message, status, catch_id, created_at, submitted_at, reviewed_by, reviewed_at, review_note, verified:bool}` (`verified:false` → the staff queue shows an "Unverified — submitted without the DM link" badge)

`stats` =
```
{
  totals: {all, last_24h, last_7d, last_30d},
  by_action: {ban, softban, kick},
  by_result: {ok, partial, failed},
  timeline_30d: [{date:"YYYY-MM-DD", count}],      // every day of the last 30, zero-filled
  by_hour_utc: [24 ints],
  account_age: {avg_days, median_days, under_7d_pct, under_30d_pct},        // nulls when no data
  time_to_catch: {median_seconds, under_60s_pct, under_1h_pct},             // seconds joined→caught; nulls when no data
  signals: {default_avatar_pct, link_pct, invite_pct, everyone_pct, attachment_pct, dm_delivered_pct},
  top_messages: [{preview, count}],                // top 5 repeated normalised contents (count >= 2)
  appeals: {total, invited, pending, approved, denied, false_positive_pct},  // false_positive_pct = approved / total bans
  first_catch_at, last_catch_at
}
```
`*_pct` are floats 0–100 (1 decimal) or `null`.

## Slash commands (bot, `cogs/honeypot.py`, group `/honeypot`)
`setup` (creates like the dashboard), `status` (config + totals), `stats` (embed of the stats above),
`action <ban|softban|kick>`, `enable` / `disable`, `appeals` (list pending), `scramble [mode now|on|off] [every_hours]`. Gate with the bot's existing
permission model (`ban_members` grant / staff), same helper other cogs use. Both the slash commands and the
internal API MUST call the same `HoneypotService` methods — no duplicated logic.

## Dashboard backend (`server/routes/honeypot.py`, `server/routes/appeals.py`)
Thin clients through `server/services/internal_client.py` (the only module allowed to call the internal API),
same 502 `{"error":"internal_api_unavailable"}` translation as `server/routes/moderation.py`.

Authenticated (`/api/honeypot/...`):
- `GET config`, `GET catches`, `GET stats`, `GET appeals` → permission `moderate_members`
- `PUT config`, `POST setup`, `POST scramble`, `POST appeals/{id}/approve`, `POST appeals/{id}/deny` → permission `ban_members`
- actor/reviewer id ALWAYS comes from `session.discord_user_id`, never the body.

PUBLIC appeal sign-in with Discord (`/api/public/appeal-auth/...`, 2026-10-28): `login?intent=appeal|rejoin`, `callback`,
`me`, `submit`, `logout`. Discord tokens are never stored: used once (identity; for rejoin also the add-member call) and
revoked; only a signed 30-minute identity cookie remains. Details in ARCHITECTURE section 19 "Appeal outcomes".

PUBLIC, unauthenticated (`/api/public/appeals/...`), for people who are banned and cannot log in:
- `GET /api/public/appeals/{token}` → `{user_id, username, status}` (proxy of lookup)
- `POST /api/public/appeals` → since 2026-10-29 always `403 {"error":"signin_required"}` for every body (appeals require Discord sign-in via `/api/public/appeal-auth/submit`); it no longer calls the bot. (Before: proxy of submit; token-less bodies got a byte-identical `200 {"ok":true}`.)
- Rate limited with slowapi (use the existing `limiter` in `server/auth/oauth.py`; key on
  `CF-Connecting-IP` when present — requests arrive through a Cloudflare Tunnel — falling back to the
  socket address), 5/minute on POST and 30/minute on GET. Pydantic-validate lengths. Never echo internal
  error detail to the public; map everything to `{"error":"not_eligible"|"already_submitted"|"rate_limited"|"unavailable"}`.

## Dashboard frontend
- `/honeypot` (protected, nav link + command palette entry, only shown with `moderate_members`):
  - Not configured → "Create honeypot" card (category name, channel name, action, DM toggle, appeal toggle) → POST setup.
  - Configured → status header (enabled toggle, channel link/exists warning), settings form (action, DM before, appeals, delete days), "Name scrambling" card (on/off, interval, Scramble now),
    **live catches feed rendered as Discord-style embed cards** (reuse `components/embed/EmbedPreview.tsx` if it fits),
    **"Stats" button → dialog** with the stats above (headline numbers, 30-day timeline chart, hour-of-day chart,
    signal bars, top repeated messages, appeal/false-positive numbers) — use Recharts (already a dependency? check package.json; add if not).
  - Appeals queue tab: pending appeals with name / user id / message / the original catch (message content), Approve (unban) / Deny with optional note.
- `/appeal` and `/appeal/:token` (PUBLIC — outside `RequireAuth` and outside `AppShell`; standalone clean page):
  since 2026-10-29: Discord sign-in required; the form (Name, "What happened?") only appears once signed in, and the DM-link page is a read-only status view. Historical spec (no longer built): form with Name, Discord User ID (prefilled + locked when a valid token is present), "What happened?" message.
  It had an **"How do I find my User ID?"** helper: Discord → User Settings → Advanced → enable Developer Mode →
  close settings → right-click your own profile/avatar (or username in a chat) → **Copy User ID**; mobile: Settings → Advanced → Developer Mode,
  then tap your profile → three dots → Copy User ID. States: loading, invalid/expired link, already submitted, success, error, rate-limited.
