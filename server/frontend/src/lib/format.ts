/** Short relative time: "just now", "5m ago", "3h ago", "2d ago". */
export function formatRelativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime()
  const minutes = Math.floor(diffMs / 60_000)
  if (minutes < 1) return "just now"
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
}

/** Ticket numbers are shown zero-padded to 4 digits ("#0141"). */
export function padTicket(n: number): string {
  return `#${String(n).padStart(4, "0")}`
}

/**
 * Discord ID field validation (Settings IDs, warn-a-user, honeypot log channel):
 * 17 to 20 digits. Returns an error string in plain words, or null when valid
 * or empty (emptiness is the caller's "required" concern).
 */
export function discordIdError(value: string): string | null {
  const v = value.trim()
  if (!v) return null
  if (!/^\d+$/.test(v)) return "A Discord ID is numbers only (17 to 20 digits)."
  if (v.length < 17 || v.length > 20) return `A Discord ID is 17 to 20 digits. This one has ${v.length}.`
  return null
}

/** Discord CDN avatar URL from a user id + avatar hash (undefined when the user has no custom avatar). */
export function discordAvatarUrl(userId: string, avatarHash: string | null): string | undefined {
  if (!avatarHash) return undefined
  const ext = avatarHash.startsWith("a_") ? "gif" : "png"
  return `https://cdn.discordapp.com/avatars/${userId}/${avatarHash}.${ext}?size=64`
}

