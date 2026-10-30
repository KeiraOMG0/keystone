/**
 * TypeScript types mirroring the REAL dashboard-backend response shapes,
 * field-for-field, as read from:
 *   - server/routes/me.py       (GET /api/me)
 *   - server/routes/stats.py    (GET /api/dashboard/stats, a raw passthrough
 *                                 of internal_api's GET /guilds/{id}/stats/live)
 *   - server/routes/moderation.py (GET/POST/DELETE /api/moderation/warnings*,
 *                                 a passthrough of internal_api's moderation
 *                                 routes, which wrap database/repositories/
 *                                 warning_repo.py's `SELECT * FROM warnings`)
 *   - server/permissions/guards.py (403/401 error body shapes)
 *
 * Do not add fields here that the backend doesn't actually send.
 */

// ---------------------------------------------------------------------------
// GET /api/me
// ---------------------------------------------------------------------------

/** The authenticated-and-a-guild-member 200 response body from GET /api/me. */
export interface MeResponse {
  user: {
    id: string
    username: string
    /** Discord avatar hash, or null if the user has no custom avatar set. */
    avatar: string | null
  }
  guild_id: string
  role_ids: string[]
  /** Bulk permission-check result: permission string -> allowed. */
  permissions: Record<string, boolean>
  /** True only for OWNER_ID: shows the hidden Debug page (server enforces it with 404s). */
  is_owner?: boolean
  /**
   * True for the owner and STAFF_ROLE_IDS members (what require_staff allows).
   * Missing (older backend) is treated as false. Drives the staff-only nav.
   */
  is_staff?: boolean
}

// ---------------------------------------------------------------------------
// GET /api/dashboard/stats
// ---------------------------------------------------------------------------

/**
 * Raw passthrough of internal_api's GET /guilds/{guild_id}/stats/live.
 * server/routes/stats.py returns this dict as-is, no transformation.
 */
export interface DashboardStatsResponse {
  /** Discord snowflake — always a string end-to-end (see this file's module
   *  docstring on why: a bare JSON number here would silently lose
   *  precision the moment the browser's JSON.parse reads it). */
  guild_id: string
  total: number
  online: number | null
  idle: number | null
  dnd: number | null
  offline: number | null
  presence_available: boolean
}

// ---------------------------------------------------------------------------
// Error body shapes (401 / 403), from server/permissions/guards.py and
// server/routes/me.py. These are the values inside FastAPI's HTTPException
// `detail`, which our api client surfaces as `ApiError.body`.
// ---------------------------------------------------------------------------

export interface UnauthenticatedError {
  error: "unauthenticated"
}

export interface NotAMemberError {
  error: "not_a_member"
}

export interface ForbiddenError {
  error: "forbidden"
  required_permission: string
  message: string
}

export interface InternalApiUnavailableError {
  error: "internal_api_unavailable"
}

export type KnownErrorBody =
  | UnauthenticatedError
  | NotAMemberError
  | ForbiddenError
  | InternalApiUnavailableError

// ---------------------------------------------------------------------------
// GET/POST/DELETE /api/moderation/warnings*
// ---------------------------------------------------------------------------

/**
 * A single warning row, field-for-field from `warnings` table columns
 * (database/repositories/warning_repo.py's `SELECT * FROM warnings`, passed
 * through internal_api/routes/moderation.py and server/routes/moderation.py
 * without transformation). SQLite has no strict integer typing enforced at
 * the FastAPI boundary, but these all come back as JSON numbers/strings from
 * Pydantic's default int/str serialization.
 */
export interface Warning {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  /** Discord snowflake — string end-to-end, see this file's module docstring. */
  guild_id: string
  /** Discord snowflake (the warned user's Discord user ID). */
  user_id: string
  /** Discord snowflake (the issuing moderator's Discord user ID). */
  moderator_id: string
  reason: string | null
  created_at: string
}

/** GET /api/moderation/warnings/{user_id} response body. */
export interface WarningsResponse {
  /** Discord snowflake. */
  user_id: string
  warnings: Warning[]
}

/**
 * GET /api/moderation/warnings/recent response body — most recent warnings
 * across the whole guild (any user), newest first. Populates the moderation
 * page's landing state. Same `Warning` row shape as WarningsResponse, just
 * without the `user_id` filter wrapper since results span multiple users.
 */
export interface RecentWarningsResponse {
  warnings: Warning[]
}

/** POST /api/moderation/warnings response body. */
export interface AddWarningResponse {
  warning_id: number
}

/** DELETE /api/moderation/warnings/{warning_id} response body. */
export interface RemoveWarningResponse {
  removed: boolean
}

export interface WarningNotFoundError {
  error: "warning_not_found"
}

// ---------------------------------------------------------------------------
// GET /api/moderation/history/{userId}
// POST /api/moderation/kick|ban|unban|timeout|untimeout
//
// Field-for-field from database/repositories/mod_action_repo.py's
// `mod_actions` table, passed through internal_api/routes/moderation.py and
// server/routes/moderation.py without transformation. `extra_data` is
// parsed JSON server-side (e.g. {"warning_id": 12} for action_type "warn" /
// "warning_removed" rows) or null if the row had none.
// ---------------------------------------------------------------------------

export type ModActionType = "kick" | "ban" | "unban" | "timeout" | "untimeout" | "warn" | "warning_removed"

