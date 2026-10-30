import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { useQueryClient, type QueryClient } from "@tanstack/react-query"
import { useAuth } from "@/hooks/useAuth"
import { LiveConnection } from "@/live/connection"
import { isRoleQueryKey } from "@/api/rolesLive"
import { LiveContext, type LiveContextValue } from "@/live/useLive"
import type { BotState, Envelope, LiveHandler, LiveState, LiveTopic } from "@/live/protocol"

/** Query-key roots the live topics feed; all are re-fetched to resync. */
const LIVE_ROOTS = new Set(["me", "dashboard-stats", "status", "diagnostics", "honeypot", "tickets", "moderation", "reaction-role-roles", "permissions", "role-admin", "audit", "birthdays", "automation", "music"])

/**
 * Events are DELTAS; after any gap (first connect, reconnect, bot restart) the
 * only correct move is to re-fetch. Active queries refetch now, inactive ones
 * are marked stale. `minAgeMs` skips queries that were fetched a moment ago
 * (avoids a duplicate request at page load). Besides LIVE_ROOTS this also
 * covers every role-carrying key (isRoleQueryKey: <ResolvedRole> caches and the
 * QOTD/Fortnite role pickers), without touching user/channel resolve caches.
 */
function resync(qc: QueryClient, minAgeMs: number): void {
  qc.invalidateQueries({
    predicate: (q) =>
      (LIVE_ROOTS.has(String(q.queryKey[0])) || isRoleQueryKey(q.queryKey)) && Date.now() - q.state.dataUpdatedAt >= minAgeMs,
  })
}

/**
 * Owns the ONE WebSocket of this tab. Mounted inside AuthProvider: it connects
 * once the session is known-good and stops on a real logout. A transient
 * outage never stops it (it retries forever).
 */
export function LiveProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient()
  const { state: authState } = useAuth()
  const [state, setState] = useState<LiveState>("connecting")
  const [bot, setBot] = useState<BotState>({ online: null, latencyMs: null })
  const botOnline = useRef<boolean | null>(null)

  const [conn] = useState(
    () =>
      new LiveConnection({
        onState: setState,
        onHello: ({ reconnect }) => resync(qc, reconnect ? 0 : 1500),
        onClose: (code) => {
          // 4401: session ended, 4403: forbidden. /api/me is the arbiter.
          if (code === 4401 || code === 4403) qc.invalidateQueries({ queryKey: ["me"] })
        },
      })
  )

  useEffect(() => {
    if (authState === "authenticated") conn.start()
    else if (authState === "unauthenticated" || authState === "not_a_member") conn.stop()
  }, [conn, authState])

  useEffect(() => () => conn.stop(), [conn])

  // The bot connected/disconnected state, for the whole app.
  useEffect(() => {
    const onBot: LiveHandler = (msg: Envelope) => {
      const d = msg.data as { online?: boolean; latency_ms?: number | null }
      const online = typeof d.online === "boolean" ? d.online : null
      const wasOffline = botOnline.current === false
      botOnline.current = online
      setBot({ online, latencyMs: typeof d.latency_ms === "number" ? d.latency_ms : null })
      if (wasOffline && online === true) resync(qc, 0) // bot restarted: re-fetch everything
      else if (online !== null && msg.type === "event") qc.invalidateQueries({ queryKey: ["status"] })
    }
    return conn.subscribe("bot", onBot)
  }, [conn, qc])

  const subscribe = useCallback((topic: LiveTopic, handler: LiveHandler) => conn.subscribe(topic, handler), [conn])
  const resyncTopic = useCallback((topic: LiveTopic) => conn.resync(topic), [conn])
  const tap = useCallback((listener: (msg: Envelope) => void) => conn.tap(listener), [conn])

  const value = useMemo<LiveContextValue>(
    () => ({ state, connected: state === "live", bot, subscribe, resync: resyncTopic, tap }),
    [state, bot, subscribe, resyncTopic, tap]
  )

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}
