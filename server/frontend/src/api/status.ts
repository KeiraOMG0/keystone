import { useQuery } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { useLiveConnected } from "@/live/useLive"

export interface ServiceStatus {
  status: "up" | "down"
  latency_ms?: number | null
  detail?: string
}

export interface StatusResponse {
  dashboard: ServiceStatus
  internal_api: ServiceStatus
  bot: ServiceStatus
}

async function fetchStatus(): Promise<StatusResponse> {
  return apiFetch<StatusResponse>("/api/status")
}

/**
 * Wraps GET /api/status — deliberately PUBLIC (no auth), unlike every other
 * hook in this folder, since this is the one page a user needs mid-outage.
 * Polls fairly aggressively (10s) since the whole point of this page is
 * "is it back up yet", and retries on failure instead of giving up (a
 * network-level failure here IS a legitimate status to display, handled by
 * the caller via `isError`, not something to suppress with infinite retry).
 */
export function useStatus() {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: ["status"],
    queryFn: fetchStatus,
    staleTime: 5_000,
    // LIVE: while the websocket is up, bot up/down TRANSITIONS invalidate this
    // query immediately (LiveProvider), so the 10s poll relaxes to a 60s
    // refresh of the latency numbers. Socket down (or a public/logged-out
    // page with no live layer): the original 10s poll.
    refetchInterval: connected ? 60_000 : 10_000,
    retry: 1,
  })
}
