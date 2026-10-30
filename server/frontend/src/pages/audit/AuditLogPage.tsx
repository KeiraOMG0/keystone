import { useEffect, useMemo, useRef, useState } from "react"
import { useSearchParams } from "react-router-dom"
import { ScrollText, X } from "lucide-react"
import { ApiError } from "@/api/client"
import { useAuditActions, useAuditLive, useAuditLog, type AuditFilters } from "@/api/audit"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { EmptyState, ErrorState, ListSkeleton } from "@/components/patterns/states"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Field } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { useAuth } from "@/hooks/useAuth"
import { describeApiError } from "@/lib/apiError"
import { useLiveConnected } from "@/live/useLive"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import type { ForbiddenError } from "@/types/api"
import { AUDIT_GRID, AuditEntryRow } from "./AuditEntryRow"
import { actionLabel, dayEndIso, dayStartIso, idFilterError } from "./format"

/** URL search params hold the filters, so a filtered view can be linked/bookmarked. */
const P = { action: "action", actor: "actor", target: "target", from: "from", to: "to" } as const

/** Debounced id input: applies to the URL once valid (or empty) and typing paused. */
function IdFilter({
  label,
  value,
  onApply,
  placeholder,
}: {
  label: string
  value: string
  onApply: (v: string) => void
  placeholder: string
}) {
  const [draft, setDraft] = useState(value)
  // Follow outside changes (row "This actor" button, Clear filters).
  useEffect(() => setDraft(value), [value])
  const error = idFilterError(draft)
  const apply = useRef(onApply)
  useEffect(() => {
    apply.current = onApply
  })
  useEffect(() => {
    const v = draft.trim()
    if (error || v === value) return
    const t = setTimeout(() => apply.current(v), 400)
    return () => clearTimeout(t)
  }, [draft, error, value])
  return (
    <Field label={label} error={error}>
      {(p) => (
        <Input
          {...p}
          inputMode="numeric"
          autoComplete="off"
          placeholder={placeholder}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
      )}
    </Field>
  )
}