/** A single mod_actions row. */
export interface ModAction {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  /** Discord snowflake. */
  guild_id: string
  /** Discord snowflake. */
  user_id: string
  /** Discord snowflake. */
  moderator_id: string
  action_type: ModActionType
  reason: string | null
  /** Timeout only; null for every other action_type. */
  duration_minutes: number | null
  /** Timeout only (ISO8601 UTC); null for every other action_type. */
  expires_at: string | null
  /** Parsed JSON, e.g. {"warning_id": 12}; null if the row had none. */
  extra_data: Record<string, unknown> | null
  created_at: string
}

/** GET /api/moderation/history/{userId} response body. */
export interface ModHistoryResponse {
  /** Discord snowflake. */
  user_id: string
  actions: ModAction[]
}

/** POST /api/moderation/kick|ban|unban|timeout|untimeout response body. */
export interface ModActionResponse {
  action_id: number
}

export interface MemberNotFoundError {
  error: "member_not_found"
}

/** A single live guild.members match. */
export interface MemberSearchResult {
  /** Discord snowflake — string end-to-end, see this file's module docstring. */
  id: string
  username: string
  display_name: string
  avatar_url: string
}

/** GET /api/moderation/members/search response body. */
export interface MemberSearchResponse {
  members: MemberSearchResult[]
}

export interface NotBannedError {
  error: "not_banned"
}

export interface DiscordForbiddenError {
  error: "discord_forbidden"
}

// ---------------------------------------------------------------------------
// GET/POST /api/tickets*, /api/ticket-panels
//
// Field-for-field from database/repositories/ticket_repo.py's `tickets` /
// `ticket_panels` tables (database/migrations/sql/005_tickets.sql), passed
// through internal_api/routes/tickets.py and server/routes/tickets.py
// without transformation.
// ---------------------------------------------------------------------------

export type TicketStatus = "open" | "closed" | "archived"

/**
 * A single ticket row. `panel_purpose` is a LEFT JOIN of ticket_panels.purpose
 * (e.g. "support" | "verify") added by ticket_repo.py's list/get queries —
 * null if the panel was deleted. `claimed_by` / `closed_at` are null until
 * the ticket is claimed / closed respectively. `archived_at` (migration 023)
 * is null until the ticket is archived, and is the reference timestamp for
 * the 30-day auto-purge job. `control_message_id` (migration 023) is the
 * Discord message ID of the Close/Claim control message — not generally
 * useful client-side, included only because the route returns the row as-is.
 */
export interface Ticket {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  /** Discord snowflake. */
  guild_id: string
  /** Small DB autoincrement id — safe as a JS number. */
  panel_id: number
  /** Discord snowflake (the ticket's channel). */
  channel_id: string
  /** Discord snowflake (the member who opened the ticket). */
  opened_by: string
  /** Discord snowflake (the staff member who claimed it), or null if unclaimed. */
  claimed_by: string | null
  status: TicketStatus
  ticket_number: number
  opened_at: string
  closed_at: string | null
  archived_at: string | null
  /** Discord snowflake (the Close/Claim control message id), or null. */
  control_message_id: string | null
  panel_purpose: string | null
}

/** A single row from GET /api/tickets/{ticket_id}/messages — plain-text
 * chat-log capture (database/migrations/sql/023_ticket_claim_chatlog_purge.sql's
 * ticket_messages table). Never contains attachment or voice-message data. */
export interface TicketMessage {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  /** Small DB autoincrement id — safe as a JS number. */
  ticket_id: number
  /** Discord snowflake (the message author's Discord user ID). */
  author_id: string
  content: string
  sent_at: string
}

/** GET /api/tickets/{ticket_id}/messages response body. `enabled` reflects
 * config.TICKET_CHAT_LOGGING_ENABLED at request time — the dashboard should
 * show a "chat logging is disabled" indicator rather than treating an empty
 * `messages` array as "no messages yet" when this is false. */
export interface TicketMessagesResponse {
  enabled: boolean
  messages: TicketMessage[]
}

/** A ticket panel row (database/repositories/ticket_repo.py's ticket_panels table). */
export interface TicketPanel {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  /** Discord snowflake. */
  guild_id: string
  /** Discord snowflake. */
  channel_id: string
  /** Discord snowflake, or null if never posted. */
  message_id: string | null
  /** Small DB autoincrement id — safe as a JS number. */
  template_id: number | null
  /** Discord snowflake (category channel id), or null. */
  open_category_id: string | null
  /** Discord snowflake (category channel id), or null. */
  closed_category_id: string | null
  /** Discord snowflake (channel id), or null. */
  transcript_channel_id: string | null
  max_open_per_user: number
  naming_pattern: string
  button_label: string
  button_emoji: string | null
  /** Discord snowflake. */
  created_by: string
  created_at: string
  purpose: string
}

/** POST /api/tickets/{ticket_id}/close|claim|archive response body. */
export interface TicketActionResponse {
  success: boolean
}

export interface TicketNotFoundError {
  error: "ticket_not_found"
}

// ---------------------------------------------------------------------------
// GET/POST/DELETE /api/permissions/* — staff permission-grants management.
//
// Field-for-field from internal_api/routes/permissions.py, which wraps
// services.permission_service.PermissionService (grant/revoke/list_grants)
// and database/repositories/advanced_repo.py's PermissionRepository
// (`permission_grants` table, database/migrations/sql/006_advanced.sql).
// Passed through server/routes/permissions.py without transformation.
//
// This page is gated by require_staff (not require_permission) — see
// server/permissions/guards.py's require_staff docstring. A 403 from these
// routes carries required_permission: "staff" (a sentinel, not one of the 14
// VALID_PERMISSIONS strings) so Forbidden403 can still render generically.
// ---------------------------------------------------------------------------

