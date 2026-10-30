import { useMutation, useQuery } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"

/*
 * Owner-only Debug > Lab (/api/debug/lab/*, server/routes/debug.py ->
 * internal_api/routes/debug.py -> services/debug_lab_service.py). Every route
 * answers 404 to anyone but OWNER_ID. All tools are side-effect free; the only
 * Discord output is an optional SafeChannel copy of a preview. Types live here
 * (only consumer). Snowflakes are strings; never Number() them.
 */

const noRetryOn404 = (count: number, error: unknown) =>
  !(error && typeof error === "object" && "status" in error && (error as { status: number }).status === 404) && count < 2

export interface LabRole {
  id: string
  name: string
  position: number
  color: number
  managed: boolean
  staff: boolean
}

export interface LabBinding {
  event_type: string
  channel_id: string | null
  channel_name: string | null
  enabled: boolean
  send_dm: boolean
  templates: string[]
}

export interface LabSpamRules {
  enabled: boolean
  flood_messages: number
  flood_seconds: number
  duplicate_messages: number
  duplicate_seconds: number
  mention_limit: number
  timeout_minutes: number
}

export interface LabOptions {
  roles: LabRole[]
  templates: string[]
  event_types: string[]
  bindings: LabBinding[]
  features: { welcome: boolean; boost: boolean }
  antispam: Partial<LabSpamRules>
  antispam_fields: { key: string; label: string; min: number | null; max: number | null; type: string }[]
  valid_permissions: string[]
}

export function useLabOptions(enabled: boolean) {
  return useQuery({
    queryKey: ["debug", "lab", "options"],
    queryFn: ({ signal }) => apiFetch<LabOptions>("/api/debug/lab/options", { signal }),
    enabled,
    retry: noRetryOn404,
    staleTime: 30_000,
  })
}

// ---------------------------------------------------------------- permissions

export interface LabCheck {
  type: "permission" | "staff" | "owner"
  permission?: string
  passes: boolean
}

export interface LabCommand {
  name: string
  kind: "prefix" | "slash"
  cog: string | null
  aliases: string[]
  checks: LabCheck[]
  custom_checks: number
  default_permissions: string[]
  access: "yes" | "partial" | "no"
  discord_visible: boolean
}

export interface LabPermissionRow {
  permission: string
  allowed: boolean
  via: "owner" | "staff_role" | "grant" | null
  grants: { role_id: string; role_name: string }[]
  /** Member mode: what PermissionService.has_permission() answered (null in role mode). */
  bot_says: boolean | null
}

export interface LabPermissionResult {
  subject: {
    mode: "member" | "roles"
    user_id: string | null
    name: string | null
    display_name: string | null
    avatar_url: string | null
    is_bot: boolean
    roles: { id: string; name: string; position: number; is_default: boolean; staff: boolean; grants: string[] }[]
  }
  is_owner: boolean
  is_staff: boolean
  staff_reason: "owner" | "staff_role" | null
  staff_roles: { id: string; name: string }[]
  permissions: LabPermissionRow[]
  commands: LabCommand[]
  discord_permissions: string[]
  unknown_role_grants: string[]
  notes: string[]
}

export function useLabPermissions() {
  return useMutation({
    mutationFn: (body: { user_id?: string; role_ids?: string[] }) =>
      apiFetch<LabPermissionResult>("/api/debug/lab/permissions", { method: "POST", body: JSON.stringify(body) }),
  })
}

// ---------------------------------------------------------------- previews

export type LabPreviewKind = "welcome" | "boost" | "event" | "template"

export interface DiscordEmbedJson {
  title?: string
  description?: string
  url?: string
  color?: number
  timestamp?: string
  footer?: { text?: string; icon_url?: string }
  image?: { url?: string }
  thumbnail?: { url?: string }
  author?: { name?: string; url?: string; icon_url?: string }
  fields?: { name: string; value: string; inline?: boolean }[]
}

export interface LabPreviewResult {
  kind: LabPreviewKind
  event_type: string | null
  messages: { content: string | null; embeds: DiscordEmbedJson[]; template?: string | null }[]
  channel_id: string | null
  channel_name: string | null
  notes: string[]
  placeholders: Record<string, string>
  member: { user_id: string; display_name: string; name: string }
  sent: { channel_id: string; message_ids: string[]; jump_url: string | null } | null
}

export interface LabPreviewInput {
  kind: LabPreviewKind
  user_id: string
  event_type?: string | null
  role_id?: string | null
  template?: string | null
  send_channel_id?: string | null
}

export function useLabPreview() {
  return useMutation({
    mutationFn: (body: LabPreviewInput) =>
      apiFetch<LabPreviewResult>("/api/debug/lab/preview", { method: "POST", body: JSON.stringify(body) }),
  })
}

// ---------------------------------------------------------------- anti-spam

export interface LabSpamMessage {
  at: number
  user?: string
  channel?: string
  content?: string | null
  mentions?: number | null
}

export interface LabSpamResult {
  rules: LabSpamRules
  results: (LabSpamMessage & {
    index: number
    user: string
    channel: string
    mentions: number
    verdict: { rule: string; detail: string; first: boolean; deletes: number[] } | null
  })[]
  deleted: number[]
  counts: Record<string, number>
  timeouts: { user: string; at: number; minutes: number; rule: string }[]
  notes: string[]
}

export function useLabAntispam() {
  return useMutation({
    mutationFn: (body: { overrides: Record<string, number | boolean | null>; messages: LabSpamMessage[] }) =>
      apiFetch<LabSpamResult>("/api/debug/lab/antispam", { method: "POST", body: JSON.stringify(body) }),
  })
}

// ---------------------------------------------------------------- latency

export interface LabLatencyResult {
  results: { name: string; path: string; status: number | null; ms: number; bytes: number; error: string | null }[]
  total_ms: number
}

export function useLabLatency() {
  return useMutation({
    mutationFn: () => apiFetch<LabLatencyResult>("/api/debug/lab/latency"),
  })
}
