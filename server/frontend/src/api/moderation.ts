import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError, apiFetch } from "@/api/client"
import { useLiveConnected, useLiveTopic } from "@/live/useLive"
import type {
  AddWarningResponse,
  MemberSearchResponse,
  ModActionResponse,
  ModHistoryResponse,
  RecentWarningsResponse,
  RemoveWarningResponse,
  WarningsResponse,
} from "@/types/api"

/** Poll interval for the moderation queries, used ONLY while the live socket is down. */
const FALLBACK_POLL_MS = 30_000

/**
 * LIVE `moderation` topic (requires "moderate_members"): kick/ban/unban/
 * timeout/untimeout/warn/warning_removed events refresh the recent-warnings
 * feed and the per-user warnings/history caches. Mount once per
 * moderation-related page; pass `enabled: false` until the page's main query
 * has data (CLAUDE.md live-page convention).
 */
export function useModerationLive(opts?: { enabled?: boolean }) {
  const queryClient = useQueryClient()
  useLiveTopic("moderation", (msg) => {
    const userId = (msg.data as { user_id?: string }).user_id
    queryClient.invalidateQueries({ queryKey: recentWarningsQueryKey })
    if (userId) {
      queryClient.invalidateQueries({ queryKey: warningsQueryKey(userId) })
      queryClient.invalidateQueries({ queryKey: historyQueryKey(userId) })
    } else {
      queryClient.invalidateQueries({ queryKey: ["moderation"] })
    }
  }, opts)
}

function warningsQueryKey(userId: string) {
  return ["moderation", "warnings", userId] as const
}

const recentWarningsQueryKey = ["moderation", "warnings", "recent"] as const

async function fetchWarnings(userId: string): Promise<WarningsResponse> {
  return apiFetch<WarningsResponse>(`/api/moderation/warnings/${encodeURIComponent(userId)}`)
}

/**
 * Wraps GET /api/moderation/warnings/{userId} (requires "moderate_members"
 * server-side). Only fires when `userId` is a non-empty string — the caller
 * is responsible for not passing a value until the user has actually
 * submitted a search, so we don't fetch on every keystroke.
 */
export function useWarnings(userId: string) {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: warningsQueryKey(userId),
    queryFn: () => fetchWarnings(userId),
    enabled: userId.length > 0,
    refetchInterval: connected ? false : FALLBACK_POLL_MS,
  })
}

async function fetchRecentWarnings(): Promise<RecentWarningsResponse> {
  return apiFetch<RecentWarningsResponse>("/api/moderation/warnings/recent")
}

/**
 * Wraps GET /api/moderation/warnings/recent (requires "moderate_members"
 * server-side) — most recent warnings across the whole guild, newest first.
 * Powers ModerationPage's landing state so there's something to look at
 * before a moderator searches a specific user ID. Always enabled (unlike
 * useWarnings, which waits for a search) since this is meant to load
 * immediately on page visit.
 */
export function useRecentWarnings(enabled = true) {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: recentWarningsQueryKey,
    queryFn: fetchRecentWarnings,
    enabled,
    refetchInterval: connected ? false : FALLBACK_POLL_MS,
  })
}

function memberSearchQueryKey(query: string) {
  return ["moderation", "members", "search", query] as const
}

async function searchMembers(query: string): Promise<MemberSearchResponse> {
  return apiFetch<MemberSearchResponse>(`/api/moderation/members/search?q=${encodeURIComponent(query)}`)
}

/**
 * Wraps GET /api/moderation/members/search (requires "moderate_members"
 * server-side) — live guild.members username/display-name typeahead for the
 * "Search user" box. Only fires once 2+ characters are typed; the caller is
 * responsible for debouncing keystrokes before `query` changes.
 */
export function useMemberSearch(query: string) {
  return useQuery({
    queryKey: memberSearchQueryKey(query),
    queryFn: () => searchMembers(query),
    enabled: query.trim().length >= 2,
  })
}

interface AddWarningInput {
  /** Discord snowflake — string end-to-end, never Number(). */
  user_id: string
  reason: string | null
}

async function addWarningRequest(body: AddWarningInput): Promise<AddWarningResponse> {
  return apiFetch<AddWarningResponse>("/api/moderation/warnings", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/**
 * Wraps POST /api/moderation/warnings. On success, invalidates that user's
 * warnings query and the guild-wide recent-warnings query (the new warning
 * should show up in ModerationPage's landing list too) so both re-fetch.
 */
export function useAddWarning() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: addWarningRequest,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: warningsQueryKey(variables.user_id) })
      queryClient.invalidateQueries({ queryKey: historyQueryKey(variables.user_id) })
      queryClient.invalidateQueries({ queryKey: recentWarningsQueryKey })
    },
  })
}

interface RemoveWarningInput {
  /** Small DB autoincrement id — safe as a JS number. */
  warning_id: number
  /** Discord snowflake of the member the warning belongs to. Sent as
   *  ?user_id= so the backend only deletes a warning of THAT member, and used
   *  by onSuccess to know which query to invalidate. */
  user_id: string
}

async function removeWarningRequest({ warning_id, user_id }: RemoveWarningInput): Promise<RemoveWarningResponse> {
  return apiFetch<RemoveWarningResponse>(
    `/api/moderation/warnings/${warning_id}?user_id=${encodeURIComponent(user_id)}`,
    { method: "DELETE" }
  )
}

/**
 * Wraps DELETE /api/moderation/warnings/{warningId}. On success, invalidates
 * the warnings query for the user the removed warning belonged to, plus the
 * guild-wide recent-warnings query so a removed warning drops out of
 * ModerationPage's landing list too.
 */