/** A single live Discord role, from GET /api/permissions/roles (internal_api's
 *  GET /guilds/{guild_id}/roles, sourced directly from discord.Guild.roles —
 *  no DB query). `color` is a "#rrggbb" string, or null for the default
 *  (no-color) role. Sorted highest position first, matching Discord's own
 *  role list ordering. The @everyone role is excluded. */
export interface GuildRole {
  /** Discord snowflake. */
  id: string
  name: string
  color: string | null
  position: number
  managed: boolean
  /** True if this role is in DEFAULT_STAFF_ROLE_IDS (services/
   *  permission_service.py) — it already bypasses every has_permission()
   *  check via is_staff(), regardless of explicit grants. The matrix UI
   *  marks/disables these rows rather than implying grants control them. */
  is_default_staff: boolean
}

/** GET /api/permissions/roles response body. */
export interface RolesResponse {
  roles: GuildRole[]
}

/** A single row from the `permission_grants` table. */
export interface PermissionGrant {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  /** Discord snowflake. */
  guild_id: string
  /** Discord snowflake. */
  role_id: string
  permission: string
  /** Discord snowflake. */
  granted_by: string
  granted_at: string
}

/** GET /api/permissions/grants response body. `valid_permissions` is the
 *  full VALID_PERMISSIONS set from services/permission_service.py (currently
 *  14 strings) — the matrix's column list. */
export interface GrantsResponse {
  grants: PermissionGrant[]
  valid_permissions: string[]
}

/** POST /api/permissions/grants response body. */
export interface AddGrantResponse {
  granted: boolean
}

/** DELETE /api/permissions/grants response body. */
export interface RevokeGrantResponse {
  revoked: boolean
}

export interface InvalidGrantError {
  error: "invalid_grant"
}

// ---------------------------------------------------------------------------
// GET/POST/PATCH/DELETE /api/reaction-roles/panels* — self-roles panel
// builder dashboard page.
//
// Field-for-field from database/repositories/reaction_role_repo.py's
// `reaction_role_panels` / `reaction_role_entries` tables
// (database/migrations/sql/007_reaction_roles.sql), passed through
// internal_api/routes/reaction_roles.py and server/routes/reaction_roles.py
// without transformation. Gated server-side by the "manage_reaction_roles"
// permission — the same one cogs/reaction_roles.py's /reactionrole slash
// commands already require.
//
// `exclusive` is the pre-existing "unique role" concept: when true, picking
// one entry's role in this panel removes any other role from the SAME
// panel (radio-button style) — enforced live in cogs/reaction_roles.py's
// on_interaction button handler, not just stored inertly.
// ---------------------------------------------------------------------------

/** A single emoji -> role mapping row on a panel. */
export interface ReactionRoleEntry {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  /** Small DB autoincrement id — safe as a JS number. */
  panel_id: number
  emoji: string
  /** Discord snowflake. */
  role_id: string
  label: string | null
  position: number
}

/**
 * A reaction-role panel. `message_id` is null until the panel has been
 * posted to Discord at least once (see `usePostReactionRolePanel`).
 * `entries` is added by internal_api/routes/reaction_roles.py's list/get
 * endpoints (a join, not a raw table column) so the dashboard never needs a
 * second round trip to show a panel's mappings.
 */
export interface ReactionRolePanel {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  /** Discord snowflake. */
  guild_id: string
  /** Discord snowflake. */
  channel_id: string
  /** Discord snowflake, or null if never posted. */
  message_id: string | null
  /** Small DB autoincrement id — safe as a JS number. */
  template_id: number | null
  title: string
  exclusive: boolean | number
  /** Discord snowflake. */
  created_by: string
  created_at: string
  entries: ReactionRoleEntry[]
}

/** POST /api/reaction-roles/panels response body. */
export interface CreateReactionRolePanelResponse {
  panel_id: number
}

/** POST /api/reaction-roles/panels/{id}/post response body. */
export interface PostReactionRolePanelResponse {
  /** Discord snowflake, or null. */
  message_id: string | null
}

/** POST /api/reaction-roles/panels/{id}/entries response body. */
export interface AddReactionRoleEntryResponse {
  entry_id: number
}

export interface PanelNotFoundError {
  error: "panel_not_found"
}

export interface EntryNotFoundError {
  error: "entry_not_found"
}

// ---------------------------------------------------------------------------
// GET /api/channels — text-channel picker, added for the reaction-role
// panel-creation form (internal_api's GET /guilds/{guild_id}/channels,
// sourced directly from discord.Guild.text_channels — no DB query).
// ---------------------------------------------------------------------------

export interface GuildChannel {
  /** Discord snowflake. */
  id: string
  name: string
  position: number
}

// ---------------------------------------------------------------------------
// GET/POST/PATCH/PUT/DELETE /api/templates* — message template editor
// (embed builder). Field-for-field from database/migrations/sql/002_templates.sql's
// `message_templates` / `template_embeds` / `embed_fields` tables, passed
// through internal_api/routes/templates.py and server/routes/templates.py.
// Gated server-side by the "manage_templates" permission.
// ---------------------------------------------------------------------------

