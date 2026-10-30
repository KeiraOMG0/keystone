import { ChevronDown, Filter } from "lucide-react"
import type { AuditEntry } from "@/api/audit"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { CopyButton } from "@/components/ui/copy-button"
import { Tip } from "@/components/ui/tooltip"
import { ResolvedChannel } from "@/components/ResolvedChannel"
import { ResolvedRole } from "@/components/ResolvedRole"
import { ResolvedUser } from "@/components/ResolvedUser"
import { formatDateTime, formatRelativeTime } from "@/lib/format"
import { cn } from "@/lib/utils"
import {
  actionLabel,
  actionVariant,
  asFlatDetail,
  detailKeyLabel,
  detailPreview,
  idKindForKey,
  isSnowflake,
  prettyJson,
  type Scalar,
} from "./format"

/** Desktop grid columns; the header row in AuditLogPage uses the same template. */
export const AUDIT_GRID = "md:grid md:grid-cols-[140px_minmax(150px,200px)_minmax(0,1fr)_minmax(0,1fr)_36px] md:items-center md:gap-4"

/**
 * Names: viewers with staff_base get <ResolvedUser>/<ResolvedRole>/... (the
 * /api/resolve endpoint is staff_base-only); everyone else sees the name the
 * audit endpoint attached from the bot's cache, else the raw id.
 */
function UserRef({ id, name, canResolve }: { id: string | null; name?: string | null; canResolve: boolean }) {
  if (!id || id === "0") return <span className="text-muted-foreground">Unknown</span>
  if (canResolve) return <ResolvedUser userId={id} />
  return name ? (
    <span title={id}>{name}</span>
  ) : (
    <code className="font-mono text-xs text-muted-foreground">{id}</code>
  )
}

function TargetRef({ entry, canResolve }: { entry: AuditEntry; canResolve: boolean }) {
  const { target_type: type, target_id: id, target_name: name } = entry
  if (!id) return type ? <span className="text-muted-foreground">{actionLabel(type)}</span> : <span className="text-subtle-foreground">None</span>
  if (type === "member" || type === "user") return <UserRef id={id} name={name} canResolve={canResolve} />
  if (canResolve && type === "role") return <ResolvedRole roleId={id} />
  if (canResolve && type === "channel") return <ResolvedChannel channelId={id} />
  if (name) {
    return (
      <span title={id}>
        {type === "channel" ? "#" : ""}
        {name}
      </span>
    )
  }
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      {type ? <span className="text-muted-foreground">{actionLabel(type)}</span> : null}
      <code className="truncate font-mono text-xs text-muted-foreground">{id}</code>
    </span>
  )
}

function DetailValue({ k, v, canResolve }: { k: string; v: Scalar | Scalar[]; canResolve: boolean }) {
  if (Array.isArray(v)) {
    return v.length === 0 ? (
      <span className="text-subtle-foreground">none</span>
    ) : (
      <span className="flex flex-wrap gap-1">
        {v.map((x, i) => (
          <code key={i} className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">
            {String(x)}
          </code>
        ))}
      </span>
    )
  }
  if (v === null) return <span className="text-subtle-foreground">none</span>
  if (typeof v === "boolean") return <span>{v ? "Yes" : "No"}</span>
  const kind = idKindForKey(k)
  if (kind && isSnowflake(v)) {
    if (!canResolve) return <code className="font-mono text-xs">{v}</code>
    if (kind === "user") return <ResolvedUser userId={v} />
    if (kind === "role") return <ResolvedRole roleId={v} />
    return <ResolvedChannel channelId={v} />
  }
  return <span className="break-words whitespace-pre-wrap">{String(v)}</span>
}

function DetailBody({ entry, canResolve }: { entry: AuditEntry; canResolve: boolean }) {
  if (entry.detail_raw) {
    return (
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">The stored detail is not valid JSON; shown as text.</p>
        <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap">{entry.detail_raw}</pre>
      </div>
    )
  }
  if (entry.detail === null || entry.detail === undefined) {
    return <p className="text-sm text-muted-foreground">No extra detail recorded.</p>
  }
  const flat = asFlatDetail(entry.detail)
  if (flat) {
    const keys = Object.keys(flat)
    const hasDiff = "before" in flat && "after" in flat
    return (
      <dl className="grid grid-cols-[minmax(90px,max-content)_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
        {hasDiff ? (
          <>
            <dt className="text-muted-foreground">Change</dt>
            <dd className="flex flex-wrap items-center gap-1.5">
              <DetailValue k="before" v={flat.before} canResolve={canResolve} />
              <span aria-hidden className="text-muted-foreground">→</span>
              <span className="sr-only">changed to</span>
              <DetailValue k="after" v={flat.after} canResolve={canResolve} />
            </dd>
          </>
        ) : null}
        {keys
          .filter((k) => !(hasDiff && (k === "before" || k === "after")))
          .map((k) => (
            <div key={k} className="contents">
              <dt className="text-muted-foreground">{detailKeyLabel(k)}</dt>
              <dd className="min-w-0">
                <DetailValue k={k} v={flat[k]} canResolve={canResolve} />
              </dd>
            </div>
          ))}
      </dl>
    )
  }
  return (
    <pre className="max-h-80 overflow-auto rounded-md bg-muted p-3 font-mono text-xs">{prettyJson(entry.detail)}</pre>
  )
}

