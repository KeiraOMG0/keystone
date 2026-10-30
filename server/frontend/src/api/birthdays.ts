import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { invalidateSoon } from "@/live/invalidate"
import { useLiveConnected, useLiveTopic } from "@/live/useLive"
import type { GuildChannel, MemberSearchResponse } from "@/types/api"

/**
 * Birthdays page (/dashboard/birthdays), every route gated on the bot
 * permission `manage_birthdays` (staff bypass), the same grant the /config,
 * /override and /birthdays slash commands check. Snowflakes are strings.
 */

export interface BirthdaySettings {
  birthday_role_id: string | null
  announce_channel_id: string | null
  /** null = UTC */
  guild_timezone: string | null
  announce_message: string | null
  announce_message_pl: string | null
  /** {mention} pings (true) or shows the display name (false) */
  announce_ping: boolean
  /** separate @mention message before the announcement */
  ping_user: boolean
  add_only: boolean
  private_confirms: boolean
}

export interface BirthdayEntry {
  user_id: string
  month: number
  day: number
  /** "Mar-14" */
  date_display: string
  user_timezone: string | null
  /** local year of the last announcement (0 = never) */
  last_celebrated: number
  /** ISO date of the next celebration (server timezone calendar) */
  next_date: string
  days_until: number
  in_guild: boolean
  display_name: string | null
  username: string | null
}

export interface NearestGroup {
  /** days from today (negative = past) */
  offset: number
  date: string
  month: number
  day: number
  user_ids: string[]
}

export interface BirthdayOverview {
  settings: BirthdaySettings
  status: {
    registered: number
    scheduled: number
    server_timezone: string
    server_today: string
    role: { id: string | null; name: string | null; exists: boolean; usable: boolean }
    channel: { id: string | null; name: string | null; exists: boolean; can_send: boolean }
  }
  entries: BirthdayEntry[]
  nearest: NearestGroup[]
  limits: { announce_message_max: number; import_max_bytes: number; test_members_max: number }
  warnings?: string[]
  changed?: string[]
}

export interface BirthdayRoleOption {
  id: string
  name: string
  color: number
  position: number
  managed: boolean
  /** null = you may pick it */
  blocked_reason: string | null
}

export interface BirthdayConfigInput {
  birthday_role_id?: string | null
  clear_birthday_role?: boolean
  announce_channel_id?: string | null
  clear_announce_channel?: boolean
  guild_timezone?: string | null
  clear_guild_timezone?: boolean
  announce_message?: string | null
  announce_message_pl?: string | null
  set_messages?: boolean
  announce_ping?: boolean
  ping_user?: boolean
  add_only?: boolean
  private_confirms?: boolean
}

export interface BirthdayEntryInput {
  userId: string
  month: number
  day: number
  timezone: string | null
  clear_timezone: boolean
}

export interface ImportPreview {
  dry_run: boolean
  valid: number
  users: number
  new: number
  updated: number
  unchanged: number
  invalid: { line: number; text: string; reason: string }[]
  entries: { user_id: string; birth_month: number; birth_day: number; user_timezone: string | null; in_guild: boolean }[]
  imported?: number
  skipped?: number
  scheduled?: number
}

export type ExportFormat = "plaintext" | "csv" | "ics"

/** Query root "birthdays" is in LIVE_ROOTS (src/live/LiveProvider.tsx): refetched after a reconnect. */
export const birthdayKeys = {
  all: ["birthdays"] as const,
  overview: ["birthdays", "overview"] as const,
  roles: ["birthdays", "roles"] as const,
  channels: ["birthdays", "channels"] as const,
  timezones: ["birthdays", "timezones"] as const,
  search: (q: string) => ["birthdays", "member-search", q] as const,
}

/** GET /api/birthdays. Live via useBirthdaysLive; polls every 60 s only while the socket is down. */
export function useBirthdays() {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: birthdayKeys.overview,
    queryFn: ({ signal }) => apiFetch<BirthdayOverview>("/api/birthdays", { signal }),
    refetchInterval: connected ? false : 60_000,
  })
}