/** A single field row on an embed. */
export interface EmbedField {
  id?: number
  embed_id?: number
  position?: number
  name: string
  value: string
  inline: boolean | number
}

export type EmbedTimestampMode = "none" | "now"

/** A single embed row (template_embeds table), with its nested fields. */
export interface TemplateEmbed {
  id?: number
  template_id?: number
  position?: number
  title: string | null
  description: string | null
  /** Decimal integer color (e.g. 0x5865F2 = 5793266), matching discord.Color
   *  and the `color INTEGER` column — never a hex string at this boundary. */
  color: number | null
  url: string | null
  footer_text: string | null
  footer_icon_url: string | null
  thumbnail_url: string | null
  image_url: string | null
  author_name: string | null
  author_icon_url: string | null
  author_url: string | null
  timestamp_mode: EmbedTimestampMode
  fields: EmbedField[]
}

/** A message_templates row without nested embeds — the list-view shape from
 *  GET /api/templates. */
export interface TemplateSummary {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  /** Discord snowflake. */
  guild_id: string
  name: string
  description: string | null
  category: string | null
  content: string | null
  /** Discord snowflake. */
  created_by: string
  /** Discord snowflake, or null if never edited after creation. */
  updated_by: string | null
  created_at: string
  updated_at: string
}

/** GET /api/templates/{id} response body — the full template with nested
 *  embeds+fields (database/repositories/template_repo.py's get_full). */
export interface TemplateFull extends TemplateSummary {
  embeds: TemplateEmbed[]
}

/** POST /api/templates response body. */
export interface CreateTemplateResponse {
  template_id: number
}

export interface TemplateNotFoundError {
  error: "template_not_found"
}

export interface ValidationError {
  error: "validation_error"
  detail: string
}

// ---------------------------------------------------------------------------
// POST /api/ticket-panels, GET /api/categories, GET /api/ticket-panel-channels
// — ticket panel builder. Wraps internal_api/routes/ticket_panels.py, which
// calls the real TicketService.create_panel + TicketService.post_panel so a
// dashboard-created panel actually posts a live Discord message. Gated
// server-side by "manage_tickets" (same permission as the rest of the
// tickets page).
// ---------------------------------------------------------------------------

/** A category channel, from GET /api/categories (discord.Guild.categories). */
export interface GuildCategory {
  /** Discord snowflake. */
  id: string
  name: string
  position: number
}

export interface CreateTicketPanelRequest {
  /** Discord snowflake — sent as a string, never wrapped in Number(). */
  channel_id: string
  template_name?: string | null
  /** Discord snowflake, or null. */
  open_category_id?: string | null
  /** Discord snowflake, or null. */
  closed_category_id?: string | null
  /** Discord snowflake, or null. */
  transcript_channel_id?: string | null
  max_open_per_user: number
  naming_pattern: string
  button_label: string
  button_emoji?: string | null
  purpose: string
}

/**
 * POST /api/ticket-panels response body. `message_id` is null and
 * `post_errors` non-empty if the panel row was created but posting the
 * live Discord message failed (e.g. missing channel permissions) — the
 * panel still exists and can be viewed/retried, it just isn't live yet.
 */
export interface CreateTicketPanelResponse {
  /** Small DB autoincrement id — safe as a JS number. */
  panel_id: number
  /** Discord snowflake, or null if the post step failed. */
  message_id: string | null
  post_errors: string[]
  panel: TicketPanel | null
}

// ---------------------------------------------------------------------------
// GET/PUT /api/qotd/config, GET /api/qotd/history, GET /api/qotd/models,
// GET /api/qotd/fallback-questions, POST /api/qotd/generate|post — QOTD
// management dashboard page.
//
// Field-for-field from the `qotd_config` / `qotd_history` tables
// (database/migrations/sql/009_qotd.sql, 010_qotd_role.sql,
// 011_qotd_staff_ping.sql, 016_qotd_approval_channel.sql), passed through
// internal_api/routes/qotd.py and server/routes/qotd.py without
// transformation. Gated server-side by the "manage_qotd" permission — the
// same one cogs/qotd.py's `/qotd` slash-command group already requires.
//
// QOTD already runs a fully automatic daily scheduler in the bot process
// (cogs/qotd.py's `_scheduler` tasks.loop, driven by this same
// `qotd_config` row): when `enabled` and the current UTC time matches
// post_hour:post_minute, it generates a question and sends a staff-lounge
// APPROVAL embed (approve/deny/regenerate/custom buttons), not a direct
// public post. This page edits that config and offers a manual
// generate-preview-then-post flow (POST /generate never posts; POST /post
// always does) — it does not add a second/competing scheduler.
// ---------------------------------------------------------------------------

/** A single `qotd_config` row (one per guild), or null if QOTD has never
 *  been configured for this guild yet. */
export interface QotdConfig {
  /** Discord snowflake. */
  guild_id: string
  /** Discord snowflake, or null if never configured. */
  channel_id: string | null
  post_hour: number
  post_minute: number
  enabled: boolean | number
  model: string
  use_emojis: boolean | number
  /** Discord snowflake, or null. */
  ping_role_id: string | null
  /** Discord snowflake, or null. */
  staff_ping_role_id: string | null
  /** Discord snowflake, or null. */
  approval_channel_id: string | null
  updated_at: string
}