export function AuditEntryRow({
  entry,
  expanded,
  onToggle,
  isNew,
  canResolve,
  onFilter,
}: {
  entry: AuditEntry
  expanded: boolean
  onToggle: () => void
  isNew: boolean
  canResolve: boolean
  onFilter: (patch: { action?: string; actorId?: string; targetId?: string }) => void
}) {
  const preview = detailPreview(entry.detail)
  const panelId = `audit-entry-${entry.id}`
  const when = entry.created_at

  return (
    <li className={cn("border-b border-border last:border-b-0", isNew && "bg-primary-soft/40 transition-colors duration-1000")}>
      <div
        className={cn("relative flex cursor-pointer flex-col gap-1.5 px-4 py-3 hover:bg-hover max-md:pr-14 md:py-2.5", AUDIT_GRID)}
        onClick={(e) => {
          // Clicks on links/buttons inside the row (copy, dev-mode chips) don't toggle.
          if ((e.target as HTMLElement).closest("button, a")) return
          onToggle()
        }}
      >
        <div className="flex items-center justify-between gap-2 md:block">
          {when ? (
            <Tip label={formatDateTime(when)}>
              <time dateTime={when} className="text-sm text-muted-foreground tabular-nums">
                {formatRelativeTime(when)}
              </time>
            </Tip>
          ) : (
            <span className="text-sm text-muted-foreground">Unknown time</span>
          )}
          <span className="md:hidden">
            <Badge variant={actionVariant(entry.action)}>{actionLabel(entry.action)}</Badge>
          </span>
        </div>
        <div className="hidden min-w-0 md:block">
          <Badge variant={actionVariant(entry.action)} className="max-w-full truncate">
            {actionLabel(entry.action)}
          </Badge>
        </div>
        <div className="flex min-w-0 items-center gap-1.5 text-sm">
          <span className="text-xs text-muted-foreground md:hidden">By</span>
          <UserRef id={entry.actor_id} name={entry.actor_name} canResolve={canResolve} />
        </div>
        <div className="flex min-w-0 flex-col text-sm">
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="text-xs text-muted-foreground md:hidden">Target</span>
            <TargetRef entry={entry} canResolve={canResolve} />
          </span>
          {preview ? <span className="truncate text-xs text-muted-foreground">{preview}</span> : null}
        </div>
        <div className="flex justify-end max-md:absolute max-md:right-2 max-md:bottom-2">
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-expanded={expanded}
            aria-controls={panelId}
            aria-label={expanded ? "Hide details" : "Show details"}
            onClick={onToggle}
          >
            <ChevronDown className={cn("size-4 transition-transform", expanded && "rotate-180")} aria-hidden />
          </Button>
        </div>
      </div>
      {expanded ? (
        <div id={panelId} className="flex flex-col gap-4 border-t border-border bg-muted/40 px-4 py-4">
          <DetailBody entry={entry} canResolve={canResolve} />
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span>Entry #{entry.id}</span>
            {when ? <span>{formatDateTime(when)}</span> : null}
            <span className="inline-flex items-center gap-1">
              Actor ID <code className="font-mono">{entry.actor_id ?? "none"}</code>
              {entry.actor_id && entry.actor_id !== "0" ? <CopyButton value={entry.actor_id} /> : null}
            </span>
            {entry.target_id ? (
              <span className="inline-flex items-center gap-1">
                Target ID <code className="font-mono">{entry.target_id}</code>
                <CopyButton value={entry.target_id} />
              </span>
            ) : null}
            <CopyButton value={prettyJson(entry)} label="Copy entry as JSON" />
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="secondary" size="sm" onClick={() => onFilter({ action: entry.action })}>
              <Filter aria-hidden /> This action
            </Button>
            {entry.actor_id && entry.actor_id !== "0" ? (
              <Button type="button" variant="secondary" size="sm" onClick={() => onFilter({ actorId: entry.actor_id ?? undefined })}>
                <Filter aria-hidden /> This actor
              </Button>
            ) : null}
            {entry.target_id ? (
              <Button type="button" variant="secondary" size="sm" onClick={() => onFilter({ targetId: entry.target_id ?? undefined })}>
                <Filter aria-hidden /> This target
              </Button>
            ) : null}
          </div>
        </div>
      ) : null}
    </li>
  )
}
