import { useEffect, useSyncExternalStore } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import type { StatusResponse } from "@/api/status"
import { supervisorStatusKey } from "@/api/supervisor"
import { useToast } from "@/components/ui/toast"
import type { SupervisorServiceName, SupervisorStatusResponse, SupervisorTarget } from "@/types/api"

/**
 * The "Restarting..." watch for Diagnostics -> Services, kept OUTSIDE the
 * SupervisorPanel (a module store) so collapsing the section, or the page
 * moving the panel between its bot-down and normal positions, never drops it.
 * `useSupervisorRestartWatcher()` (mounted once by DiagnosticsPage) does the
 * polling; the panel only reads and starts it.
 */

export const RESTART_LABEL: Record<SupervisorTarget, string> = { bot: "bot", dashboard: "dashboard", tunnel: "tunnel", all: "everything" }
/** How long to wait for the target to come back before reporting an error. */
export const RESTART_TIMEOUT_S: Record<SupervisorTarget, number> = { bot: 120, dashboard: 120, tunnel: 60, all: 180 }
const POLL_MS = 2000
const FETCH_TIMEOUT_MS = 8000

export interface PendingRestart {
  target: SupervisorTarget
  names: SupervisorServiceName[]
  oldPids: Partial<Record<SupervisorServiceName, number | null>>
  startedAt: number
}

interface WatchState {
  pending: PendingRestart | null
  timedOut: string | null
}

let state: WatchState = { pending: null, timedOut: null }
const listeners = new Set<() => void>()

function setState(next: Partial<WatchState>) {
  state = { ...state, ...next }
  for (const l of listeners) l()
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function startRestartWatch(pending: PendingRestart) {
  setState({ pending, timedOut: null })
}

export function clearRestartTimeout() {
  setState({ timedOut: null })
}

export function useRestartWatchState(): WatchState {
  return useSyncExternalStore(subscribe, () => state)
}

function withTimeout(): RequestInit {
  return typeof AbortSignal !== "undefined" && "timeout" in AbortSignal ? { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) } : {}
}

/**
 * Polls until every target runs with a NEW pid (and, for the bot, the public
 * /api/status says the bot is up), tolerating fetch failures during the
 * dashboard/tunnel gap. One check at a time; the store is re-checked after
 * every await and cleared synchronously before the toast, so overlapping
 * checks can never toast twice.
 */
export function useSupervisorRestartWatcher() {
  const qc = useQueryClient()
  const { toast } = useToast()
  const { pending } = useRestartWatchState()

  useEffect(() => {
    if (!pending) return
    let cancelled = false
    let inFlight = false
    const label = RESTART_LABEL[pending.target]
    const limit = RESTART_TIMEOUT_S[pending.target]
    const isCurrent = () => !cancelled && state.pending === pending

    async function check() {
      if (!isCurrent()) return
      const waited = (Date.now() - pending!.startedAt) / 1000
      if (waited > limit) {
        const msg = `The ${label} did not come back within ${limit}s. Check logs/supervisor/ on the host, or the output below.`
        setState({ pending: null, timedOut: msg })
        toast({ title: "Restart not confirmed", description: msg, variant: "destructive" })
        return
      }
      if (inFlight) return
      inFlight = true
      try {
        let status: SupervisorStatusResponse
        let pub: StatusResponse | null = null
        try {
          status = await apiFetch<SupervisorStatusResponse>("/api/supervisor/status", withTimeout())
          if (pending!.names.includes("bot")) pub = await apiFetch<StatusResponse>("/api/status", withTimeout())
        } catch {
          return // dashboard/tunnel gap: keep polling
        }
        if (!isCurrent()) return
        qc.setQueryData(supervisorStatusKey, status)
        const back = pending!.names.every((n) => {
          const s = status.services.find((x) => x.name === n)
          return !!s && s.running && s.pid !== pending!.oldPids[n]
        })
        const botOk = !pending!.names.includes("bot") || pub?.bot.status === "up"
        if (back && botOk) {
          setState({ pending: null })
          toast({ title: `Restarted ${label}`, description: `Back after ${Math.round(waited)}s.`, variant: "success" })
          void qc.invalidateQueries({ queryKey: ["status"] })
          void qc.invalidateQueries({ queryKey: ["diagnostics"] })
        }
      } finally {
        inFlight = false
      }
    }

    const poll = setInterval(() => void check(), POLL_MS)
    return () => {
      cancelled = true
      clearInterval(poll)
    }
  }, [pending, qc, toast])
}