/** GET /api/qotd/config response body. `pending_approval` mirrors
 *  cogs/qotd.py's in-memory `_pending` dict — true if the scheduler already
 *  sent today's approval embed to the staff lounge and it hasn't been
 *  resolved (approved/denied/timed out) yet. */
export interface QotdConfigResponse {
  config: QotdConfig | null
  last_posted_at: string | null
  pending_approval: boolean
}

/** Body for PUT /api/qotd/config. All fields optional — only send what
 *  changed. `clear_ping_role` / `clear_staff_ping_role` /
 *  `clear_approval_channel` explicitly null out that column (mirrors
 *  database/repositories/qotd_repo.py's upsert_config kwargs — a plain
 *  `undefined`/omitted field leaves the existing value untouched, matching
 *  `/qotd setapprovalchannel` with no channel argument). */
export interface UpsertQotdConfigInput {
  /** Discord snowflake — sent as a string, never wrapped in Number(). */
  channel_id?: string | null
  post_hour?: number | null
  post_minute?: number | null
  enabled?: boolean | null
  model?: string | null
  use_emojis?: boolean | null
  /** Discord snowflake, or null. */
  ping_role_id?: string | null
  clear_ping_role?: boolean
  /** Discord snowflake, or null. */
  staff_ping_role_id?: string | null
  clear_staff_ping_role?: boolean
  /** Discord snowflake, or null. */
  approval_channel_id?: string | null
  clear_approval_channel?: boolean
}

export type QotdSource = "ai" | "fallback" | "custom"

/** A single `qotd_history` row. */
export interface QotdHistoryEntry {
  /** Small DB autoincrement id — safe as a JS number. */
  id: number
  question: string
  source: QotdSource | string
  posted_at: string
  /** Discord snowflake, or null. */
  message_id: string | null
  /** Discord snowflake, or null. */
  channel_id: string | null
}

/** GET /api/qotd/history response body. */
export interface QotdHistoryResponse {
  history: QotdHistoryEntry[]
}

/** One entry in the Ollama model catalogue (cogs/qotd.py's AVAILABLE_MODELS —
 *  static, not stored in the DB; `model` on QotdConfig just holds one of
 *  these `id` strings). */
export interface QotdModelOption {
  id: string
  label: string
  desc: string
}

/** GET /api/qotd/models response body. */
export interface QotdModelsResponse {
  models: QotdModelOption[]
}

/** GET /api/qotd/fallback-questions response body — the hardcoded
 *  FALLBACK_QUESTIONS pool from services/qotd_service.py, read-only (still a
 *  Python constant, not a DB table). */
export interface QotdFallbackQuestionsResponse {
  questions: string[]
}

/** POST /api/qotd/generate response body. Never posts anything — this is a
 *  preview only, mirroring `/qotd preview`. `errors` is non-empty only when
 *  `source === "fallback"` (each AI attempt's failure reason). */
export interface GenerateQotdResponse {
  question: string
  source: QotdSource | string
  errors: string[]
}

/** Body for POST /api/qotd/post. `source` should be whatever POST /generate
 *  returned (or "custom" for a staff-written question) — it's stored as-is
 *  on the new qotd_history row. */
export interface PostQotdInput {
  question: string
  source: QotdSource | string
  /** Discord snowflake — sent as a string, never wrapped in Number(). This
   *  is the exact field that caused the confirmed production bug: sending
   *  Number(channelId) here silently corrupted the real #qotd channel id
   *  (987654321098765432) to 987654321098765400, which the backend
   *  correctly 404'd as channel_not_found since no such channel exists. */
  channel_id: string
  use_emojis?: boolean
  /** Discord snowflake, or null/omitted for no ping. */
  ping_role_id?: string | null
}

/** POST /api/qotd/post response body. */
export interface PostQotdResponse {
  /** Discord snowflake. */
  message_id: string
  /** Discord snowflake. */
  channel_id: string
}

export interface ChannelNotFoundError {
  error: "channel_not_found"
}

// Settings types live in src/api/settings.ts (GET/PUT /api/settings).

// ---------------------------------------------------------------------------
// POST /api/resolve — batch Discord-ID -> display-metadata resolver, used by
// nearly every dashboard page to turn raw snowflakes into names/avatars/
// colors instead of printing bare IDs. Field-for-field from
// internal_api/routes/resolve.py, passed through server/routes/resolve.py
// without transformation. Gated server-side by "holds any bot permission"
// (staff, or any one permission grant), not a narrow feature permission.
//
// Users who are no longer in the server come back from the bot's user cache
// (user_cache.db) with `departed: true` plus their last known name/avatar and
// membership status, so old history still renders a name. A null entry means
// the id didn't resolve at all (never cached and Discord's fetch_user failed);
// that is expected/non-error data, not a fetch failure.
// ---------------------------------------------------------------------------

/** Membership status of a cached user who is not in the server right now. */
export type DepartedStatus = "left" | "kicked" | "banned" | "unbanned"

export interface ResolvedUser {
  /** Discord snowflake. */
  id: string
  username: string
  display_name: string
  avatar_url: string
  /** Only on users who are NOT in the server (from the user cache). Members omit every field below. */
  departed?: true
  status?: DepartedStatus
  /** ISO UTC; null when unknown (e.g. only known from the ban list or fetch_user). */
  departed_at?: string | null
  /** When `status` last changed (e.g. the unban time for "unbanned"). */
  status_changed_at?: string | null
  /** Discord snowflake of the moderator who kicked/banned, when known. */
  departed_by_id?: string | null
  /** "moderator" | "bot" | "detected_at_startup" (left while the bot was offline; date approximate) | null */
  departure_source?: string | null
  global_name?: string | null
  nick?: string | null
  /** '#rrggbb' of their top coloured role when last seen, or null. */
  color?: string | null
  bot?: boolean
  joined_at?: string | null
  first_seen?: string | null
  last_seen?: string | null
  /** When the cached profile was last refreshed. */
  cached_at?: string | null
}

