import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { useLiveConnected, useLiveTopic } from "@/live/useLive"
import type { HoneypotAppeal, HoneypotCatch, MemberSearchResponse, ModAction, Warning } from "@/types/api"

/**
 * Member profile (`/userinfo`, richer), `!verify`, `!purge` (job + poll),
 * `!msglog` DM export and the `/serverinfo` facts. Backed by
 * server/routes/members.py, server/routes/moderation.py (purge, message log)
 * and server/routes/stats.py (server info). Every Discord snowflake is a
 * string end-to-end; never Number() one.
 */

const FALLBACK_POLL_MS = 30_000

// ---------------------------------------------------------------------------
// Types (local to these features)
// ---------------------------------------------------------------------------

export type MemberStatus = "member" | "banned" | "kicked" | "not_member"

export interface MemberProfileRole {
  id: string
  name: string
  color: string | null
}

export interface MemberProfile {
  user_id: string
  in_guild: boolean
  status: MemberStatus
  user: {
    id: string
    username: string | null
    global_name: string | null
    display_name: string | null
    avatar_url: string | null
    bot: boolean
    created_at: string
    /** false when Discord could not tell us who this id is. */
    known: boolean
  }
  member: {
    nick: string | null
    joined_at: string | null
    premium_since: string | null
    pending: boolean
    timed_out_until: string | null
    color: string | null
    roles: MemberProfileRole[]
    verified: boolean
    unverified: boolean
    is_guild_owner: boolean
  } | null
  /** banned null = unknown (the bot may not read bans). */
  ban: { banned: boolean | null; reason: string | null }
  last_departure: { action: "kick" | "ban" | "unban"; at: string | null } | null
  /** null = withheld (see `restricted`). */
  warnings: Warning[] | null
  history: ModAction[] | null
  honeypot: { catches: HoneypotCatch[]; latest_appeal: HoneypotAppeal | null } | null
  /** Advisory rank verdict for YOU acting on this user (every action re-checks). */
  actor_block_reason: string | null
  actor_checked: boolean
  /** Sections the viewer may not see: "warnings" | "history" | "honeypot". */
  restricted: string[]
}

export interface VerifyChannelsResponse {
  category_id: string | null
  channels: { id: string; name: string }[]
}

export interface PurgeChannel {
  id: string
  name: string
  category: string | null
  bot_can_purge: boolean
}

export interface PurgeJob {
  job_id: string
  status: "running" | "done" | "failed"
  channel_id: string
  amount: string
  user_id: string | null
  deleted: number | null
  error: string | null
  started_at: string
  finished_at: string | null
}

export type MessageLogStatus =
  | { enabled: false; retention_days: number }
  | { enabled: true; rows: number; deleted: number; oldest: string | null; retention_days: number; size_kb: number }

export interface ServerInfo {
  id: string
  name: string
  icon_url: string | null
  owner_id: string | null
  member_count: number | null
  created_at: string
  boost_tier: number
  boosts: number
  channels: number
  text_channels: number
  voice_channels: number
  categories: number
  roles: number
  emojis: number
  verification_level: string
}

// ---------------------------------------------------------------------------
// Bounds (mirror services/purge_service.py and server/routes/moderation.py)
// ---------------------------------------------------------------------------

export const PURGE_MIN = 1
export const PURGE_MAX = 1000
export const PURGE_CONFIRM_ABOVE = 100
export const PURGE_MAX_WINDOW_SECONDS = 14 * 24 * 3600
const UNIT_SECONDS: Record<string, number> = { s: 1, m: 60, h: 3600, d: 86400 }

/** {error} or {confirm: whether the purge needs the explicit confirm step}. */
export function checkPurgeAmount(raw: string): { error: string | null; confirm: boolean } {
  const text = raw.trim()
  if (/^-?\d+$/.test(text)) {
    const n = Number(text)
    if (n < PURGE_MIN || n > PURGE_MAX) return { error: `Enter a count from ${PURGE_MIN} to ${PURGE_MAX}.`, confirm: false }
    return { error: null, confirm: n > PURGE_CONFIRM_ABOVE }
  }
  const m = /^(\d+)\s*([smhd])$/i.exec(text)
  if (!m) return { error: "Enter a count (1-1000) or a window like 30s, 15m, 2h or 1d.", confirm: false }
  const seconds = Number(m[1]) * UNIT_SECONDS[m[2].toLowerCase()]
  if (seconds <= 0 || seconds > PURGE_MAX_WINDOW_SECONDS) return { error: "The window must be between 1 second and 14 days.", confirm: false }
  return { error: null, confirm: true }
}

// ---------------------------------------------------------------------------
// Profile
// ---------------------------------------------------------------------------

export function profileQueryKey(userId: string) {
  return ["members", "profile", userId] as const
}

/** GET /api/members/{id}/profile (staff_base). Refreshes on the `moderation`
 *  live topic for this user (needs moderate_members; otherwise it polls). */
