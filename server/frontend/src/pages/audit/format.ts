/** Display helpers for the Audit log page. */

export type AuditBadgeVariant = "default" | "success" | "warning" | "destructive" | "info" | "accent"

/** "permission_grant" / "template.edit" -> "Permission grant" / "Template edit". */
export function actionLabel(action: string): string {
  const words = action.replace(/[._:-]+/g, " ").trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : action
}

/**
 * Badge colour from the action name's verb, so new actions get a sensible
 * colour without a lookup table. Colour is never the only signal: the label
 * is always printed.
 */
export function actionVariant(action: string): AuditBadgeVariant {
  const a = action.toLowerCase()
  if (/(ban|kick|delete|revoke|removed|remove|left|denied|purge)/.test(a)) return "destructive"
  if (/(warn|timeout|mute|honeypot|spam)/.test(a)) return "warning"
  if (/(grant|create|added|joined|approved|verified|posted)/.test(a)) return "success"
  if (/(edit|update|changed|renamed|move|regenerated|set)/.test(a)) return "info"
  if (a.startsWith("permission") || a.startsWith("role")) return "accent"
  return "default"
}

/** "member_id" -> "Member", "from_channel_id" -> "From channel", "until" -> "Until". */
export function detailKeyLabel(key: string): string {
  const base = key.endsWith("_id") ? key.slice(0, -3) : key.endsWith("_ids") ? key.slice(0, -4) + "s" : key
  return actionLabel(base)
}

export type IdKind = "user" | "role" | "channel" | null

/** Which resolver a `*_id` detail key needs (null = show the raw id). */
export function idKindForKey(key: string): IdKind {
  if (!key.endsWith("_id")) return null
  if (key.includes("channel") || key.includes("category")) return "channel"
  if (key.includes("role")) return "role"
  if (/(member|user|actor|moderator|author|owner|reviewer|by)/.test(key)) return "user"
  return null
}

export function isSnowflake(value: unknown): value is string {
  return typeof value === "string" && /^\d{15,21}$/.test(value)
}

export type Scalar = string | number | boolean | null

/** A flat {key: scalar | scalar[]} object renders as a key/value list; anything else as JSON. */
export function asFlatDetail(detail: unknown): Record<string, Scalar | Scalar[]> | null {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return null
  const entries = Object.entries(detail as Record<string, unknown>)
  if (entries.length === 0) return null
  const isScalar = (v: unknown): v is Scalar =>
    v === null || typeof v === "string" || typeof v === "number" || typeof v === "boolean"
  for (const [, v] of entries) {
    if (isScalar(v)) continue
    if (Array.isArray(v) && v.length <= 50 && v.every(isScalar)) continue
    return null
  }
  return detail as Record<string, Scalar | Scalar[]>
}

/** Pretty JSON for the expanded row (never throws). */
export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value)
  } catch {
    return String(value)
  }
}

/** A short one-line preview of the detail for the collapsed row. */
export function detailPreview(detail: unknown): string {
  if (detail === null || detail === undefined) return ""
  if (typeof detail !== "object") return String(detail)
  const flat = asFlatDetail(detail)
  if (!flat) return ""
  if ("before" in flat && "after" in flat) return `${fmt(flat.before)} → ${fmt(flat.after)}`
  for (const key of ["permission", "name", "question", "reason", "code", "model"]) {
    if (key in flat && flat[key] !== null && flat[key] !== "") return fmt(flat[key])
  }
  return ""
}

function fmt(v: Scalar | Scalar[]): string {
  if (Array.isArray(v)) return v.map((x) => String(x)).join(", ")
  if (v === null) return "none"
  return String(v)
}

/** <input type="date"> value (local day) -> ISO instant at that day's local midnight. */
export function dayStartIso(day: string): string | undefined {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return undefined
  const [y, m, d] = day.split("-").map(Number)
  const date = new Date(y, m - 1, d)
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString()
}

/** Local midnight of the day AFTER `day` (makes "To" inclusive of that whole day). */
export function dayEndIso(day: string): string | undefined {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return undefined
  const [y, m, d] = day.split("-").map(Number)
  const date = new Date(y, m - 1, d + 1)
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString()
}

/** Target ids may be small database ids (templates) or snowflakes: digits only, up to 20. */
export function idFilterError(value: string): string | null {
  const v = value.trim()
  if (!v) return null
  if (!/^\d+$/.test(v)) return "An ID is numbers only."
  if (v.length > 20) return "That ID is too long (20 digits at most)."
  return null
}