/** GET /api/user-cache/{id} (any bot permission); DELETE (staff only) forgets it. */
export interface CachedUserResponse {
  user: ResolvedUser
  in_guild: boolean
}

export interface ResolvedRole {
  /** Discord snowflake. */
  id: string
  name: string
  color: string | null
}

export interface ResolvedChannel {
  /** Discord snowflake. */
  id: string
  name: string
}

/** POST /api/resolve response body. Keyed by the string form of each
 *  requested id; a key is present with a null value when that id could not
 *  be resolved in this guild. */
export interface ResolveResponse {
  users: Record<string, ResolvedUser | null>
  roles: Record<string, ResolvedRole | null>
  channels: Record<string, ResolvedChannel | null>
}

// ---------------------------------------------------------------------------
// Honeypot + ban appeals (docs/honeypot_contract.md). Snowflakes are strings.
// ---------------------------------------------------------------------------

export type HoneypotAction = "ban" | "softban" | "kick"
export type HoneypotActionResult = "ok" | "partial" | "failed"
export type AppealStatus = "invited" | "pending" | "approved" | "denied"

export interface HoneypotConfig {
  configured: boolean
  enabled: boolean
  channel_id: string | null
  category_id: string | null
  channel_exists: boolean
  action: HoneypotAction
  dm_before: boolean
  appeal_enabled: boolean
  delete_message_days: number
  log_channel_id: string | null
  warning_message_id: string | null
  stats_message_id: string | null
  appeal_base_url: string
  /** Name scrambling (migration 029). */
  scramble_enabled: boolean
  scramble_interval_hours: number
  last_scrambled_at: string | null
  /** null while off, or when never scrambled yet (the next 5-minute check does it). */
  next_scramble_at: string | null
  /** Live Discord names; null when the channel/category is missing. */
  channel_name: string | null
  category_name: string | null
  /** Rejoin link (migration 030): shown on approved appeals; optionally in kick/softban DMs. */
  rejoin_invite_url: string | null
  invite_in_removal_dm: boolean
}

export type HoneypotConfigUpdate = Partial<
  Pick<
    HoneypotConfig,
    | "enabled"
    | "action"
    | "dm_before"
    | "appeal_enabled"
    | "delete_message_days"
    | "scramble_enabled"
    | "scramble_interval_hours"
    | "invite_in_removal_dm"
  >
> & { log_channel_id?: string | null; rejoin_invite_url?: string | null }

/** POST /api/honeypot/scramble */
export interface HoneypotScrambleResponse {
  config: HoneypotConfig
  channel: { old: string; new: string }
  /** null when the category was not renamed (see warnings). */
  category: { old: string; new: string } | null
  warnings: string[]
}

export interface HoneypotSetupInput {
  category_name?: string
  channel_name?: string
  position_top?: boolean
  action?: HoneypotAction
  dm_before?: boolean
  appeal_enabled?: boolean
}

export interface HoneypotSetupResponse {
  config: HoneypotConfig
  warnings: string[]
}

export interface HoneypotCatch {
  id: number
  user_id: string
  username: string
  display_name: string | null
  avatar_url: string | null
  channel_id: string
  content: string
  action: HoneypotAction
  action_result: HoneypotActionResult
  errors: string[]
  dm_sent: boolean
  account_created_at: string | null
  joined_at: string | null
  seconds_since_join: number | null
  had_default_avatar: boolean
  flags: { link: boolean; invite: boolean; everyone: boolean; attachment: boolean }
  created_at: string
  appeal_status: AppealStatus | null
  /** Set when an app posted the message for this member (they ran its command); snowflake id as a string. */
  via_app?: { id: string; name: string | null } | null
}

export interface HoneypotCatchesResponse {
  catches: HoneypotCatch[]
}

export interface HoneypotStats {
  totals: { all: number; last_24h: number; last_7d: number; last_30d: number }
  by_action: { ban: number; softban: number; kick: number }
  by_result: { ok: number; partial: number; failed: number }
  timeline_30d: { date: string; count: number }[]
  by_hour_utc: number[]
  account_age: {
    avg_days: number | null
    median_days: number | null
    under_7d_pct: number | null
    under_30d_pct: number | null
  }
  time_to_catch: { median_seconds: number | null; under_60s_pct: number | null; under_1h_pct: number | null }
  signals: {
    default_avatar_pct: number | null
    link_pct: number | null
    invite_pct: number | null
    everyone_pct: number | null
    attachment_pct: number | null
    dm_delivered_pct: number | null
  }
  top_messages: { preview: string; count: number }[]
  appeals: {
    total: number
    invited: number
    pending: number
    approved: number
    denied: number
    false_positive_pct: number | null
  }
  first_catch_at: string | null
  last_catch_at: string | null
}

