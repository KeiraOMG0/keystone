import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/**
 * Discord-style message frame used by the template editor and the ticket panel
 * builder previews: bot avatar, name with an APP badge, optional text, then the
 * embed(s) and/or a component row (buttons). Themed through tokens (the embed
 * itself uses the --embed-* tokens); purely presentational.
 */
export function DiscordMessage({
  text,
  children,
  botName = "Keystone",
  className,
}: {
  text?: ReactNode
  children?: ReactNode
  botName?: string
  className?: string
}) {
  return (
    <div className={cn("flex gap-3 rounded-lg border border-border bg-card p-4", className)}>
      <div
        aria-hidden
        className="flex size-10 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground"
      >
        K
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex items-center gap-2">
          <span className="text-base font-semibold text-foreground">{botName}</span>
          <span className="rounded-[3px] bg-[#5865f2] px-1 py-px text-2xs font-semibold text-white">APP</span>
        </div>
        {text ? <p className="text-base break-words whitespace-pre-wrap text-foreground">{text}</p> : null}
        {children}
      </div>
    </div>
  )
}

/** A Discord-style (blurple) button as rendered under a panel message. */
export function DiscordButton({ label, emoji }: { label: string; emoji?: string | null }) {
  return (
    <span className="mt-1 inline-flex h-8 w-fit items-center gap-1.5 rounded-[3px] bg-[#5865f2] px-4 text-sm font-medium text-white">
      {emoji ? <span>{emoji}</span> : null}
      {label}
    </span>
  )
}
