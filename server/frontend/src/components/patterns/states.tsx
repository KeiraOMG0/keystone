import type { ReactNode } from "react"
import { Link } from "react-router-dom"
import { AlertCircle, type LucideIcon } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"

/** Empty state: icon tile, title, one sentence, optional single action. */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  className,
}: {
  icon?: LucideIcon
  title: string
  description?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn("flex flex-col items-center px-4 py-10 text-center", className)}>
      {Icon ? (
        <div className="mb-3 flex size-11 items-center justify-center rounded-xl bg-muted text-muted-foreground">
          <Icon className="size-5" aria-hidden />
        </div>
      ) : null}
      <p className="text-md font-semibold text-foreground">{title}</p>
      {description ? <p className="mt-1 max-w-[280px] text-sm text-muted-foreground">{description}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  )
}

/**
 * Error state: danger-soft tile, "Could not load X", the reason, Try again,
 * and a /status link for network / 502-type failures.
 */
export function ErrorState({
  title,
  message,
  onRetry,
  retrying,
  showStatusLink,
  className,
}: {
  title: string
  message?: ReactNode
  onRetry?: () => void
  retrying?: boolean
  showStatusLink?: boolean
  className?: string
}) {
  return (
    <div role="alert" className={cn("flex flex-col items-center px-4 py-10 text-center", className)}>
      <div className="mb-3 flex size-11 items-center justify-center rounded-xl bg-danger-soft text-danger">
        <AlertCircle className="size-5" aria-hidden />
      </div>
      <p className="text-md font-semibold text-foreground">{title}</p>
      {message ? <p className="mt-1 max-w-[340px] text-sm text-muted-foreground">{message}</p> : null}
      <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
        {onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry} loading={retrying}>
            {retrying ? "Retrying" : "Try again"}
          </Button>
        ) : null}
        {showStatusLink ? (
          <Button asChild variant="link" size="sm">
            <Link to="/status">Check service status</Link>
          </Button>
        ) : null}
      </div>
    </div>
  )
}

/** Skeleton compositions that mirror the final layouts. */
export function StatRowSkeleton({ count = 5 }: { count?: number }) {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-5" aria-busy="true">
      {Array.from({ length: count }, (_, i) => (
        <Card key={i} size="sm" className="gap-3 px-5">
          <Skeleton className="h-3.5 w-20" />
          <Skeleton className="h-8 w-16" />
        </Card>
      ))}
    </div>
  )
}

export function ListSkeleton({ rows = 5, avatar = true }: { rows?: number; avatar?: boolean }) {
  return (
    <div className="flex flex-col divide-y divide-border" aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-3 px-5 py-3">
          {avatar ? <Skeleton className="size-8 shrink-0 rounded-full" /> : null}
          <div className="flex flex-1 flex-col gap-1.5">
            <Skeleton className="h-3.5 w-2/5" />
            <Skeleton className="h-3 w-3/5" />
          </div>
          <Skeleton className="h-5 w-14 rounded-full" />
        </div>
      ))}
    </div>
  )
}

export function TableSkeleton({ rows = 5, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="flex flex-col divide-y divide-border" aria-busy="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-6 px-4 py-3.5">
          {Array.from({ length: cols }, (_, j) => (
            <Skeleton key={j} className={cn("h-3.5", j === 0 ? "w-16" : "flex-1")} />
          ))}
        </div>
      ))}
    </div>
  )
}

export function PageSkeleton() {
  return (
    <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-6" aria-busy="true" role="status" aria-label="Loading page">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-8 w-56" />
        <Skeleton className="h-4 w-80 max-w-full" />
      </div>
      <Card className="gap-0 py-0">
        <ListSkeleton rows={4} />
      </Card>
    </div>
  )
}
