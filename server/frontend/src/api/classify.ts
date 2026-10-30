/**
 * Pure (import-free) failure classification, shared by apiFetch, useMe and
 * AuthContext. Kept dependency-free on purpose so it can be checked with plain
 * node (see docs/ARCHITECTURE.md "Stay logged in" for the manual check).
 *
 * THE RULE: the user is logged out ONLY by a real 401 whose JSON body is
 * {"error":"unauthenticated"} (server/auth/dependencies.py require_session).
 * Everything else that is not a definitive JSON answer from the API is
 * "transient": the site/bot/tunnel is down or restarting, and the session may
 * be perfectly fine.
 */

/** Statuses that mean "the path to the API is broken", not "the API said no". */
export function isTransientStatus(status: number): boolean {
  return (
    status === 0 || // network error / fetch threw
    status === 408 || // request timeout
    status === 425 ||
    status === 429 || // rate limited: back off, do not log out
    (status >= 500 && status <= 599) // 500/502/503/504, Cloudflare 52x and 530
  )
}

/**
 * @param status  HTTP status (0 for a network failure)
 * @param isJson  whether the response body was a JSON document. A non-JSON
 *                error body (Cloudflare 52x/1xxx HTML page, proxy plain text,
 *                empty body) is never the API talking, whatever the status.
 */
export function isTransientFailure(status: number, isJson: boolean): boolean {
  return isTransientStatus(status) || !isJson
}

export type AuthFailure = "unauthenticated" | "not_a_member" | "transient"

/**
 * Classify a failed GET /api/me. `body` is the (detail-unwrapped) JSON body or
 * null. Only the two definitive shapes are non-transient; any unexpected shape
 * is treated as transient so it can never log the user out.
 */
export function classifyAuthFailure(status: number, body: unknown, transient: boolean): AuthFailure {
  if (!transient && body !== null && typeof body === "object") {
    const code = (body as { error?: unknown }).error
    if (status === 401 && code === "unauthenticated") return "unauthenticated"
    if (status === 403 && code === "not_a_member") return "not_a_member"
  }
  return "transient"
}

/** Capped exponential backoff with jitter (50-100% of the ceiling). */
export function backoffDelay(attempt: number, capMs = 15_000, baseMs = 500, rand: () => number = Math.random): number {
  const ceiling = Math.min(capMs, baseMs * 2 ** Math.min(attempt, 30))
  return Math.round(ceiling * (0.5 + 0.5 * rand()))
}