export interface HoneypotAppeal {
  id: number
  user_id: string
  username: string | null
  name: string | null
  message: string | null
  status: AppealStatus
  catch_id: number | null
  created_at: string
  submitted_at: string | null
  reviewed_by: string | null
  reviewed_at: string | null
  review_note: string | null
  /** false = filed with only a typed user id (no DM link token): identity is unconfirmed. */
  verified: boolean
  /** How identity was confirmed (migration 030). */
  verified_via: "dm_link" | "discord_login" | null
  /** Outcome DM: null = not tried, false = failed (usual after a ban), true = delivered. */
  outcome_dm_sent: boolean | null
  /** When they used "Rejoin server" after approval. */
  rejoined_at: string | null
  /** Created on the Debug page to test the appeal flow (no real ban). */
  is_test: boolean
}

/** GET /api/debug/appeal-test (owner only). */
export interface AppealTestCase {
  catch_id: number
  user_id: string
  username: string | null
  created_at: string
  appeal_id: number | null
  status: AppealStatus | null
  verified_via: string | null
  rejoined_at: string | null
}

export type AppealTestState = "eligible" | "pending" | "approved"

export interface HoneypotAppealsResponse {
  appeals: HoneypotAppeal[]
}

export interface AppealApproveResponse {
  ok: boolean
  unbanned: boolean
  dm_sent: boolean
}

/** Public appeal API (no auth). Errors arrive as {"error": code}. */
export type PublicAppealErrorCode =
  | "not_eligible"
  | "already_submitted"
  | "rate_limited"
  | "unavailable"
  | "invalid_request"
  | "not_signed_in"
  | "signin_required"

/** The person's own appeal outcome (never the reviewer or staff note). */
export interface PublicAppealOutcome {
  status: AppealStatus
  submitted_at: string | null
  reviewed_at: string | null
  /** Only when approved and staff set a rejoin link. */
  rejoin_invite_url: string | null
  rejoined: boolean
}

export interface PublicAppealLookup extends PublicAppealOutcome {
  user_id: string
  username: string | null
}

/** GET /api/public/appeal-auth/me */
export type PublicAppealMe =
  | { signed_in: false }
  | {
      signed_in: true
      user: { id: string; username: string }
      /** Can file a new appeal now (a honeypot ban on record, not already appealed). */
      eligible: boolean
      appeal: PublicAppealOutcome | null
    }

// ---------------------------------------------------------------------------
// Roles page (/dashboard/roles): server/routes/role_admin.py, staff only.
// Every id is a Discord snowflake string; never Number() them.
// ---------------------------------------------------------------------------

export interface AdminRole {
  id: string
  name: string
  /** "#rrggbb", or null for Discord's "no colour". */
  color: string | null
  /** Discord position: 0 = @everyone, higher = higher in the list. */
  position: number
  member_count: number
  hoist: boolean
  mentionable: boolean
  managed: boolean
  is_default: boolean
  is_bot_managed: boolean
  is_integration: boolean
  is_premium_subscriber: boolean
  is_bot_top_role: boolean
  /**
   * False for @everyone, managed roles, roles at/above the bot's top role, and
   * (for anyone but the owner) roles at/above the viewer's own top role.
   */
  manageable: boolean
  unmanageable_reason: string | null
  /** Enabled Discord permission flags (discord.py names). */
  permissions: string[]
  unicode_emoji: string | null
  icon_url: string | null
  is_default_staff: boolean
  /** Internal bot permission grants (permission_grants) held by this role. */
  internal_permissions: string[]
  created_at: string | null
}

export interface RolePermissionInfo {
  key: string
  label: string
  dangerous: boolean
}

export interface RolePermissionGroup {
  group: string
  permissions: RolePermissionInfo[]
}

export interface RoleAdminOverview {
  /** Highest position first, @everyone last. */
  roles: AdminRole[]
  bot: {
    user_id: string
    top_role_id: string
    top_role_name: string
    top_position: number
    administrator: boolean
    manage_roles: boolean
    /** Discord permission flags the bot holds (the only ones it may grant). */
    permissions: string[]
  }
  guild: {
    role_icons: boolean
    premium_tier: number
    role_count: number
    role_limit: number
  }
  permission_catalog: RolePermissionGroup[]
  valid_internal_permissions: string[]
  /** The viewing (acting) user's hierarchy; null only if the backend sent no actor. */
  actor: RoleAdminActor | null
}

export interface RoleAdminActor {
  user_id: string
  /** OWNER_ID / guild owner: bypasses the actor hierarchy and may grant Administrator. */
  is_owner: boolean
  is_member: boolean
  top_role_id: string | null
  /** 0 = only @everyone. Non-owners may only change roles strictly below this. */
  top_position: number
  /** Discord flags this user may add/remove (never "administrator" unless owner). */
  permissions: string[]
}

export interface RoleMutationResponse {
  role: AdminRole
  warnings?: string[]
  dropped_permissions?: string[]
  changed?: boolean
}

export interface RoleMember {
  user_id: string
  username: string
  display_name: string
  avatar_url: string
  bot: boolean
}

export interface RoleMembersResponse {
  members: RoleMember[]
  total: number
  offset: number
}

export interface RoleMemberChangeResponse {
  changed: boolean
  member: RoleMember
}

/** 4xx body (`detail`) from /api/roles routes. */
export interface RoleAdminErrorBody {
  error: string
  message?: string
  permissions?: string[]
}

// ---------------------------------------------------------------------------
// Fortnite item shop (/api/fortnite/*, perm manage_schedules) and the hidden
// owner-only Debug page (/api/debug/*, 404 for everyone but OWNER_ID).
// ---------------------------------------------------------------------------

