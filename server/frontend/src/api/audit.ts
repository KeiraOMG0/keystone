import { useEffect, useRef, useState } from "react"
import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { useLiveConnected, useLiveTopic } from "@/live/useLive"

/**
 * One audit_log row as GET /api/audit and the live `audit` event send it
 * (services/audit_service.py serialize_row). Every id except the row's own
 * `id` is a string. `detail` is the parsed detail_json (snowflakes inside are
 * strings); `detail_raw` is set only when detail_json did not parse.
 * `actor_name` / `target_name` come from the bot's cache on list responses
 * only (absent on live rows).
 */
export interface AuditEntry {
  id: number
  guild_id: string | null
  actor_id: string | null
  action: string
  target_type: string | null
  target_id: string | null
  detail: unknown
  detail_raw: string | null
  /** ISO 8601 UTC ("...Z") */
  created_at: string | null
  actor_name?: string | null
  target_name?: string | null
}

export interface AuditPage {
  entries: AuditEntry[]
  next_before_id: number | null
}

export interface AuditActionCount {
  action: string
  count: number
}

/** Active filters. `since` / `until` are ISO datetimes (until exclusive). */
export interface AuditFilters {
  action?: string
  actorId?: string
  targetId?: string
  since?: string
  until?: string
}

export const AUDIT_PAGE_SIZE = 50

/** Query root "audit" is in LIVE_ROOTS (src/live/LiveProvider.tsx): refetched after a reconnect. */
export const auditKeys = {
  all: ["audit"] as const,
  log: (f: AuditFilters) =>
    ["audit", "log", f.action ?? "", f.actorId ?? "", f.targetId ?? "", f.since ?? "", f.until ?? ""] as const,
  actions: ["audit", "actions"] as const,
}

function auditUrl(f: AuditFilters, beforeId: number | undefined): string {
  const p = new URLSearchParams()
  if (f.action) p.set("action", f.action)
  if (f.actorId) p.set("actor_id", f.actorId)
  if (f.targetId) p.set("target_id", f.targetId)
  if (f.since) p.set("since", f.since)
  if (f.until) p.set("until", f.until)
  if (beforeId !== undefined) p.set("before_id", String(beforeId))
  p.set("limit", String(AUDIT_PAGE_SIZE))
  return `/api/audit?${p.toString()}`
}

/**
 * GET /api/audit (requires "view_audit"), keyset-paginated: "Load more" asks
 * for the rows older than the last one shown. Live via useAuditLive; polls
 * every 30s only while the socket is down.
 */
export function useAuditLog(filters: AuditFilters) {
  const connected = useLiveConnected()
  return useInfiniteQuery({
    queryKey: auditKeys.log(filters),
    queryFn: ({ pageParam }) => apiFetch<AuditPage>(auditUrl(filters, pageParam)),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.next_before_id ?? undefined,
    refetchInterval: connected ? false : 30_000,
  })
}

/** GET /api/audit/actions: action names + counts for the filter dropdown. */
export function useAuditActions(enabled: boolean) {
  return useQuery({
    queryKey: auditKeys.actions,
    queryFn: () => apiFetch<{ actions: AuditActionCount[] }>("/api/audit/actions"),
    enabled,
    staleTime: 60_000,
  })
}

/** Does a (live) row fall inside the active filters? */
export function matchesFilters(row: AuditEntry, f: AuditFilters): boolean {
  if (f.action && row.action !== f.action) return false
  if (f.actorId && row.actor_id !== f.actorId) return false
  if (f.targetId && row.target_id !== f.targetId) return false
  if (row.created_at) {
    const t = Date.parse(row.created_at)
    if (f.since && t < Date.parse(f.since)) return false
    if (f.until && t >= Date.parse(f.until)) return false
  }
  return true
}

/**
 * LIVE `audit` topic (requires "view_audit"). Mount once on the Audit log page,
 * with `enabled` = the log query has data. A row matching the active filters
 * is prepended to the first loaded page (deduped by id, so a replay is
 * harmless); a row without an id invalidates instead. The action counts are
 * bumped in place (a never-seen action re-fetches the list). Returns the ids
 * that just arrived, for a short highlight.
 */
export function useAuditLive(filters: AuditFilters, enabled: boolean): { freshIds: ReadonlySet<number> } {
  const qc = useQueryClient()
  const [freshIds, setFresh] = useState<ReadonlySet<number>>(new Set())
  const timers = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())

  useEffect(() => {
    const pending = timers.current
    return () => {
      pending.forEach(clearTimeout)
      pending.clear()
    }
  }, [])

  useLiveTopic(
    "audit",
    (msg) => {
      const d = msg.data as { kind?: string; row?: AuditEntry }
      if (d.kind !== "audit") return
      const row = d.row
      if (!row || typeof row.id !== "number") {
        void qc.invalidateQueries({ queryKey: auditKeys.all })
        return
      }

      let isNewAction = false
      qc.setQueryData<{ actions: AuditActionCount[] }>(auditKeys.actions, (old) => {
        if (!old) return old
        if (!old.actions.some((a) => a.action === row.action)) {
          isNewAction = true
          return old
        }
        return { actions: old.actions.map((a) => (a.action === row.action ? { ...a, count: a.count + 1 } : a)) }
      })
      if (isNewAction) void qc.invalidateQueries({ queryKey: auditKeys.actions })

      if (!matchesFilters(row, filters)) return
      let added = false
      qc.setQueryData<InfiniteData<AuditPage, number | undefined>>(auditKeys.log(filters), (old) => {
        if (!old || old.pages.length === 0) return old
        if (old.pages.some((p) => p.entries.some((e) => e.id === row.id))) return old
        // Newest first: only prepend rows newer than what is shown (a late,
        // older event would belong further down; the next refetch places it).
        const top = old.pages[0].entries[0]
        if (top && top.id > row.id) return old
        added = true
        const [first, ...rest] = old.pages
        return { ...old, pages: [{ ...first, entries: [row, ...first.entries] }, ...rest] }
      })
      if (!added) return
      setFresh((prev) => new Set(prev).add(row.id))
      const t = setTimeout(() => {
        timers.current.delete(t)
        setFresh((prev) => {
          const next = new Set(prev)
          next.delete(row.id)
          return next
        })
      }, 6_000)
      timers.current.add(t)
    },
    { enabled }
  )

  return { freshIds }
}
