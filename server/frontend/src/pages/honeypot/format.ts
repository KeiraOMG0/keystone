import { ApiError } from "@/api/client"

/** Parses an ISO-8601 UTC string; tolerates a missing timezone suffix (SQLite). */
export function parseUtc(iso: string | null | undefined): Date | null {
  if (!iso) return null
  const hasTz = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso)
  const d = new Date(hasTz ? iso : `${iso.replace(" ", "T")}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

export function formatDateTime(iso: string | null | undefined): string {
  const d = parseUtc(iso)
  return d ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "unknown"
}

export function formatRelative(iso: string | null | undefined): string {
  const d = parseUtc(iso)
  if (!d) return "unknown"
  const secs = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000))
  if (secs < 60) return "just now"
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "n/a"
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`
  return `${(seconds / 86400).toFixed(1)}d`
}

/** Account age at the time of the catch, as a short string. */
export function accountAge(createdAt: string | null, caughtAt: string): string {
  const a = parseUtc(createdAt)
  const b = parseUtc(caughtAt)
  if (!a || !b) return "unknown"
  return formatDuration((b.getTime() - a.getTime()) / 1000)
}

export function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "n/a" : `${value.toFixed(1)}%`
}

/** Human-readable message for a failed dashboard API call. */
export function apiErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { error?: string; message?: string } | null
    switch (body?.error) {
      case "forbidden":
        return body.message ?? "You do not have permission to do that."
      case "already_configured":
        return "A honeypot channel already exists. Reload the page."
      case "discord_forbidden":
        return "The bot is missing a Discord permission (for example Manage Channels)."
      case "appeal_not_found":
        return "That appeal no longer exists."
      case "not_a_honeypot_ban":
        return "This user is currently banned for a different reason (not the honeypot), so nobody was unbanned and the appeal is still pending. Review that ban manually, or deny the appeal."
      case "too_soon":
        return "The names were changed less than 10 minutes ago. Discord allows 2 renames per 10 minutes, so try again shortly."
      case "rate_limited":
        return "Discord is rate limiting renames right now (2 per 10 minutes). Try again in a few minutes."
      case "channel_missing":
        return "The honeypot channel no longer exists. Recreate it first."
      case "conflict":
        return "That appeal was already reviewed."
      case "invalid_request":
        return body.message ?? "The server rejected those values."
      case "internal_api_unavailable":
        return "The bot is unreachable right now. Try again shortly."
    }
    if (err.status === 0) return "Network error. Check your connection."
  }
  return "Something went wrong. Try again."
}

export const ACTION_LABEL: Record<string, string> = {
  ban: "Ban",
  softban: "Softban",
  kick: "Kick",
}

export const ACTION_HELP: Record<string, string> = {
  ban: "Permanently bans the account and deletes recent messages.",
  softban: "Bans then immediately unbans, wiping recent messages but letting them rejoin.",
  kick: "Removes the account; it can rejoin right away.",
}
