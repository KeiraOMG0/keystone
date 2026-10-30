import { ApiError } from "@/api/client"

/**
 * One readable sentence for any error a mutation or query can throw. Shared by
 * every page (replaces the per-page `formatApiError` copies). Handles the body
 * shapes the dashboard backend returns:
 *   {"detail": "text"}                       HTTPException with a string
 *   {"detail": [{"msg": ..., "loc": [...]}]} FastAPI 422 validation errors
 *   {"detail": {"error": code, "message"?}}  HTTPException with a dict
 *   {"error": code, "message"?}              custom JSONResponse bodies
 *   {"message": "text"}
 * and falls back to a status-based sentence, then to `fallback`.
 */
export function describeApiError(error: unknown, fallback = "Something went wrong."): string {
  if (!(error instanceof ApiError)) {
    if (error instanceof Error && error.message) return error.message
    return fallback
  }
  const fromBody = messageFromBody(error.body)
  if (fromBody) return fromBody
  return statusMessage(error.status) ?? (error.message || fallback)
}

const ERROR_CODES: Record<string, string> = {
  internal_api_unavailable: "The bot is offline or restarting. Try again in a moment.",
  rate_limited: "Too many requests. Wait a minute and try again.",
  forbidden: "You don't have permission to do that.",
  not_found: "Not found.",
  unauthenticated: "Your session has expired. Sign in again.",
}

function messageFromBody(body: unknown): string | null {
  if (!body || typeof body !== "object") return null
  const b = body as Record<string, unknown>
  const detail = b.detail
  if (typeof detail === "string" && detail) return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        if (typeof d === "string") return d
        if (d && typeof d === "object" && typeof (d as { msg?: unknown }).msg === "string") {
          const loc = (d as { loc?: unknown }).loc
          const field = Array.isArray(loc) && loc.length ? String(loc[loc.length - 1]) : ""
          const msg = (d as { msg: string }).msg
          return field && field !== "body" ? `${field}: ${msg}` : msg
        }
        return null
      })
      .filter((p): p is string => !!p)
    if (parts.length) return parts.join("; ")
  }
  if (detail && typeof detail === "object") {
    const nested = messageFromBody(detail)
    if (nested) return nested
  }
  if (typeof b.message === "string" && b.message) return b.message
  if (typeof b.error === "string" && b.error) return ERROR_CODES[b.error] ?? b.error.replace(/_/g, " ")
  return null
}

function statusMessage(status: number): string | null {
  if (status === 0) return "Can't reach the dashboard. Check your connection."
  if (status === 401) return ERROR_CODES.unauthenticated
  if (status === 403) return ERROR_CODES.forbidden
  if (status === 404) return ERROR_CODES.not_found
  if (status === 429) return ERROR_CODES.rate_limited
  if (status === 502 || status === 503) return ERROR_CODES.internal_api_unavailable
  if (status === 504 || status === 524) return "The request took too long. It may still finish; refresh in a moment."
  return null
}
