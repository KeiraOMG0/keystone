import type { ReactNode } from "react"
import { ApiError } from "@/api/client"
import { Card } from "@/components/ui/card"
import { ErrorState, TableSkeleton } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { describeApiError } from "@/lib/apiError"
import { formatDateTime } from "@/lib/format"
import type { ForbiddenError } from "@/types/api"

/** The inline-403 body of a tab, or null. */
export function forbiddenOf(error: unknown): ForbiddenError | { error: "not_a_member" | "staff_only" } | null {
  if (!(error instanceof ApiError) || error.status !== 403) return null
  const body = (error.body ?? {}) as { error?: string; detail?: { error?: string } }
  const inner = (body.detail ?? body) as { error?: string }
  if (inner.error === "not_a_member") return { error: "not_a_member" }
  if (inner.error === "forbidden" && "required_permission" in inner) return inner as ForbiddenError
  return { error: "staff_only" }
}

/**
 * Loading / inline-403 / error wrapper shared by every tab. `permission` is
 * what the tab needs (shown in the 403 when the server body has none).
 */
export function TabState({
  query,
  permission,
  what,
  children,
}: {
  query: { isPending: boolean; isError: boolean; error: unknown; refetch: () => unknown; isRefetching: boolean; data: unknown }
  permission: string
  what: string
  children: ReactNode
}) {
  const forbidden = forbiddenOf(query.error)
  if (forbidden && query.data === undefined) {
    return forbidden.error === "not_a_member" ? (
      <Forbidden403 reason="not_a_member" />
    ) : (
      <Forbidden403 reason="forbidden" requiredPermission={"required_permission" in forbidden ? forbidden.required_permission : permission} />
    )
  }
  if (query.isPending) {
    return (
      <Card className="gap-0 py-0">
        <TableSkeleton rows={4} cols={4} />
      </Card>
    )
  }
  if (query.isError && query.data === undefined) {
    return (
      <Card>
        <ErrorState
          title={`Could not load ${what}`}
          message={describeApiError(query.error)}
          onRetry={() => void query.refetch()}
          retrying={query.isRefetching}
          showStatusLink
        />
      </Card>
    )
  }
  return <>{children}</>
}

/** "#channel" or a fallback for a channel the bot can't see any more. */
export function channelLabel(name: string | null | undefined, id: string | null | undefined): string {
  if (name) return `#${name}`
  return id ? `#deleted-channel (${id})` : "No channel"
}

/** UTC ISO -> "Mon 3 Nov, 09:00 UTC" + local time in the title attribute. */
export function UtcTime({ iso }: { iso: string }) {
  const d = new Date(iso)
  const utc = d.toLocaleString([], { timeZone: "UTC", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
  return (
    <time dateTime={iso} title={`Your time: ${formatDateTime(iso)}`} className="tabular-nums">
      {utc} UTC
    </time>
  )
}

export function formatDelay(seconds: number): string {
  if (!seconds) return "Immediately"
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`
  if (seconds < 86400) return `${+(seconds / 3600).toFixed(1)} h`
  return `${+(seconds / 86400).toFixed(1)} d`
}

/** A tab the viewer's permissions (GET /api/me) don't cover: no request is sent. */
export function NotAllowed({ permission }: { permission: string }) {
  return <Forbidden403 reason="forbidden" requiredPermission={permission} />
}