export function useMemberProfile(userId: string, opts: { liveTopicAllowed: boolean }) {
  const connected = useLiveConnected()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: profileQueryKey(userId),
    queryFn: ({ signal }) => apiFetch<MemberProfile>(`/api/members/${encodeURIComponent(userId)}/profile`, { signal }),
    enabled: /^\d{15,21}$/.test(userId),
    refetchInterval: connected && opts.liveTopicAllowed ? false : FALLBACK_POLL_MS,
  })
  useLiveTopic(
    "moderation",
    (msg) => {
      const uid = (msg.data as { user_id?: string }).user_id
      if (!uid || uid === userId) queryClient.invalidateQueries({ queryKey: profileQueryKey(userId) })
    },
    { enabled: opts.liveTopicAllowed && query.data !== undefined }
  )
  return query
}

/** Moderation mutations (api/moderation.ts) invalidate their own keys; the
 *  profile page calls this after any of them succeeds. */
export function useInvalidateProfile() {
  const queryClient = useQueryClient()
  return (userId: string) => queryClient.invalidateQueries({ queryKey: profileQueryKey(userId) })
}

/** GET /api/members/search (staff_base): typeahead for the Members page. */
export function useStaffMemberSearch(query: string) {
  return useQuery({
    queryKey: ["members", "search", query],
    queryFn: ({ signal }) => apiFetch<MemberSearchResponse>(`/api/members/search?q=${encodeURIComponent(query)}`, { signal }),
    enabled: query.trim().length >= 2,
  })
}

// ---------------------------------------------------------------------------
// Verify
// ---------------------------------------------------------------------------

export function useVerifyChannels(enabled: boolean) {
  return useQuery({
    queryKey: ["members", "verify-channels"],
    queryFn: ({ signal }) => apiFetch<VerifyChannelsResponse>("/api/members/verify-channels", { signal }),
    enabled,
    staleTime: 60_000,
  })
}

export function useVerifyMember() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ user_id, channel_id }: { user_id: string; channel_id: string }) =>
      apiFetch<{ ok: boolean; message_status: string }>(`/api/members/${encodeURIComponent(user_id)}/verify`, {
        method: "POST",
        body: JSON.stringify({ channel_id }),
      }),
    onSuccess: (_d, v) => queryClient.invalidateQueries({ queryKey: profileQueryKey(v.user_id) }),
  })
}

// ---------------------------------------------------------------------------
// Purge (202 job + poll)
// ---------------------------------------------------------------------------

export function usePurgeChannels(enabled: boolean) {
  return useQuery({
    queryKey: ["moderation", "purge-channels"],
    queryFn: ({ signal }) => apiFetch<{ channels: PurgeChannel[] }>("/api/moderation/purge-channels", { signal }),
    enabled,
    staleTime: 60_000,
  })
}

export interface StartPurgeInput {
  channel_id: string
  amount: string
  user_id: string | null
  confirm: boolean
}

export function useStartPurge() {
  return useMutation({
    mutationFn: (body: StartPurgeInput) =>
      apiFetch<PurgeJob>("/api/moderation/purge", { method: "POST", body: JSON.stringify(body) }),
  })
}

/** Polls a purge job every 1.5 s until it is done or failed. */
export function usePurgeJob(jobId: string | null) {
  return useQuery({
    queryKey: ["moderation", "purge-job", jobId],
    queryFn: ({ signal }) => apiFetch<PurgeJob>(`/api/moderation/purge/${encodeURIComponent(jobId ?? "")}`, { signal }),
    enabled: jobId !== null,
    refetchInterval: (q) => (q.state.data && q.state.data.status !== "running" ? false : 1500),
    retry: false,
  })
}

// ---------------------------------------------------------------------------
// Message log (staff only, DM only)
// ---------------------------------------------------------------------------

export function useMessageLogStatus(enabled: boolean) {
  return useQuery({
    queryKey: ["moderation", "message-log", "status"],
    queryFn: ({ signal }) => apiFetch<MessageLogStatus>("/api/moderation/message-log/status", { signal }),
    enabled,
  })
}

export interface ExportInput {
  kind: "user" | "channel" | "deleted"
  user_id?: string | null
  channel_id?: string | null
  window?: string | null
}

/** POST /api/moderation/message-log/export: the bot DMs the file to YOU. */
export function useMessageLogExport() {
  return useMutation({
    mutationFn: (body: ExportInput) =>
      apiFetch<{ sent: boolean; count: number }>("/api/moderation/message-log/export", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  })
}

// ---------------------------------------------------------------------------
// Server info (Overview card)
// ---------------------------------------------------------------------------

export function useServerInfo() {
  return useQuery({
    queryKey: ["dashboard", "server-info"],
    queryFn: ({ signal }) => apiFetch<ServerInfo>("/api/dashboard/server-info", { signal }),
    staleTime: 5 * 60_000,
  })
}
