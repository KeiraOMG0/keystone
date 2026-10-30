import { ApiError, apiFetch } from "@/api/client"
import type { PublicAppealErrorCode, PublicAppealLookup, PublicAppealMe } from "@/types/api"

/**
 * PUBLIC appeal API (server/routes/appeals.py + appeal_auth.py) - no dashboard
 * session needed. Submitting requires Discord sign-in (appeal-auth/submit); the
 * DM-link token is a read-only status lookup. Errors are a flat {"error": code}.
 */

const KNOWN: string[] = [
  "not_eligible",
  "already_submitted",
  "rate_limited",
  "unavailable",
  "invalid_request",
  "not_signed_in",
  "signin_required",
]

/** Normalises any thrown value into one of the public error codes. */
export function appealErrorCode(err: unknown): PublicAppealErrorCode {
  if (err instanceof ApiError) {
    const code = (err.body as { error?: string } | null)?.error
    if (code && KNOWN.includes(code)) return code as PublicAppealErrorCode
    if (err.status === 429) return "rate_limited"
  }
  return "unavailable"
}

export function lookupAppealToken(token: string): Promise<PublicAppealLookup> {
  return apiFetch<PublicAppealLookup>(`/api/public/appeals/${encodeURIComponent(token)}`)
}

/**
 * Discord sign-in for appeals (server/routes/appeal_auth.py). A full-page
 * navigation, not fetch: it redirects to Discord's consent screen and back to
 * /appeal?signin=... or /appeal?rejoin=.... "rejoin" also asks for guilds.join
 * so the bot can add an approved person back; the token is used once and revoked.
 */
export function appealLoginUrl(intent: "appeal" | "rejoin"): string {
  return `/api/public/appeal-auth/login?intent=${intent}`
}

/** The signed-in person's own appeal state (identity proven by Discord login). */
export function fetchAppealMe(): Promise<PublicAppealMe> {
  return apiFetch<PublicAppealMe>("/api/public/appeal-auth/me")
}

/** Appeal as the signed-in person: verified, honest errors (not_eligible / already_submitted). */
export function submitSignedInAppeal(body: { name: string; message: string }): Promise<{ ok: boolean; status: string }> {
  return apiFetch<{ ok: boolean; status: string }>("/api/public/appeal-auth/submit", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

export function appealSignOut(): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>("/api/public/appeal-auth/logout", { method: "POST" })
}