/**
 * LIVE `birthdays` topic (manage_birthdays): any birthday / settings change
 * (dashboard, slash commands, the scheduler marking one celebrated) re-fetches
 * the overview. Idempotent. Enable once the overview has data.
 */
export function useBirthdaysLive(enabled: boolean) {
  const qc = useQueryClient()
  useLiveTopic("birthdays", () => invalidateSoon(qc, birthdayKeys.overview), { enabled })
}

export function useBirthdayRoles(enabled = true) {
  return useQuery({
    queryKey: birthdayKeys.roles,
    queryFn: ({ signal }) => apiFetch<{ roles: BirthdayRoleOption[] }>("/api/birthdays/roles", { signal }),
    staleTime: 60_000,
    enabled,
  })
}

export function useBirthdayChannels(enabled = true) {
  return useQuery({
    queryKey: birthdayKeys.channels,
    queryFn: ({ signal }) => apiFetch<GuildChannel[]>("/api/birthdays/channels", { signal }),
    staleTime: 60_000,
    enabled,
  })
}

export function useBirthdayTimezones() {
  return useQuery({
    queryKey: birthdayKeys.timezones,
    queryFn: ({ signal }) => apiFetch<{ timezones: string[] }>("/api/birthdays/timezones", { signal }),
    staleTime: Infinity,
  })
}

/** Member typeahead (2+ characters). Debounce `query` before passing it. */
export function useBirthdayMemberSearch(query: string) {
  const q = query.trim()
  return useQuery({
    queryKey: birthdayKeys.search(q),
    queryFn: ({ signal }) =>
      apiFetch<MemberSearchResponse>(`/api/birthdays/members/search?q=${encodeURIComponent(q)}&limit=10`, { signal }),
    enabled: q.length >= 2,
    staleTime: 30_000,
  })
}

export function useUpdateBirthdayConfig() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: BirthdayConfigInput) =>
      apiFetch<BirthdayOverview>("/api/birthdays/config", { method: "PUT", body: JSON.stringify(input) }),
    onSuccess: (data) => {
      const { warnings: _w, changed: _c, ...overview } = data
      qc.setQueryData(birthdayKeys.overview, overview)
    },
  })
}

export function useSetBirthday() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, ...body }: BirthdayEntryInput) =>
      apiFetch<{ ok: boolean; created: boolean }>(`/api/birthdays/entries/${encodeURIComponent(userId)}`, {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: birthdayKeys.overview }),
  })
}

export function useRemoveBirthday() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (userId: string) =>
      apiFetch<{ removed: boolean }>(`/api/birthdays/entries/${encodeURIComponent(userId)}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: birthdayKeys.overview }),
  })
}

export function useBirthdayTest() {
  return useMutation({
    mutationFn: (userIds: string[]) =>
      apiFetch<{ ok: boolean; channel_id: string | null; member_count: number }>("/api/birthdays/test", {
        method: "POST",
        body: JSON.stringify({ user_ids: userIds }),
      }),
  })
}

export function useBirthdayResetTimers() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => apiFetch<{ reset: number; scheduled: number }>("/api/birthdays/reset-timers", { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: birthdayKeys.overview }),
  })
}

export function useBirthdayExport() {
  return useMutation({
    mutationFn: (format: ExportFormat) =>
      apiFetch<{ filename: string; content: string; count: number }>(`/api/birthdays/export?format=${format}`),
  })
}

export function useBirthdayImport() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { filename: string; content: string; dry_run: boolean }) =>
      apiFetch<ImportPreview>("/api/birthdays/import", { method: "POST", body: JSON.stringify(input) }),
    onSuccess: (data) => {
      if (!data.dry_run) void qc.invalidateQueries({ queryKey: birthdayKeys.overview })
    },
  })
}
