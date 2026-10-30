import { Link } from "react-router-dom"
import { Bot } from "lucide-react"
import { useLive } from "@/live/useLive"
import { useStatus } from "@/api/status"
import { cn } from "@/lib/utils"

export type LiveView = {
  key: "connecting" | "live" | "bot-offline" | "reconnecting" | "offline"
  label: string
  dot: string
  pulse: boolean
}

/**
 * Maps the ONE live-layer state (useLive) to the sidebar footer row (the
 * header "Live" pill was removed; LiveBanners still announce outages).
 * Keeps the Live / Reconnecting / Offline-polling / Bot-offline states the
 * old LiveIndicator had; a label is always present (never colour alone).
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useLiveView(): LiveView {
  const { state, bot } = useLive()
  if (state === "live") {
    if (bot.online === false)
      return { key: "bot-offline", label: "Bot offline", dot: "bg-danger", pulse: false }
    return { key: "live", label: "Live", dot: "bg-success", pulse: true }
  }
  if (state === "reconnecting")
    return { key: "reconnecting", label: "Reconnecting", dot: "bg-warning", pulse: true }
  if (state === "offline")
    return { key: "offline", label: "Offline, polling", dot: "bg-danger", pulse: false }
  return { key: "connecting", label: "Connecting", dot: "bg-subtle-foreground", pulse: false }
}

/** Sidebar footer: Live row (dot + label + ping) and Bot row (+ Status link). */
export function SidebarLiveRows({ collapsed }: { collapsed?: boolean }) {
  const v = useLiveView()
  const { bot } = useLive()
  const { data, isError } = useStatus()
  const botOffline = bot.online === false || (data ? data.bot.status === "down" : false)
  const serviceIssue = isError || (data ? data.dashboard.status === "down" || data.internal_api.status === "down" : false)
  const ping = bot.latencyMs
  if (collapsed) {
    return (
      <div className="flex flex-col items-center gap-2 py-1" aria-label={`${v.label}. ${botOffline ? "Bot offline" : "Bot online"}`}>
        <span aria-hidden className={cn("size-2 rounded-full", v.dot, v.pulse && "kb-live-dot")} />
        <Bot className={cn("size-4", botOffline ? "text-danger" : "text-muted-foreground")} aria-hidden />
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-0.5 text-xs">
      <div className="flex items-center gap-2 px-2.5 py-1 text-muted-foreground">
        <span aria-hidden className={cn("size-2 shrink-0 rounded-full", v.dot, v.pulse && "kb-live-dot")} />
        <span>{v.label}</span>
        {v.key === "live" && ping != null ? <span className="ml-auto tabular-nums text-subtle-foreground">{Math.round(ping)} ms</span> : null}
      </div>
      <div className="flex items-center gap-2 px-2.5 py-1 text-muted-foreground">
        <Bot className={cn("size-3.5 shrink-0", botOffline ? "text-danger" : "text-muted-foreground")} aria-hidden />
        <span>{botOffline ? "Bot offline" : serviceIssue ? "Service issue" : "Bot online"}</span>
        <Link
          to="/status"
          className="ml-auto rounded-sm text-primary underline-offset-4 hover:underline"
        >
          Status
        </Link>
      </div>
    </div>
  )
}

/**
 * THE shared in-page live caption ("Updates live" beside lists that follow a
 * live topic). It reflects the socket: a pulsing green dot only while
 * `connected`; otherwise a static warning dot and `fallbackText` (say how the
 * page refreshes meanwhile, e.g. "Live updates paused. Refreshing every 30 s.").
 */
export function LiveCaption({
  liveText = "Updates live",
  fallbackText = "Live updates paused. Refreshing periodically.",
  className,
}: {
  liveText?: string
  fallbackText?: string
  className?: string
}) {
  const { connected, state } = useLive()
  const text = connected ? liveText : state === "connecting" ? "Connecting to live updates" : fallbackText
  return (
    <span className={cn("flex items-center gap-1.5 text-xs text-subtle-foreground", className)} role="status">
      <span
        aria-hidden
        className={cn("size-1.5 shrink-0 rounded-full", connected ? "kb-live-dot bg-success" : state === "connecting" ? "bg-subtle-foreground" : "bg-warning")}
      />
      {text}
    </span>
  )
}
