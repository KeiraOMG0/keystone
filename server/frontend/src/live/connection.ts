import { backoffDelay } from "@/api/classify"
import type { Envelope, LiveHandler, LiveState, LiveTopic } from "@/live/protocol"

/**
 * ONE shared WebSocket per tab. Framework-free (LiveProvider owns the single
 * instance) so it can be reasoned about and tested on its own.
 *
 *  - reference-counted topic subscriptions (`subscribe(topic, handler)`): the
 *    server-side subscription exists exactly while >= 1 handler does, so e.g.
 *    the bot only produces `diagnostics` while a page is watching it;
 *  - reconnects FOREVER with capped exponential backoff + jitter;
 *  - heartbeat: pings every PING_MS; a silent socket (no frame for STALE_MS) is
 *    dropped and re-opened without waiting for TCP to time out;
 *  - `online` and `visibilitychange` trigger an immediate reconnect / liveness
 *    probe instead of waiting out the backoff;
 *  - subscriptions are replayed on every (re)connect after `hello`.
 * wss:// vs ws:// comes from location.protocol; same-origin, no path or proxy
 * assumptions (Cloudflare Tunnel proxies websockets transparently).
 */

const PING_MS = 25_000
const STALE_MS = 65_000
const PROBE_MS = 5_000
const STABLE_MS = 10_000
const OFFLINE_AFTER_MS = 15_000
const SOCKET_PATH = "/api/ws"

export function socketUrl(loc: Pick<Location, "protocol" | "host"> = window.location): string {
  return `${loc.protocol === "https:" ? "wss:" : "ws:"}//${loc.host}${SOCKET_PATH}`
}

export interface ConnectionEvents {
  onState?: (state: LiveState) => void
  /** Fired on every `hello` (first connect included). */
  onHello?: (info: { reconnect: boolean }) => void
  /** Socket closed with an application close code (4401 = session ended, 4403 = forbidden). */
  onClose?: (code: number) => void
}

export class LiveConnection {
  state: LiveState = "connecting"

  private ws: WebSocket | null = null
  private started = false
  private everLive = false
  private attempt = 0
  private liveSince = 0
  private lastRx = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private pingTimer: ReturnType<typeof setInterval> | null = null
  private offlineTimer: ReturnType<typeof setTimeout> | null = null
  private syncTimer: ReturnType<typeof setTimeout> | null = null
  private handlers = new Map<LiveTopic, Set<LiveHandler>>()
  /** Raw-frame listeners (owner-only Debug > Lab live inspector); see tap(). */
  private taps = new Set<(msg: Envelope) => void>()
  private sent = new Set<LiveTopic>()
  private events: ConnectionEvents

  constructor(events: ConnectionEvents = {}) {
    this.events = events
  }

  // ------------------------------------------------------------------ public

  start(): void {
    if (this.started) return
    this.started = true
    window.addEventListener("online", this.onOnline)
    window.addEventListener("offline", this.onOffline)
    document.addEventListener("visibilitychange", this.onVisibility)
    this.connect()
  }

  stop(): void {
    this.started = false
    window.removeEventListener("online", this.onOnline)
    window.removeEventListener("offline", this.onOffline)
    document.removeEventListener("visibilitychange", this.onVisibility)
    this.clearTimers()
    this.dropSocket(false)
    this.everLive = false
    this.attempt = 0
    this.setState("connecting")
  }

  /** Ref-counted subscription. Returns the unsubscribe function. */
  subscribe(topic: LiveTopic, handler: LiveHandler): () => void {
    let set = this.handlers.get(topic)
    if (!set) {
      set = new Set()
      this.handlers.set(topic, set)
    }
    set.add(handler)
    this.scheduleSync()
    return () => {
      const current = this.handlers.get(topic)
      if (!current) return
      current.delete(handler)
      if (current.size === 0) this.handlers.delete(topic)
      this.scheduleSync()
    }
  }

  /**
   * Observe EVERY parsed frame this socket receives (hello, pong, event,
   * snapshot, delta, error...) without subscribing to anything. Used only by
   * the owner-only Debug > Lab live inspector. Returns the remove function.
   */
  tap(listener: (msg: Envelope) => void): () => void {
    this.taps.add(listener)
    return () => {
      this.taps.delete(listener)
    }
  }

  /**
   * Ask the server to resend a full snapshot of a topic this tab is subscribed
   * to (the diagnostics delta stream uses it when a delta does not fit what it
   * has). No-op while the socket is down: the next subscribe sends one anyway.
   */
  resync(topic: LiveTopic): void {
    if (this.state !== "live" || !this.sent.has(topic)) return
    this.send({ type: "resync", topics: [topic] })
  }

  // ---------------------------------------------------------------- internals

  private setState(next: LiveState): void {
    if (this.state === next) return
    this.state = next
    this.events.onState?.(next)
  }

  private clearTimers(): void {
    for (const t of [this.reconnectTimer, this.offlineTimer, this.syncTimer]) if (t) clearTimeout(t)
    if (this.pingTimer) clearInterval(this.pingTimer)
    this.reconnectTimer = this.offlineTimer = this.syncTimer = null
    this.pingTimer = null
  }