export function useRemoveWarning() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: removeWarningRequest,
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: warningsQueryKey(variables.user_id) })
      queryClient.invalidateQueries({ queryKey: historyQueryKey(variables.user_id) })
      queryClient.invalidateQueries({ queryKey: recentWarningsQueryKey })
    },
  })
}

// ---------------------------------------------------------------------------
// Unified mod-action history + kick/ban/unban/timeout/untimeout
// ---------------------------------------------------------------------------

function historyQueryKey(userId: string) {
  return ["moderation", "history", userId] as const
}

async function fetchHistory(userId: string): Promise<ModHistoryResponse> {
  return apiFetch<ModHistoryResponse>(`/api/moderation/history/${encodeURIComponent(userId)}`)
}

/**
 * Wraps GET /api/moderation/history/{userId} (requires "moderate_members"
 * server-side) — the unified kick/ban/unban/timeout/untimeout/warn history.
 * Same enabled-gating convention as useWarnings: only fires once a search
 * has actually been submitted.
 */
export function useModHistory(userId: string) {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: historyQueryKey(userId),
    queryFn: () => fetchHistory(userId),
    enabled: userId.length > 0,
    refetchInterval: connected ? false : FALLBACK_POLL_MS,
  })
}

/** Invalidates both the warnings and history queries for a user — every
 * mutation below can affect either or both (e.g. a kick doesn't touch
 * warnings, but keeping both fresh is cheap and avoids stale-state bugs if
 * this ever changes). */
function invalidateModerationQueries(queryClient: ReturnType<typeof useQueryClient>, userId: string) {
  queryClient.invalidateQueries({ queryKey: warningsQueryKey(userId) })
  queryClient.invalidateQueries({ queryKey: historyQueryKey(userId) })
}

interface KickInput {
  /** Discord snowflake — string end-to-end, never Number(). */
  user_id: string
  reason: string | null
}

async function kickRequest(body: KickInput): Promise<ModActionResponse> {
  return apiFetch<ModActionResponse>("/api/moderation/kick", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/** Wraps POST /api/moderation/kick (requires "kick_members" server-side). */
export function useKickMember() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: kickRequest,
    onSuccess: (_data, variables) => invalidateModerationQueries(queryClient, variables.user_id),
  })
}

interface BanInput {
  /** Discord snowflake — string end-to-end, never Number(). */
  user_id: string
  reason: string | null
  /** 0-7 (the backend 422s anything else). Defaults to 0 server-side. */
  delete_message_days?: number
}

async function banRequest(body: BanInput): Promise<ModActionResponse> {
  return apiFetch<ModActionResponse>("/api/moderation/ban", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/** Wraps POST /api/moderation/ban (requires "ban_members" server-side). */
export function useBanMember() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: banRequest,
    onSuccess: (_data, variables) => invalidateModerationQueries(queryClient, variables.user_id),
  })
}

interface UnbanInput {
  /** Discord snowflake — string end-to-end, never Number(). */
  user_id: string
  reason: string | null
}

async function unbanRequest(body: UnbanInput): Promise<ModActionResponse> {
  return apiFetch<ModActionResponse>("/api/moderation/unban", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/** Wraps POST /api/moderation/unban (requires "ban_members" server-side). */
export function useUnbanUser() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: unbanRequest,
    onSuccess: (_data, variables) => invalidateModerationQueries(queryClient, variables.user_id),
  })
}

interface TimeoutInput {
  /** Discord snowflake — string end-to-end, never Number(). */
  user_id: string
  duration_minutes: number
  reason: string | null
}

async function timeoutRequest(body: TimeoutInput): Promise<ModActionResponse> {
  return apiFetch<ModActionResponse>("/api/moderation/timeout", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/** Wraps POST /api/moderation/timeout (requires "moderate_members" server-side). */
export function useTimeoutMember() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: timeoutRequest,
    onSuccess: (_data, variables) => invalidateModerationQueries(queryClient, variables.user_id),
  })
}

interface UntimeoutInput {
  /** Discord snowflake — string end-to-end, never Number(). */
  user_id: string
  reason: string | null
}

async function untimeoutRequest(body: UntimeoutInput): Promise<ModActionResponse> {
  return apiFetch<ModActionResponse>("/api/moderation/untimeout", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/** Wraps POST /api/moderation/untimeout (requires "moderate_members" server-side). */
export function useUntimeoutMember() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: untimeoutRequest,
    onSuccess: (_data, variables) => invalidateModerationQueries(queryClient, variables.user_id),
  })
}

// ---------------------------------------------------------------------------
// Input bounds and refusal helpers (mirror server/routes/moderation.py)
// ---------------------------------------------------------------------------

/** Discord's audit-log reason limit; the backend 422s anything longer. */
export const MAX_REASON_LENGTH = 512
/** Discord's maximum timeout: 28 days. */
export const MAX_TIMEOUT_MINUTES = 28 * 24 * 60
export const MAX_DELETE_MESSAGE_DAYS = 7

const BLOCK_CODES = new Set(["hierarchy_blocked", "actor_not_member"])

/**
 * The bot's rank/identity refusal for a moderation mutation (403
 * {"error": "hierarchy_blocked" | "actor_not_member", "message"}), or null for
 * any other error. The server check is what counts; this only lets the page
 * show the reason prominently instead of only in a toast.
 */
export function moderationBlockReason(error: unknown): string | null {
  if (!(error instanceof ApiError) || error.status !== 403) return null
  // apiFetch already unwraps FastAPI's {"detail": ...}: body IS the detail.
  const body = error.body
  if (!body || typeof body !== "object") return null
  const { error: code, message } = body as { error?: unknown; message?: unknown }
  if (typeof code !== "string" || !BLOCK_CODES.has(code)) return null
  return typeof message === "string" && message ? message : "You can't take this action on that member."
}