export default function AuditLogPage() {
  const { permissions } = useAuth()
  const canResolve = permissions.staff_base === true
  const connected = useLiveConnected()
  const [params, setParams] = useSearchParams()

  const action = params.get(P.action) ?? ""
  const actor = params.get(P.actor) ?? ""
  const target = params.get(P.target) ?? ""
  const from = params.get(P.from) ?? ""
  const to = params.get(P.to) ?? ""
  const rangeError = from && to && from > to ? "The start date is after the end date." : null

  const filters: AuditFilters = useMemo(
    () => ({
      action: action || undefined,
      actorId: actor && !idFilterError(actor) ? actor : undefined,
      targetId: target && !idFilterError(target) ? target : undefined,
      since: !rangeError && from ? dayStartIso(from) : undefined,
      until: !rangeError && to ? dayEndIso(to) : undefined,
    }),
    [action, actor, target, from, to, rangeError]
  )
  const hasFilters = Boolean(action || actor || target || from || to)

  const log = useAuditLog(filters)
  const actions = useAuditActions(log.data !== undefined)
  // Live rows are only merged once the list has data (a failed background
  // refetch keeps `data`, so the subscription survives it).
  const { freshIds } = useAuditLive(filters, log.data !== undefined)
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set())

  function setParam(patch: Record<string, string>) {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        for (const [k, v] of Object.entries(patch)) {
          if (v) next.set(k, v)
          else next.delete(k)
        }
        return next
      },
      { replace: true }
    )
  }

  function toggle(id: number) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  if (log.isError && log.error instanceof ApiError && log.error.status === 403) {
    const body = log.error.body as ForbiddenError | null
    return <Forbidden403 reason="forbidden" requiredPermission={body?.required_permission ?? "view_audit"} />
  }

  const entries = log.data?.pages.flatMap((p) => p.entries) ?? []
  const newest = entries.find((e) => freshIds.has(e.id))
  const actionOptions = actions.data?.actions ?? []
  const errStatus = log.error instanceof ApiError ? log.error.status : null

  return (
    <PageContainer>
      <PageHeader
        title="Audit log"
        description="Everything the bot recorded: moderation, role and channel changes, permission grants and more."
      />

      <Card className="gap-0 py-0">
        <div className="grid gap-3 border-b border-border p-4 sm:grid-cols-2 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_minmax(0,1fr)_minmax(0,0.8fr)_minmax(0,0.8fr)_auto] lg:items-start">
          <Field label="Action">
            {(p) => (
              <Select {...p} value={action} onChange={(e) => setParam({ [P.action]: e.target.value })}>
                <option value="">All actions</option>
                {action && !actionOptions.some((a) => a.action === action) ? (
                  <option value={action}>{actionLabel(action)}</option>
                ) : null}
                {actionOptions.map((a) => (
                  <option key={a.action} value={a.action}>
                    {actionLabel(a.action)} ({a.count})
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <IdFilter label="Actor ID" value={actor} onApply={(v) => setParam({ [P.actor]: v })} placeholder="User ID" />
          <IdFilter label="Target ID" value={target} onApply={(v) => setParam({ [P.target]: v })} placeholder="User, role or channel ID" />
          <Field label="From" error={rangeError}>
            {(p) => (
              <Input {...p} type="date" value={from} max={to || undefined} onChange={(e) => setParam({ [P.from]: e.target.value })} />
            )}
          </Field>
          <Field label="To">
            {(p) => (
              <Input {...p} type="date" value={to} min={from || undefined} onChange={(e) => setParam({ [P.to]: e.target.value })} />
            )}
          </Field>
          <div className="flex items-end sm:col-span-2 lg:col-span-1 lg:h-full">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={!hasFilters}
              onClick={() => setParams(new URLSearchParams(), { replace: true })}
              className="max-md:h-11"
            >
              <X aria-hidden /> Clear filters
            </Button>
          </div>
        </div>

        {connected && log.data !== undefined ? (
          <p className="flex items-center gap-2 border-b border-border px-4 py-2 text-xs text-muted-foreground">
            <span aria-hidden className="kb-live-dot size-1.5 rounded-full bg-success" />
            New entries that match these filters appear here live.
          </p>
        ) : null}
        <div aria-live="polite" className="sr-only">
          {newest ? `New audit entry: ${actionLabel(newest.action)}` : ""}
        </div>

        <div
          className={`hidden border-b border-border px-4 py-2 text-xs font-semibold text-muted-foreground ${AUDIT_GRID}`}
          aria-hidden
        >
          <span>Time</span>
          <span>Action</span>
          <span>Actor</span>
          <span>Target</span>
          <span />
        </div>

        {log.isPending ? (
          <ListSkeleton rows={8} avatar={false} />
        ) : log.isError && log.data === undefined ? (
          <ErrorState
            title="Could not load the audit log"
            message={describeApiError(log.error)}
            onRetry={() => void log.refetch()}
            retrying={log.isRefetching}
            showStatusLink={errStatus === 0 || errStatus === 502 || errStatus === 503}
          />
        ) : entries.length === 0 ? (
          hasFilters ? (
            <EmptyState
              icon={ScrollText}
              title="No entries match these filters"
              description="Try a wider date range or another action."
              action={
                <Button variant="secondary" size="sm" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
                  Clear filters
                </Button>
              }
            />
          ) : (
            <EmptyState icon={ScrollText} title="No audit entries yet" description="Bot actions show up here live as they happen." />
          )
        ) : (
          <>
            <ul aria-label="Audit log entries">
              {entries.map((e) => (
                <AuditEntryRow
                  key={e.id}
                  entry={e}
                  expanded={expanded.has(e.id)}
                  onToggle={() => toggle(e.id)}
                  isNew={freshIds.has(e.id)}
                  canResolve={canResolve}
                  onFilter={(patch) =>
                    setParam({
                      ...(patch.action !== undefined ? { [P.action]: patch.action } : {}),
                      ...(patch.actorId !== undefined ? { [P.actor]: patch.actorId } : {}),
                      ...(patch.targetId !== undefined ? { [P.target]: patch.targetId } : {}),
                    })
                  }
                />
              ))}
            </ul>
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-3 text-sm text-muted-foreground">
              <span>
                {entries.length} {entries.length === 1 ? "entry" : "entries"} shown
                {log.hasNextPage ? "" : " (all)"}
              </span>
              {log.isFetchNextPageError ? (
                <span className="text-danger">{describeApiError(log.error, "Could not load more.")}</span>
              ) : null}
              {log.hasNextPage ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => void log.fetchNextPage()}
                  loading={log.isFetchingNextPage}
                  className="max-md:h-11"
                >
                  {log.isFetchingNextPage ? "Loading" : "Load more"}
                </Button>
              ) : null}
            </div>
          </>
        )}
      </Card>
    </PageContainer>
  )
}
