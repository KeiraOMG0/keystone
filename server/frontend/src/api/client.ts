/**
 * Same-origin fetch wrapper. No base URL — the frontend is served by the
 * same FastAPI process as the API in production (and proxied to it in
 * local dev via vite.config.ts's server.proxy), so all calls use relative
 * paths like "/api/me".
 *
 * `credentials: "include"` is required so the httpOnly session cookie
 * (server/config.py's SESSION_COOKIE_NAME) is sent on every request.
 */

import { isTransientFailure, isTransientStatus } from "@/api/classify"

/**
 * Thrown on any non-2xx response. Carries the parsed JSON body (when the
 * response had one) so callers can inspect shapes like
 * {"error": "not_a_member"} or {"error": "forbidden", "required_permission": ...}
 * per server/permissions/guards.py and server/routes/me.py.
 */
export class ApiError extends Error {
  status: number
  /** Parsed JSON response body, if the response had a JSON content type. */
  body: unknown
  /**
   * True when this failure says nothing about the user's session: network
   * error, 408/425/429/5xx (incl. Cloudflare 52x/530), or a non-JSON error
   * body such as a Cloudflare HTML error page. Never treat these as "logged out".
   */
  transient: boolean

  constructor(status: number, body: unknown, message?: string, transient?: boolean) {
    super(message ?? `Request failed with status ${status}`)
    this.name = "ApiError"
    this.status = status
    this.body = body
    this.transient = transient ?? isTransientStatus(status)
  }
}

interface ParsedBody {
  body: unknown
  isJson: boolean
}

async function parseBody(response: Response): Promise<ParsedBody> {
  const contentType = response.headers.get("content-type") ?? ""
  if (!contentType.includes("application/json")) {
    return { body: null, isJson: false }
  }
  try {
    return { body: await response.json(), isJson: true }
  } catch {
    return { body: null, isJson: false }
  }
}

/** True for the error fetch() throws when its AbortSignal fires. */
export function isAbortError(error: unknown): boolean {
  return (
    (typeof DOMException !== "undefined" && error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  )
}

/**
 * `init.signal` is passed straight to fetch(). Query functions forward
 * react-query's `signal` (`queryFn: ({ signal }) => apiFetch(url, { signal })`)
 * so a request nobody is waiting for any more (page left, key changed, query
 * cancelled) is really aborted. An abort is re-thrown as the original
 * AbortError, never as an ApiError, so it can never be read as an auth
 * verdict or shown as "Network error" (react-query treats it as a
 * cancellation and keeps the previous data).
 */
export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      credentials: "include",
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    })
  } catch (networkError) {
    if (init?.signal?.aborted || isAbortError(networkError)) throw networkError
    // Network failure / server unreachable — no HTTP status to report.
    throw new ApiError(0, null, networkError instanceof Error ? networkError.message : "Network error")
  }

  const { body, isJson } = await parseBody(response)

  if (!response.ok) {
    // FastAPI's HTTPException(detail=...) shows up under body.detail.
    const detail =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : body
    // Cloudflare / proxy error pages (HTML, plain text, empty): status is kept,
    // body stays null, and the failure is flagged transient so callers never
    // read it as an auth verdict.
    throw new ApiError(response.status, detail, undefined, isTransientFailure(response.status, isJson))
  }

  // A 2xx HTML document where JSON was expected is a proxy/interstitial page
  // (e.g. a tunnel or Access login screen), not the API.
  if ((response.headers.get("content-type") ?? "").includes("text/html")) {
    throw new ApiError(response.status, null, "Unexpected HTML response from the server", true)
  }

  return body as T
}