  private connect(): void {
    if (!this.started || this.ws) return
    if (typeof navigator !== "undefined" && navigator.onLine === false) {
      this.setState("offline")
      return // the `online` event reconnects
    }
    let ws: WebSocket
    try {
      ws = new WebSocket(socketUrl())
    } catch {
      this.scheduleReconnect()
      return
    }
    this.ws = ws
    this.lastRx = Date.now()
    ws.onmessage = (ev) => this.onMessage(ws, ev)
    ws.onclose = (ev) => {
      if (this.ws !== ws) return
      this.ws = null
      this.afterClose(ev.code)
    }
    ws.onerror = () => {
      /* onclose follows; nothing to do */
    }
  }

  private onMessage(ws: WebSocket, ev: MessageEvent): void {
    if (this.ws !== ws || typeof ev.data !== "string") return
    this.lastRx = Date.now()
    // A frame proves the socket works even if a stray `offline` event flagged it.
    if (this.state !== "live" && this.liveSince) this.setState("live")
    let msg: Envelope
    try {
      msg = JSON.parse(ev.data) as Envelope
    } catch {
      return
    }
    for (const t of this.taps) {
      try {
        t(msg)
      } catch (err) {
        console.error("live tap failed", err)
      }
    }
    switch (msg.type) {
      case "hello": {
        const reconnect = this.everLive
        this.everLive = true
        this.liveSince = Date.now()
        if (this.offlineTimer) clearTimeout(this.offlineTimer)
        this.offlineTimer = null
        this.sent.clear()
        this.setState("live")
        this.startHeartbeat()
        this.syncNow()
        this.events.onHello?.({ reconnect })
        return
      }
      case "event":
      case "snapshot":
      case "delta": {
        if (!msg.topic) return
        const set = this.handlers.get(msg.topic)
        if (!set) return
        for (const h of [...set]) {
          try {
            h(msg)
          } catch (err) {
            console.error("live handler failed", err)
          }
        }
        return
      }
      default:
        return // pong / ack / error: liveness already recorded
    }
  }

  private afterClose(code: number): void {
    if (this.pingTimer) clearInterval(this.pingTimer)
    this.pingTimer = null
    this.sent.clear()
    if (this.liveSince && Date.now() - this.liveSince >= STABLE_MS) this.attempt = 0
    this.liveSince = 0
    if (!this.started) return
    if (code >= 4000) this.events.onClose?.(code)
    this.enterDownState()
    this.scheduleReconnect()
  }

  private enterDownState(): void {
    if (typeof navigator !== "undefined" && navigator.onLine === false) {
      this.setState("offline")
      return
    }
    if (this.state === "offline") return // stay "offline" (polling) until a hello arrives; retries continue
    this.setState(this.everLive ? "reconnecting" : "connecting")
    if (!this.offlineTimer) {
      this.offlineTimer = setTimeout(() => {
        this.offlineTimer = null
        if (this.state !== "live") this.setState("offline") // polling fallback engages; retries continue
      }, OFFLINE_AFTER_MS)
    }
  }

  private scheduleReconnect(): void {
    if (!this.started || this.reconnectTimer) return
    const delay = backoffDelay(this.attempt)
    this.attempt += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private reconnectNow(): void {
    if (!this.started) return
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    this.attempt = 0
    if (!this.ws) this.connect()
  }

  private dropSocket(notify: boolean): void {
    const ws = this.ws
    if (!ws) return
    this.ws = null
    ws.onmessage = ws.onclose = ws.onerror = null
    try {
      ws.close(1000)
    } catch {
      /* already closed */
    }
    if (notify) this.afterClose(0)
  }

  // --------------------------------------------------------------- heartbeat

  private send(obj: unknown): boolean {
    const ws = this.ws
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    try {
      ws.send(JSON.stringify(obj))
      return true
    } catch {
      return false
    }
  }

  private startHeartbeat(): void {
    if (this.pingTimer) clearInterval(this.pingTimer)
    this.pingTimer = setInterval(() => {
      if (Date.now() - this.lastRx > STALE_MS) {
        this.dropSocket(true) // silent socket: do not wait for TCP to notice
        return
      }
      this.send({ type: "ping" })
    }, PING_MS)
  }

  /** Is the socket really alive? Ping and drop it if nothing comes back. */
  private probe(): void {
    if (!this.ws) return
    const at = Date.now()
    this.send({ type: "ping" })
    setTimeout(() => {
      if (this.ws && this.lastRx < at) this.dropSocket(true)
    }, PROBE_MS)
  }

  // ------------------------------------------------------------ subscriptions

  private scheduleSync(): void {
    if (this.syncTimer) return
    this.syncTimer = setTimeout(() => {
      this.syncTimer = null
      this.syncNow()
    }, 0) // coalesce a burst of mounts into one message
  }

  private syncNow(): void {
    if (this.state !== "live") return // replayed on hello
    const want = new Set(this.handlers.keys())
    const add = [...want].filter((t) => !this.sent.has(t))
    const drop = [...this.sent].filter((t) => !want.has(t))
    if (add.length && this.send({ type: "subscribe", topics: add })) add.forEach((t) => this.sent.add(t))
    if (drop.length && this.send({ type: "unsubscribe", topics: drop })) drop.forEach((t) => this.sent.delete(t))
  }

  // ------------------------------------------------------- browser triggers

  private onOnline = (): void => {
    this.reconnectNow()
    this.probe()
  }

  private onOffline = (): void => {
    this.setState("offline")
  }

  private onVisibility = (): void => {
    if (document.visibilityState !== "visible") return
    if (!this.ws) this.reconnectNow()
    else this.probe()
  }
}
