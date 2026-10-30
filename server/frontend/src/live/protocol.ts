/** Wire protocol for WS /api/ws (server/live/endpoint.py). See docs/ARCHITECTURE.md. */

export const LIVE_TOPICS = ["bot", "stats", "diagnostics", "honeypot", "tickets", "moderation", "audit", "roles", "birthdays", "automation", "music"] as const
export type LiveTopic = (typeof LIVE_TOPICS)[number]

/**
 * `delta` (diagnostics only): `data` is {set: [[path, value], ...], unset: [path, ...]}
 * relative to the previous diagnostics message THIS socket received; see
 * applyDelta in src/api/diagnostics.ts.
 */
export type EnvelopeType = "event" | "snapshot" | "delta" | "pong" | "error" | "hello" | "ack"

export interface Envelope<T = Record<string, unknown>> {
  v: number
  type: EnvelopeType
  topic: LiveTopic | null
  /** ms since epoch, server clock */
  ts: number
  /** per-topic sequence (dashboard process wide) */
  seq: number
  data: T
  /** diagnostics only: this socket's stream revision after applying the message */
  rev?: number
  /** diagnostics `delta` only: the revision the delta applies on top of */
  base_rev?: number
}

/**
 * Connection state shown in the UI:
 *   connecting   first connect in flight
 *   live         hello received; events flowing
 *   reconnecting the socket dropped recently (< OFFLINE_AFTER_MS); retrying
 *   offline      the browser is offline OR the socket has been down for a while;
 *                queries fall back to polling (retrying continues forever)
 */
export type LiveState = "connecting" | "live" | "reconnecting" | "offline"

export type LiveHandler = (msg: Envelope) => void

export interface BotState {
  online: boolean | null
  latencyMs: number | null
}
