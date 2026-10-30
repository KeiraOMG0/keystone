/**
 * Sends a browser error to the dashboard backend (POST /api/public/client-errors,
 * server/routes/client_errors.py), where it shows up on the owner-only Debug
 * page's Errors tab. Used by AppErrorBoundary and the window error listeners in
 * main.tsx.
 *
 * Deliberately dependency-free (no apiFetch, no providers): it runs while the app
 * is crashing. Never throws, never retries, and sends at most MAX_PER_PAGE
 * reports per page load, each distinct error once. The path is sent without its
 * query string, with appeal tokens hidden (the server scrubs again).
 */

export type ClientErrorKind = "render" | "error" | "rejection" | "chunk" | "test"

const ENDPOINT = "/api/public/client-errors"
const MAX_PER_PAGE = 10
const seen = new Set<string>()
let sent = 0

const CHUNK_ERROR =
  /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS|Loading chunk .* failed/i

export function isChunkLoadError(error: unknown): boolean {
  const { name, message } = normalize(error)
  return CHUNK_ERROR.test(`${name}: ${message}`)
}

function normalize(error: unknown): { name: string; message: string; stack?: string } {
  if (error instanceof Error) return { name: error.name || "Error", message: error.message, stack: error.stack }
  if (error && typeof error === "object" && "message" in error) {
    const e = error as { name?: unknown; message?: unknown; stack?: unknown }
    return {
      name: typeof e.name === "string" ? e.name : "Error",
      message: String(e.message),
      stack: typeof e.stack === "string" ? e.stack : undefined,
    }
  }
  return { name: "Error", message: typeof error === "string" ? error : String(error) }
}

/** Noise that says nothing about our code. */
function ignored(error: unknown, e: { name: string; message: string; stack?: string }): boolean {
  if (e.name === "AbortError") return true // navigation / react-query cancelled a fetch
  if (/ResizeObserver loop/i.test(e.message)) return true
  if (e.message === "Script error." && !e.stack) return true // cross-origin script, no detail
  if (e.stack && /(chrome|moz|safari(-web)?)-extension:\/\//.test(e.stack)) return true // a browser extension
  // API failures the page already shows (bot offline, no permission, ...): only report real 5xx bugs.
  if (error && typeof error === "object" && "status" in error && typeof (error as { status: unknown }).status === "number") {
    const status = (error as { status: number }).status
    if (status < 500 || status === 502 || status === 503 || status === 504) return true
  }
  return false
}

function currentPath(): string {
  return window.location.pathname.replace(/^\/appeal\/[^/]+/, "/appeal/:token")
}

/** Report one error. Returns true if a report was sent. */
export function reportClientError(kind: ClientErrorKind, error: unknown, extra?: { componentStack?: string | null }): boolean {
  try {
    const e = normalize(error)
    if (kind !== "test" && ignored(error, e)) return false
    const key = `${e.name}|${e.message}`
    if (seen.has(key) || sent >= MAX_PER_PAGE) return false
    seen.add(key)
    sent += 1
    const body = JSON.stringify({
      kind,
      name: e.name.slice(0, 200),
      message: e.message.slice(0, 2000),
      stack: e.stack ? e.stack.slice(0, 8000) : null,
      component_stack: extra?.componentStack ? extra.componentStack.slice(0, 4000) : null,
      path: currentPath(),
    })
    void fetch(ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      keepalive: true,
      body,
    }).catch(() => {})
    return true
  } catch {
    return false
  }
}

/** Hook up the global listeners once (main.tsx). Nothing here reloads the page. */
export function installGlobalErrorReporting(): void {
  window.addEventListener("error", (event) => {
    reportClientError("error", event.error ?? event.message)
  })
  window.addEventListener("unhandledrejection", (event) => {
    reportClientError("rejection", event.reason)
  })
  // A lazy chunk failed to load (usually an old open tab after a rebuild). Not
  // prevented: the import still rejects and AppErrorBoundary shows the error screen.
  window.addEventListener("vite:preloadError", (event) => {
    reportClientError("chunk", (event as Event & { payload?: unknown }).payload)
  })
}
