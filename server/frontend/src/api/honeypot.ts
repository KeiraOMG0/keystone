import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { useLiveConnected, useLiveTopic } from "@/live/useLive"
import { invalidateSoon } from "@/live/invalidate"
import type {
  AppealApproveResponse,
  AppealStatus,
  HoneypotAppealsResponse,
  HoneypotCatchesResponse,
  HoneypotConfig,
  HoneypotCatch,
  HoneypotConfigUpdate,
  HoneypotScrambleResponse,
  HoneypotSetupInput,
  HoneypotSetupResponse,
  HoneypotStats,
} from "@/types/api"

const configKey = ["honeypot", "config"] as const
const catchesKey = ["honeypot", "catches"] as const
const statsKey = ["honeypot", "stats"] as const
const appealsKey = (status: string) => ["honeypot", "appeals", status] as const

/** GET /api/honeypot/config (requires "moderate_members"). */
export function useHoneypotConfig() {
  return useQuery({
    queryKey: configKey,
    queryFn: ({ signal }) => apiFetch<HoneypotConfig>("/api/honeypot/config", { signal }),
  })
}

/**
 * LIVE `honeypot` topic (requires "moderate_members"). Mount ONCE per page
 * (HoneypotPage): applies every event to the query cache and returns the ids of
 * catches that just arrived so the feed can animate them in.
 *
 *   catch_created    -> prepended to the catches feed (deduped by id)
 *   stats            -> replaces the stats dialog's totals in place
 *   config_changed   -> replaces the config
 *   appeal_submitted / appeal_approved / appeal_denied -> appeals + catches
 *                       (the appeal badge on a catch) are re-fetched
 * Events are deltas; LiveProvider re-fetches everything after a reconnect.
 */
export function useHoneypotLive(): { freshIds: ReadonlySet<number> } {
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

  useLiveTopic("honeypot", (msg) => {
    const d = msg.data as { kind?: string; catch?: HoneypotCatch; stats?: HoneypotStats; config?: HoneypotConfig }
    switch (d.kind) {
      case "catch_created": {
        const c = d.catch
        if (!c) return
        qc.setQueryData<HoneypotCatchesResponse>(catchesKey, (old) => {
          if (!old) return old // not loaded yet: the initial fetch already includes it
          if (old.catches.some((x) => x.id === c.id)) return old
          return { ...old, catches: [c, ...old.catches].slice(0, 200) }
        })
        setFresh((prev) => new Set(prev).add(c.id))
        const t = setTimeout(() => {
          timers.current.delete(t)
          setFresh((prev) => {
            const next = new Set(prev)
            next.delete(c.id)
            return next
          })
        }, 8_000)
        timers.current.add(t)
        return
      }
      case "stats":
        if (d.stats) qc.setQueryData(statsKey, d.stats)
        return
      case "config_changed":
        if (d.config) qc.setQueryData(configKey, d.config)
        return
      case "appeal_submitted":
      case "appeal_approved":
      case "appeal_denied":
      case "appeal_rejoined":
      case "appeals_changed": // Debug-page test cases deleted
        // Coalesced: the sidebar's pending-appeals badge handles the same event.
        invalidateSoon(qc, ["honeypot", "appeals"], catchesKey)
        return
    }
  })

  return { freshIds }
}

/** GET /api/honeypot/catches - live via useHoneypotLive; polls every 15s only while the socket is down. */
export function useHoneypotCatches(enabled: boolean) {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: catchesKey,
    queryFn: ({ signal }) => apiFetch<HoneypotCatchesResponse>("/api/honeypot/catches?limit=50", { signal }),
    enabled,
    refetchInterval: connected ? false : 15_000,
  })
}

/** GET /api/honeypot/stats - only fetched while the stats dialog is open. */
export function useHoneypotStats(enabled: boolean) {
  return useQuery({
    queryKey: statsKey,
    queryFn: ({ signal }) => apiFetch<HoneypotStats>("/api/honeypot/stats", { signal }),
    enabled,
  })
}

/** GET /api/honeypot/appeals?status= */
export function useHoneypotAppeals(status: AppealStatus | "all", enabled = true) {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: appealsKey(status),
    queryFn: ({ signal }) => apiFetch<HoneypotAppealsResponse>(`/api/honeypot/appeals?status=${status}`, { signal }),
    enabled,
    refetchInterval: connected ? false : 30_000, // live appeal_* events invalidate it; poll only while down
  })
}

/** PUT /api/honeypot/config (requires "ban_members"). */
export function useUpdateHoneypotConfig() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: HoneypotConfigUpdate) =>
      apiFetch<HoneypotConfig>("/api/honeypot/config", { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: (data) => qc.setQueryData(configKey, data),
  })
}

/** POST /api/honeypot/setup (requires "ban_members"). */
export function useSetupHoneypot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: HoneypotSetupInput) =>
      apiFetch<HoneypotSetupResponse>("/api/honeypot/setup", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: (data) => {
      qc.setQueryData(configKey, data.config)
      qc.invalidateQueries({ queryKey: catchesKey })
    },
  })
}

/** POST /api/honeypot/invite (requires "ban_members"): bot creates a permanent rejoin invite. */
export function useCreateRejoinInvite() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (channel_id: string) =>
      apiFetch<HoneypotConfig>("/api/honeypot/invite", { method: "POST", body: JSON.stringify({ channel_id }) }),
    onSuccess: (data) => qc.setQueryData(configKey, data),
  })
}

/** POST /api/honeypot/scramble (requires "ban_members"): rename channel + category now. */
export function useScrambleHoneypot() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => apiFetch<HoneypotScrambleResponse>("/api/honeypot/scramble", { method: "POST" }),
    onSuccess: (data) => qc.setQueryData(configKey, data.config),
  })
}

interface ReviewInput {
  appeal_id: number
  note: string | null
}

function invalidateAppeals(qc: ReturnType<typeof useQueryClient>) {
  // Coalesced with the appeal_* live event the bot publishes for the same action.
  invalidateSoon(qc, ["honeypot", "appeals"], catchesKey, statsKey)
}

/** POST /api/honeypot/appeals/{id}/approve - unbans the user. */
export function useApproveAppeal() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ appeal_id, note }: ReviewInput) =>
      apiFetch<AppealApproveResponse>(`/api/honeypot/appeals/${appeal_id}/approve`, {
        method: "POST",
        body: JSON.stringify({ note }),
      }),
    onSuccess: () => invalidateAppeals(qc),
  })
}

/** POST /api/honeypot/appeals/{id}/deny */
export function useDenyAppeal() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ appeal_id, note }: ReviewInput) =>
      apiFetch<{ ok: boolean }>(`/api/honeypot/appeals/${appeal_id}/deny`, {
        method: "POST",
        body: JSON.stringify({ note }),
      }),
    onSuccess: () => invalidateAppeals(qc),
  })
}
