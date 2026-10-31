/** Date helpers for the member hub. The bot sends ISO strings with a Z, except
 *  SQLite timestamps ("2026-10-30 12:00:00", UTC without a zone), which some
 *  browsers can't parse as-is. */
export function toDate(value: string | null | undefined): Date | null {
  if (!value) return null
  let v = value.trim()
  if (/^\d{4}-\d{2}-\d{2} \d/.test(v)) v = v.replace(" ", "T")
  if (/T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(v)) v += "Z"
  const d = new Date(v)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatDay(value: string | null | undefined): string {
  const d = toDate(value)
  return d ? d.toLocaleDateString([], { dateStyle: "medium" }) : "Unknown"
}

export function formatWhen(value: string | null | undefined): string {
  const d = toDate(value)
  return d ? d.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : ""
}

/** "312 days" / "2 years" since a date. */
export function durationSince(value: string | null | undefined): string | null {
  const d = toDate(value)
  if (!d) return null
  const days = Math.max(0, Math.floor((Date.now() - d.getTime()) / 86_400_000))
  if (days < 1) return "today"
  if (days < 60) return `${days} day${days === 1 ? "" : "s"}`
  const months = Math.floor(days / 30.44)
  if (months < 24) return `${months} months`
  return `${Math.floor(days / 365.25)} years`
}

/** Birthday offset in plain words: "Today", "Tomorrow", "In 3 days", "3 days ago". */
export function birthdayOffset(offset: number): string {
  if (offset === 0) return "Today"
  if (offset === 1) return "Tomorrow"
  if (offset === -1) return "Yesterday"
  return offset > 0 ? `In ${offset} days` : `${-offset} days ago`
}

export function initials(name: string | null | undefined): string {
  const parts = (name ?? "?").trim().split(/\s+/).filter(Boolean)
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : (parts[0] ?? "?").slice(0, 2)).toUpperCase()
}

/** Unicode emoji only: custom Discord emoji (<:name:id>) can't be shown here. */
export function displayEmoji(emoji: string | null | undefined): string | null {
  if (!emoji || emoji.startsWith("<") || /^[\w:]+$/.test(emoji)) return null
  return emoji
}

/** Separator roles such as "─── STAFF ───" or "━━━━━━" that servers use to group
 *  the role list: decoration, not something a member "has". */
export function isDividerRole(name: string): boolean {
  return /^[\s\-_=─━═—–~⋅·•.*]{2,}/.test(name) && /[\s\-_=─━═—–~⋅·•.*]{2,}$/.test(name)
}
