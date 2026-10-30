import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/**
 * THE page header for every /dashboard page: title, one-line description,
 * primary action(s) on the right (wraps under the title on phones). Only one
 * `primary` Button belongs in `actions`. `badge` sits next to the title
 * (e.g. the "Staff only" badge, a status badge).
 */
export function PageHeader({
  title,
  description,
  actions,
  badge,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
  badge?: ReactNode
  className?: string
}) {
  return (
    <header className={cn("flex flex-wrap items-start justify-between gap-x-4 gap-y-3", className)}>
      <div className="min-w-0 flex-1 basis-64">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">{title}</h1>
          {badge}
        </div>
        {description ? <p className="mt-1 max-w-prose text-base text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  )
}

/** Standard page column: consistent width + vertical rhythm. */
export function PageContainer({
  children,
  width = "default",
  className,
}: {
  children: ReactNode
  /** default 1200, narrow 760, form 1000, wide 1320 */
  width?: "default" | "narrow" | "form" | "wide"
  className?: string
}) {
  return (
    <div
      className={cn(
        "mx-auto flex w-full flex-col gap-6",
        width === "default" && "max-w-[1200px]",
        width === "narrow" && "max-w-[760px]",
        width === "form" && "max-w-[1000px]",
        width === "wide" && "max-w-[1320px]",
        className
      )}
    >
      {children}
    </div>
  )
}
