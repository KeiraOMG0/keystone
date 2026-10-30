import type { ReactNode } from "react"
import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"

/**
 * Stat tile: label (with optional status dot), big tabular value, optional
 * delta/caption. No coloured backgrounds or side rails; presence stats use
 * `dotClassName` and always print the label.
 */
export function StatCard({
  label,
  value,
  dotClassName,
  delta,
  className,
}: {
  label: ReactNode
  value: ReactNode
  dotClassName?: string
  delta?: ReactNode
  className?: string
}) {
  return (
    <Card size="sm" className={cn("gap-1 px-5", className)}>
      <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
        {dotClassName ? <span aria-hidden className={cn("size-2 shrink-0 rounded-full", dotClassName)} /> : null}
        {label}
      </div>
      <div className="flex items-baseline gap-2">
        <span className="text-3xl font-semibold tracking-tight text-foreground tabular-nums">{value}</span>
        {delta}
      </div>
    </Card>
  )
}
