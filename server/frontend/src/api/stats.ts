import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { useLiveConnected, useLiveTopic } from "@/live/useLive"
import type { DashboardStatsResponse } from "@/types/api"

async function fetchDashboardStats(signal?: AbortSignal): Promise<DashboardStatsResponse> {
  return apiFetch<DashboardStatsResponse>("/api/dashboard/stats", { signal })
}

export const dashboardStatsKey = ["dashboard-stats"] as const

/** A live `stats` payload is only applied when it has the REST shape (never blank the tiles with a partial push). */
function isStatsPayload(data: unknown): data is DashboardStatsResponse {
  if (!data || typeof data !== "object") return false
  const d = data as Partial<DashboardStatsResponse>
  return typeof d.total === "number" && typeof d.presence_available === "boolean"
}

/**
 * When presence is reported available but a single count is momentarily null,
 * keep the previous number instead of rendering it as 0 (the tiles flickered).
 */
function mergeStats(prev: DashboardStatsResponse | undefined, next: DashboardStatsResponse): DashboardStatsResponse {
  if (!prev || !next.presence_available) return next
  return {
    ...next,
    online: next.online ?? prev.online,
    idle: next.idle ?? prev.idle,
    dnd: next.dnd ?? prev.dnd,
    offline: next.offline ?? prev.offline,
  }
}

/**
 * Wraps GET /api/dashboard/stats (requires the "staff_base" permission server-side).
 *
 * LIVE: the `stats` topic pushes the same payload every ~5s while this hook is
 * mounted (snapshots replace the cache entry). The 30s poll only runs while the
 * socket is down. Never flashes: previous data is kept through refetches
 * (placeholderData) and for 30 min after leaving the page (gcTime), so the
 * Overview shows skeletons only on the very first load.
 */
export function useDashboardStats() {
  const qc = useQueryClient()
  const connected = useLiveConnected()
  useLiveTopic("stats", (msg) => {
    if (!isStatsPayload(msg.data)) return
    const next = msg.data
    qc.setQueryData<DashboardStatsResponse>(dashboardStatsKey, (prev) => mergeStats(prev, next))
  })
  return useQuery({
    queryKey: dashboardStatsKey,
    queryFn: async ({ signal }) => mergeStats(qc.getQueryData<DashboardStatsResponse>(dashboardStatsKey), await fetchDashboardStats(signal)),
    staleTime: 15_000,
    gcTime: 30 * 60_000,
    placeholderData: keepPreviousData,
    refetchInterval: connected ? false : 30_000,
  })
}
