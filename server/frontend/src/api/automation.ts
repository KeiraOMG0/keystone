import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError, apiFetch } from "@/api/client"
import { useLiveConnected, useLiveTopic } from "@/live/useLive"
import { invalidateSoon } from "@/live/invalidate"
import type { GuildChannel } from "@/types/api"

/**
 * Dashboard Automation page (/dashboard/automation). Wraps /api/automation/*
 * (server/routes/automation.py). Each tab has its own permission:
 * events = manage_events, greetings = require_staff, schedules =
 * manage_schedules, autoroles = manage_autorole, managed = manage_templates.
 * Snowflakes are strings; DB ids (template, schedule, rule) are numbers.
 *
 * Live: the `automation` topic carries {section, action}; each tab's hook
 * invalidates its own query and polls only while the socket is down.
 */

export type AutomationSection = "events" | "greetings" | "schedules" | "autoroles" | "managed"

export interface AutomationOptions {
  templates: { id: number; name: string; category: string | null }[]
  channels: GuildChannel[]
}

export interface EventBinding {
  id: number
  event_type: string
  channel_id: string
  channel_name: string | null
  send_dm: boolean
  enabled: boolean
  templates: { id: number; name: string }[]
  created_by: string | null
  created_at: string | null
}

export interface GreetingState {
  available: boolean
  configured: boolean
  enabled: boolean
  channel_id: string | null
  channel_name: string | null
  updated_at?: string | null
}

export interface Schedule {
  id: number
  label: string | null
  template_id: number
  template_name: string | null
  channel_id: string
  channel_name: string | null
  cron_expr: string
  enabled: boolean
  next_run_at: string | null
  last_run_at: string | null
  upcoming: string[]
  created_by: string | null
  created_at: string | null
}

export interface CronPreview {
  valid: boolean
  error: string | null
  next_runs: string[]
}

export interface AutoroleRule {
  id: number
  trigger_type: "join" | "boost" | "role_add"
  trigger_role_id: string | null
  trigger_role_name: string | null
  role_id: string
  role_name: string | null
  delay_seconds: number
  enabled: boolean
  /** Why the bot would refuse to hand this role out right now (null = fine). */
  blocked_now: string | null
  created_by: string | null
  created_at: string | null
}

export interface AutoroleRoleOption {
  id: string
  name: string
  color: string | null
  position: number
  managed: boolean
  /** Why YOU may not use this role as an autorole (null = allowed). */
  blocked_reason: string | null
}

export interface ManagedMessage {
  id: number
  message_id: string
  channel_id: string
  channel_name: string | null
  template_id: number
  template_name: string | null
  last_synced_at: string | null
  sync_failed: boolean
  created_by: string | null
  created_at: string | null
  jump_url: string
}

const ROOT = "automation"
const key = (section: AutomationSection | "options" | "autorole-roles") => [ROOT, section] as const
const POLL_MS = 60_000

/** Section of an `automation` live event -> query to refresh. */
function sectionKey(section: unknown): readonly unknown[] {
  if (section === "welcome" || section === "boost") return key("greetings")
  if (section === "events" || section === "schedules" || section === "autoroles" || section === "managed") return key(section)
  return [ROOT]
}

/**
 * Mount ONCE on the Automation page: one subscription for every tab
 * (handlers are idempotent; they only invalidate). Gated on the caller's data
 * being present so a failed background refetch does not drop it.
 */
export function useAutomationLive(enabled: boolean) {
  const qc = useQueryClient()
  useLiveTopic("automation", (msg) => invalidateSoon(qc, sectionKey((msg.data as { section?: unknown }).section)), { enabled })
}

export function useAutomationOptions(enabled = true) {
  return useQuery({
    queryKey: key("options"),
    enabled,
    queryFn: ({ signal }) => apiFetch<AutomationOptions>("/api/automation/options", { signal }),
    staleTime: 60_000,
  })
}

function useSection<T>(section: AutomationSection, path: string, enabled = true) {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: key(section),
    queryFn: ({ signal }) => apiFetch<T>(path, { signal }),
    enabled,
    refetchInterval: connected ? false : POLL_MS,
    // A 403 is the inline-403 state of that tab: do not retry it.
    retry: (count, error) => count < 2 && !(error instanceof ApiError && error.status === 403),
  })
}

function useAutomationMutation<I, O = unknown>(section: AutomationSection, fn: (input: I) => Promise<O>) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: fn,
    onSettled: () => qc.invalidateQueries({ queryKey: key(section) }),
  })
}

const json = (method: string, body?: unknown) => ({ method, body: body === undefined ? undefined : JSON.stringify(body) })

