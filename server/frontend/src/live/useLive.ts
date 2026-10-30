import { createContext, useContext, useEffect, useRef } from "react"
import type { BotState, Envelope, LiveHandler, LiveState, LiveTopic } from "@/live/protocol"

export interface LiveContextValue {
  state: LiveState
  /** True only while the socket is up and `hello` was received. */
  connected: boolean
  /** From the `bot` topic: is the bot process reachable from the dashboard backend? */
  bot: BotState
  subscribe: (topic: LiveTopic, handler: LiveHandler) => () => void
  /** Ask the server for a fresh full snapshot of a subscribed topic (no-op while down). */
  resync: (topic: LiveTopic) => void
  /** Observe every raw frame (owner-only Debug > Lab live inspector). Returns the remove function. */
  tap: (listener: (msg: Envelope) => void) => () => void
}

/**
 * Default value = "no live layer" (public pages, or outside LiveProvider):
 * `connected` is false, so every hook falls back to its existing polling.
 */
export const LiveContext = createContext<LiveContextValue>({
  state: "offline",
  connected: false,
  bot: { online: null, latencyMs: null },
  subscribe: () => () => {},
  resync: () => {},
  tap: () => () => {},
})

export function useLive(): LiveContextValue {
  return useContext(LiveContext)
}

/** True while live updates are flowing (polling hooks turn their interval off). */
export function useLiveConnected(): boolean {
  return useContext(LiveContext).connected
}

/**
 * Subscribe a component to a live topic. THE CONVENTION for new pages:
 *
 *   useLiveTopic("tickets", (msg) => queryClient.invalidateQueries({ queryKey: ["tickets"] }))
 *
 * plus `refetchInterval: connected ? false : <old polling interval>` on the
 * query (keep polling ONLY while the socket is down). Subscriptions are
 * reference-counted across the whole tab; the handler is always the latest
 * closure (no need to memoise it). `enabled: false` unsubscribes (e.g. a paused
 * Diagnostics page stops the server-side collector). Handlers must be
 * idempotent: after a reconnect the queries are re-fetched from scratch.
 */
export function useLiveTopic(topic: LiveTopic, handler: LiveHandler, opts?: { enabled?: boolean }): void {
  const { subscribe } = useContext(LiveContext)
  const ref = useRef(handler)
  useEffect(() => {
    ref.current = handler
  })
  const enabled = opts?.enabled ?? true
  useEffect(() => {
    if (!enabled) return
    return subscribe(topic, (msg) => ref.current(msg))
  }, [subscribe, topic, enabled])
}
