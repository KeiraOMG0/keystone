import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { useLive, useLiveTopic } from "@/live/useLive"

/**
 * Loosely typed on purpose: the dump is a large, evolving, per-section
 * fail-soft structure (any section can be `{ error }` instead of its normal
 * shape), so the page reads it defensively rather than pretending every field
 * is guaranteed. Only the envelope is typed.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Sec = Record<string, any>

export interface BotDump {
  reachable: boolean
  round_trip_ms?: number
  error?: string
  status_code?: number
  generated_at?: string
  guild_id?: string
  psutil_available?: boolean
  process?: Sec
  children?: Sec
  system?: Sec
  discord?: Sec
  database?: Sec
  services?: Sec
  cookies?: Sec
  config?: Sec
  logs?: Sec
  activity?: Sec
}

export interface DiagnosticsResponse {
  generated_at: string
  viewer: { user_id: string; username: string }
  bot: BotDump
  dashboard: Sec
}

export const diagnosticsKey = ["diagnostics"] as const

// ---------------------------------------------------------------------------
// Delta stream (server/live/hub.py push_diagnostics; docs/ARCHITECTURE.md
// section 22). One full `snapshot` per subscribe / resync / cache clear, then
// `delta` envelopes: data = {set: [[path, value], ...], unset: [path, ...]},
// `rev` = this socket's revision after the message, `base_rev` = the revision
// it applies to.
// ---------------------------------------------------------------------------

type Path = Array<string | number>
export interface DiagnosticsDelta {
  set?: Array<[Path, unknown]>
  unset?: Path[]
}

function isObj(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v)
}

function setIn(obj: unknown, path: Path, value: unknown, i = 0): unknown {
  if (i >= path.length) return value
  const base = isObj(obj) ? obj : {}
  const k = String(path[i])
  return { ...base, [k]: setIn(base[k], path, value, i + 1) }
}

function unsetIn(obj: unknown, path: Path, i = 0): unknown {
  if (!isObj(obj) || i >= path.length) return obj
  const k = String(path[i])
  if (!(k in obj)) return obj
  if (i === path.length - 1) {
    const next = { ...obj }
    delete next[k]
    return next
  }
  return { ...obj, [k]: unsetIn(obj[k], path, i + 1) }
}

/** Pure, immutable: copies only the objects along each changed path. */
export function applyDelta<T>(base: T, delta: DiagnosticsDelta): T {
  let out: unknown = base
  for (const entry of delta.set ?? []) {
    if (Array.isArray(entry) && Array.isArray(entry[0]) && entry[0].length) out = setIn(out, entry[0], entry[1])
  }
  for (const path of delta.unset ?? []) {
    if (Array.isArray(path) && path.length) out = unsetIn(out, path)
  }
  return out as T
}

const RESYNC_THROTTLE_MS = 3_000

/**
 * Which stream revision the cached query data is, and when we wrote it. Shared
 * by every mounted useDiagnostics (one socket per tab), so a second mount's
 * handler sees the delta as already applied (idempotent). `at` is the query's
 * dataUpdatedAt after our own write: if anything else (the HTTP fetch, the
 * offline poll) wrote the cache since, deltas no longer fit it.
 */
const stream: { rev: number | null; at: number; askedAt: number } = { rev: null, at: 0, askedAt: 0 }

/**
 * Wraps GET /api/diagnostics (STAFF ONLY - require_staff server-side).
 *
 * LIVE: the `diagnostics` topic sends one full snapshot when this tab
 * subscribes, then about every 2s a DELTA with only the paths that changed
 * since what this socket last received. Both are written into this query with
 * setQueryData (deltas merged immutably), so `data` and `dataUpdatedAt`
 * behave exactly as with polling (the page's sparkline history keeps sampling
 * on every update). A delta that does not fit the cached data (no base yet, a
 * missed message, or the cache was replaced by an HTTP fetch) is dropped and a
 * fresh snapshot is requested (`resync`), so a partial object is never
 * rendered. While `paused` the component unsubscribes, which also stops the
 * bot-side collector. The 5s poll runs only while the socket is down.
 */
export function useDiagnostics(paused: boolean) {
  const qc = useQueryClient()
  const { connected, resync } = useLive()
  useLiveTopic(
    "diagnostics",
    (msg) => {
      if (msg.type === "snapshot" || msg.type === "event") {
        qc.setQueryData(diagnosticsKey, msg.data as unknown as DiagnosticsResponse)
        stream.rev = typeof msg.rev === "number" ? msg.rev : null
        stream.at = qc.getQueryState(diagnosticsKey)?.dataUpdatedAt ?? 0
        stream.askedAt = 0
        return
      }
      if (msg.type !== "delta") return
      const state = qc.getQueryState<DiagnosticsResponse>(diagnosticsKey)
      const ours = stream.rev !== null && state?.data !== undefined && state.dataUpdatedAt === stream.at
      if (ours && msg.rev === stream.rev) return // already applied by another mounted handler
      if (!ours || msg.base_rev !== stream.rev || !state?.data) {
        if (Date.now() - stream.askedAt > RESYNC_THROTTLE_MS) {
          stream.askedAt = Date.now()
          resync("diagnostics")
        }
        return
      }
      qc.setQueryData(diagnosticsKey, applyDelta(state.data, msg.data as unknown as DiagnosticsDelta))
      stream.rev = typeof msg.rev === "number" ? msg.rev : null
      stream.at = qc.getQueryState(diagnosticsKey)?.dataUpdatedAt ?? 0
    },
    { enabled: !paused }
  )
  return useQuery({
    queryKey: diagnosticsKey,
    queryFn: () => apiFetch<DiagnosticsResponse>("/api/diagnostics"),
    refetchInterval: paused || connected ? false : 5_000,
    refetchIntervalInBackground: false,
    staleTime: 0,
    retry: 1,
    placeholderData: (prev) => prev,
  })
}

export interface ClearDiagnosticsCacheResponse {
  ok: boolean
  /** cache keys that were dropped bot-side (e.g. "database", "config") */
  cleared: string[]
  entries: number
  log_cache_cleared: boolean
  at: string | null
}

/**
 * POST /api/diagnostics/cache/clear (staff). Empties the bot's diagnostics
 * caches and makes the hub send every open Diagnostics page a full fresh
 * snapshot right away, so no invalidation is needed here. Needs the bot: 502
 * internal_api_unavailable while it is down.
 */
export function useClearDiagnosticsCache() {
  return useMutation({
    mutationFn: () => apiFetch<ClearDiagnosticsCacheResponse>("/api/diagnostics/cache/clear", { method: "POST" }),
  })
}