// ---- events (manage_events) ----------------------------------------------

export const useEventBindings = (enabled = true) =>
  useSection<{ event_types: string[]; bindings: EventBinding[] }>("events", "/api/automation/events", enabled)

export const useSetEventBinding = () =>
  useAutomationMutation("events", (i: { eventType: string; channel_id: string; template_id: number; send_dm: boolean }) =>
    apiFetch(`/api/automation/events/${i.eventType}`, json("PUT", { channel_id: i.channel_id, template_id: i.template_id, send_dm: i.send_dm }))
  )

export const useClearEventBinding = () =>
  useAutomationMutation("events", (eventType: string) => apiFetch(`/api/automation/events/${eventType}`, json("DELETE")))

export const useTestEvent = () =>
  useMutation({
    mutationFn: (eventType: string) =>
      apiFetch<{ sent: boolean; channel_id: string | null; channel_name: string | null }>(
        `/api/automation/events/${eventType}/test`,
        json("POST")
      ),
  })

// ---- welcome / boost (staff only) ------------------------------------------

export const useGreetings = (enabled = true) =>
  useSection<{ welcome: GreetingState; boost: GreetingState }>("greetings", "/api/automation/greetings", enabled)

export const useUpdateGreeting = () =>
  useAutomationMutation("greetings", (i: { kind: "welcome" | "boost"; channel_id?: string; enabled?: boolean }) =>
    apiFetch<GreetingState>(`/api/automation/greetings/${i.kind}`, json("PUT", { channel_id: i.channel_id, enabled: i.enabled }))
  )

export const useTestGreeting = () =>
  useMutation({
    mutationFn: (kind: "welcome" | "boost") =>
      apiFetch<{ sent: boolean; channel_name: string; jump_url: string | null }>(`/api/automation/greetings/${kind}/test`, json("POST")),
  })

// ---- schedules (manage_schedules) ------------------------------------------

export const useSchedules = (enabled = true) =>
  useSection<{ schedules: Schedule[] }>("schedules", "/api/automation/schedules", enabled)

export const usePreviewCron = () =>
  useMutation({
    mutationFn: (cron: string) => apiFetch<CronPreview>("/api/automation/schedules/preview", json("POST", { cron })),
  })

export const useCreateSchedule = () =>
  useAutomationMutation("schedules", (i: { template_id: number; channel_id: string; cron: string; label: string | null }) =>
    apiFetch<{ schedule_id: number }>("/api/automation/schedules", json("POST", i))
  )

export const useToggleSchedule = () =>
  useAutomationMutation("schedules", (i: { id: number; enabled: boolean }) =>
    apiFetch(`/api/automation/schedules/${i.id}`, json("PATCH", { enabled: i.enabled }))
  )

export const useRemoveSchedule = () =>
  useAutomationMutation("schedules", (id: number) => apiFetch(`/api/automation/schedules/${id}`, json("DELETE")))

// ---- autoroles (manage_autorole) -------------------------------------------

export const useAutoroles = (enabled = true) =>
  useSection<{ rules: AutoroleRule[]; triggers: string[]; max_delay_seconds: number }>("autoroles", "/api/automation/autoroles", enabled)

export function useAutoroleRoles(enabled: boolean) {
  return useQuery({
    queryKey: key("autorole-roles"),
    queryFn: ({ signal }) => apiFetch<{ roles: AutoroleRoleOption[] }>("/api/automation/autoroles/roles", { signal }),
    enabled,
    staleTime: 30_000,
  })
}

export const useCreateAutorole = () =>
  useAutomationMutation("autoroles", (i: { trigger: string; role_id: string; delay_seconds: number; trigger_role_id: string | null }) =>
    apiFetch<{ rule_id: number }>("/api/automation/autoroles", json("POST", i))
  )

export const useRemoveAutorole = () =>
  useAutomationMutation("autoroles", (id: number) => apiFetch(`/api/automation/autoroles/${id}`, json("DELETE")))

// ---- managed messages (manage_templates) -----------------------------------

export const useManagedMessages = (enabled = true) =>
  useSection<{ messages: ManagedMessage[] }>("managed", "/api/automation/managed", enabled)

export const useUntrackManaged = () =>
  useAutomationMutation("managed", (messageId: string) => apiFetch(`/api/automation/managed/${messageId}`, json("DELETE")))

export const useSyncManaged = () =>
  useAutomationMutation("managed", (templateId: number) =>
    apiFetch<{ ok: boolean; template_name: string }>("/api/automation/managed/sync", json("POST", { template_id: templateId }))
  )
