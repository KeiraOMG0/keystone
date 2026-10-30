import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/** Popover-styled tooltip for Recharts (`content={<ChartTooltip unit="catches" />}`). */
export function ChartTooltip({
  active,
  payload,
  label,
  unit,
}: {
  active?: boolean
  payload?: { value?: number | string }[]
  label?: string | number
  unit: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs text-popover-foreground shadow-md">
      <p className="text-muted-foreground">{label}</p>
      <p className="font-medium tabular-nums">
        {payload[0].value} {unit}
      </p>
    </div>
  )
}

/** Titled chart wrapper: title (13/600), optional right-side legend/value, chart, caption. */
export function ChartCard({
  title,
  aside,
  caption,
  children,
  className,
}: {
  title: ReactNode
  aside?: ReactNode
  caption?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={cn("rounded-lg border border-border bg-card p-4", className)}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        {aside}
      </div>
      {children}
      {caption ? <p className="mt-2 text-xs text-muted-foreground">{caption}</p> : null}
    </section>
  )
}
