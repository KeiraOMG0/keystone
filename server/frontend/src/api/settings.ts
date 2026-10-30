import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError, apiFetch } from "@/api/client"

// ---------------------------------------------------------------------------
// GET/PUT /api/settings (staff only; server/routes/settings.py proxying
// internal_api/routes/settings.py -> services/settings_service.py, which owns
// the allowlist, validation and the audit row). Types live here, not in
// types/api.ts, because this is the only consumer.
//
// Snowflakes are strings (null = not set); never Number() them.
// ---------------------------------------------------------------------------

export type SettingGroup = "verification" | "tickets" | "logging" | "permissions" | "music" | "antispam"
export type SettingType = "bool" | "int" | "str" | "snowflake"
export type SettingKind = "role" | "category" | "text_channel" | "user" | "template" | null
export type SettingValue = string | number | boolean | null

export interface SettingFieldMeta {
  key: string
  type: SettingType
  kind: SettingKind
  group: SettingGroup
  label: string
  description: string
  /** True = applies within seconds of saving; false = on the next bot restart. */
  live: boolean
  read_only: boolean
  min: number | null
  max: number | null
  pattern: string | null
}

export interface SettingFieldStatus {
  /** What is stored (or the .env default when nothing valid is stored). */
  saved: SettingValue
  /** What the running bot is using (startup value for restart fields). */
  running: SettingValue
  /** The .env default ("Reset" target). */
  default: SettingValue
  source: "db" | "default" | "env"
  /** Saved differs from running: applies after a bot restart. */
  pending_restart: boolean
  /** A malformed stored row that was ignored (falls back to the default). */
  invalid_stored: string | null
  /** The saved ID/template no longer resolves (deleted role, missing template...). */
  warning: string | null
}

export interface SettingsRoleOption {
  id: string
  name: string
  position: number
  /** The bot can add/remove this role (below its top role, not managed). */
  assignable: boolean
  problem: string | null
}

export interface SettingsOptions {
  roles: SettingsRoleOption[]
  categories: { id: string; name: string }[]
  text_channels: { id: string; name: string; category: string | null; bot_can_post: boolean }[]
  templates: string[]
}

export interface SettingsReport {
  values: Record<string, SettingValue>
  schema: SettingFieldMeta[]
  fields: Record<string, SettingFieldStatus>
  pending_restart: string[]
  /** False while the bot has not cached the guild yet (pickers are empty then). */
  guild_available: boolean
  options: SettingsOptions
  /** PUT only: keys that actually changed. */
  changed?: string[]
  /**
   * Epoch seconds when the running bot applied the saved settings (its start).
   * Changes after a restart; null/absent on a bot older than this field.
   */
  applied_at?: number | null
}

export const settingsQueryKey = ["settings"] as const

/** GET /api/settings. No live topic: the page is edited by few people and refetches on focus. */
export function useSettings(opts?: { refetchInterval?: number | false }) {
  return useQuery({
    queryKey: settingsQueryKey,
    queryFn: () => apiFetch<SettingsReport>("/api/settings"),
    refetchInterval: opts?.refetchInterval ?? false,
    retry: (count, err) => !(err instanceof ApiError && err.status === 403) && count < 2,
  })
}

/** PUT /api/settings. `null` resets a field to its .env default. Seeds the cache with the returned report. */
export function useUpdateSettings() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (values: Record<string, SettingValue>) =>
      apiFetch<SettingsReport>("/api/settings", { method: "PUT", body: JSON.stringify({ values }) }),
    onSuccess: (data) => {
      queryClient.setQueryData(settingsQueryKey, data)
    },
  })
}

/** Per-field messages from a 422 {"detail": {"error": "validation_failed", "fields": {...}}}, else null. */
export function settingsFieldErrors(error: unknown): Record<string, string> | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null
  const detail = (error.body as { detail?: { fields?: unknown } } | null)?.detail
  const fields = detail && typeof detail === "object" ? (detail as { fields?: unknown }).fields : null
  if (!fields || typeof fields !== "object") return null
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(fields as Record<string, unknown>)) {
    if (typeof v === "string") out[k] = v
  }
  return Object.keys(out).length ? out : null
}