export interface FortniteShopConfig {
  guild_id: string
  channel_id: string | null
  enabled: boolean
  include_jam_tracks: boolean
  ping_role_id: string | null
  /** Shop date (YYYY-MM-DD) of the last daily auto-post: the restart-safe dedupe key. */
  last_posted_date: string | null
  last_posted_hash: string | null
  last_posted_at: string | null
  last_message_id: string | null
  last_error: string | null
  last_error_at: string | null
}

export interface FortniteStatusResponse {
  config: FortniteShopConfig
  /** ISO UTC; null while disabled or without a channel. While post_pending it
   *  is "now" or the loop's retry time, not tomorrow's slot. */
  next_post_at: string | null
  /** Today's post is due but not done yet (just enabled, API not rotated, retrying). */
  post_pending: boolean
  post_delay_minutes: number
  cache: { shop_date: string | null; hash: string | null; entry_count: number }
  last_fetch_error: string | null
  wishlist: { entries: number; users: number }
  source: string
}

export interface UpdateFortniteConfigInput {
  channel_id?: string
  clear_channel?: boolean
  enabled?: boolean
  include_jam_tracks?: boolean
  ping_role_id?: string
  clear_ping_role?: boolean
}

export interface FortnitePostNowResponse {
  shop_date: string
  channel_id: string
  message_id: string | null
  jump_url: string | null
  message_count: number
}

export interface DebugActionParamOption {
  value: string
  label: string
}

export interface DebugActionParam {
  name: string
  label: string
  type: "text" | "textarea" | "select" | "bool"
  required?: boolean
  placeholder?: string
  options?: DebugActionParamOption[]
}

export interface DebugAction {
  id: string
  label: string
  group: string
  description: string
  params: DebugActionParam[]
  available: boolean
  unavailable_reason: string | null
}

export interface DebugActionsResponse {
  actions: DebugAction[]
  skipped: { feature: string; reason: string }[]
}

export interface DebugRunInput {
  action: string
  channel_id: string
  params: Record<string, string | boolean>
}

export interface DebugRunResult {
  ok: boolean
  action: string
  error: string | null
  note: string | null
  channel_id?: string
  message_ids: string[]
  jump_url: string | null
}

export interface DebugHealthResponse {
  ready: boolean
  latency_ms: number | null
  process: {
    pid?: number
    started_at?: string | null
    uptime_seconds?: number
    cpu_percent?: number
    rss_bytes?: number
    threads?: number
    error?: string
  }
  cogs_loaded_at: number | null
  guild: { id: string; name: string; members: number | null } | null
  cogs: string[]
  extensions_loaded: string[]
  extensions_expected: string[]
  extensions_failed: { extension: string; error: string }[]
  loops: {
    cog: string
    name: string
    running?: boolean
    failed?: boolean
    iterations?: number
    interval_seconds?: number | null
    next_run?: string | null
    error?: string
  }[]
}

/** Parsed shop from GET /api/debug/fortnite/preview (services/fortnite_shop_service.Shop.to_dict). */
export interface FortniteShopPreview {
  hash: string
  date: string
  shop_date: string
  vbuck_icon: string
  entry_count: number
  sections: { name: string; count: number }[]
  entries: Record<string, unknown>[]
}

// ---------------------------------------------------------------------------
// Supervisor restart controls (Diagnostics -> Services). /api/supervisor/* is
// staff-only and proxies supervisor.py's loopback control API DIRECTLY (not via
// the bot), so it works while the bot is down. PIDs are plain numbers (not
// Discord snowflakes); timestamps are ISO 8601 UTC.
// ---------------------------------------------------------------------------

export type SupervisorServiceName = "bot" | "dashboard" | "tunnel"
export type SupervisorTarget = SupervisorServiceName | "all"

export interface SupervisorRequestInfo {
  /** "display name (discord id)" for dashboard/Discord requests, "bot" or "cli (...)" otherwise. */
  by: string
  source: "dashboard" | "discord" | "bot" | "cli" | string
  reason: string | null
  at: string
}

export interface SupervisorService {
  name: SupervisorServiceName
  running: boolean
  pid: number | null
  uptime_s: number | null
  /** Starts after the first one (crash restarts and requested restarts). */
  restarts: number
  last_exit: { reason: string; at: string } | null
  last_request: SupervisorRequestInfo | null
  health_fails: number
  cooldown_remaining_s: number
  /** Due to start but waiting for its dependency (the dashboard waits for the bot's internal API). */
  waiting_on_dependency: boolean
  /** "port in use" when another process the supervisor did not start holds its port. */
  skip_reason: string | null
  next_start_in_s: number | null
  restart_pending: boolean
}

export interface SupervisorStatusResponse {
  supervisor: { pid: number; started_at: string; version: string; control: boolean; restart_cooldown_s: number }
  services: SupervisorService[]
}

export interface SupervisorRestartInput {
  target: SupervisorTarget
  reason?: string | null
}

export interface SupervisorRestartResponse {
  accepted: SupervisorServiceName[]
  skipped: { name: string; error: string; message: string }[]
  /** Seconds before the supervisor stops the target (lets this response get out first). */
  delay_s: number
  /** Approximate seconds until the new process is launched (not until it is ready). */
  eta_s: number
}

export interface SupervisorLogsResponse {
  service: SupervisorServiceName
  lines: string[]
}
