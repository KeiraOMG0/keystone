import { useMemo, useState, type ReactNode } from "react"
import { Area, AreaChart, ResponsiveContainer, Tooltip, YAxis } from "recharts"
import { ChevronDown, ChevronRight, Copy, Search, TriangleAlert } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { SortableHead } from "@/components/ui/table"
import { useToast } from "@/components/ui/toast"
import { CHART } from "@/lib/chart"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"
import { fmtDate } from "@/pages/diagnostics/fmt"

// ---------------------------------------------------------------------------
// Section card (anchor target for the sticky nav, collapsible)
// ---------------------------------------------------------------------------

export function Section({
  id,
  title,
  description,
  asOf,
  ttl,
  error,
  collapsed,
  onToggle,
  children,
}: {
  id: string
  title: string
  description?: string
  asOf?: string | null
  ttl?: number | null
  error?: string | null
  collapsed: boolean
  onToggle: () => void
  children: ReactNode
}) {
  return (
    <Card id={`sec-${id}`} className="scroll-mt-32">
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={!collapsed}
            aria-controls={`sec-body-${id}`}
            className="-m-1 flex min-w-0 items-start gap-2 rounded-md p-1 text-left select-none focus-visible:outline-2 focus-visible:outline-ring"
          >
            {collapsed ? <ChevronRight className="mt-1 size-4 shrink-0" aria-hidden /> : <ChevronDown className="mt-1 size-4 shrink-0" aria-hidden />}
            <div className="min-w-0">
              <CardTitle>{title}</CardTitle>
              {description ? <CardDescription>{description}</CardDescription> : null}
            </div>
          </button>
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
            {error ? <Badge variant="destructive">section error</Badge> : null}
            {asOf ? (
              <Badge variant="outline" title={`Cached server-side${ttl ? ` for ~${ttl}s` : ""}`}>
                as of {new Date(asOf).toLocaleTimeString()}
              </Badge>
            ) : null}
          </div>
        </div>
      </CardHeader>
      {collapsed ? null : (
        <CardContent id={`sec-body-${id}`} className="flex flex-col gap-5">
          {error ? <ErrorNote message={error} /> : children}
        </CardContent>
      )}
    </Card>
  )
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger-soft p-3 text-sm text-danger">
      <TriangleAlert className="mt-0.5 size-4 shrink-0" />
      <span className="break-words font-mono text-xs">{message}</span>
    </div>
  )
}

export function Sub({ title, right, children }: { title: string; right?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
        {right}
      </div>
      {children}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Small display atoms
// ---------------------------------------------------------------------------

export function Tile({
  label,
  value,
  hint,
  tone,
}: {
  label: string
  value: ReactNode
  hint?: ReactNode
  tone?: "ok" | "warn" | "bad"
}) {
  return (
    <div className="rounded-lg border border-border bg-muted/40 px-3 py-2">
      <div className="text-2xs font-semibold tracking-[0.06em] text-subtle-foreground uppercase">{label}</div>
      <div
        className={cn(
          "truncate text-lg font-semibold tabular-nums text-foreground",
          tone === "ok" && "text-success",
          tone === "warn" && "text-warning",
          tone === "bad" && "text-danger",
        )}
        title={typeof value === "string" ? value : undefined}
      >
        {value}
      </div>
      {hint ? <div className="truncate text-xs text-muted-foreground">{hint}</div> : null}
    </div>
  )
}

export function Tiles({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6">{children}</div>
}

export function KV({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
      {items.map(([k, v]) => (
        <div key={k} className="flex min-w-0 items-baseline justify-between gap-3 border-b border-border py-1">
          <dt className="shrink-0 text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-all text-right font-mono text-xs text-foreground">{v ?? "n/a"}</dd>
        </div>
      ))}
    </dl>
  )
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <code className={cn("break-all rounded bg-muted px-1 py-0.5 font-mono text-xs", className)}>{children}</code>
}

export function Pre({ lines, max = "max-h-72" }: { lines: string[]; max?: string }) {
  if (!lines.length) return <p className="text-sm text-muted-foreground">Empty.</p>
  return (
    <pre className={cn("overflow-auto rounded-lg border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed", max)}>
      {lines.join("\n")}
    </pre>
  )
}

export function Bar({ percent, warnAt = 75, badAt = 90 }: { percent: number | null | undefined; warnAt?: number; badAt?: number }) {
  const p = Math.max(0, Math.min(100, percent ?? 0))
  const color = p >= badAt ? "bg-danger" : p >= warnAt ? "bg-warning" : "bg-success"
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div className={cn("h-full rounded-full", color)} style={{ width: `${p}%` }} />
    </div>
  )
}

export function YesNo({ value, yes = "yes", no = "no", invert }: { value: boolean | null | undefined; yes?: string; no?: string; invert?: boolean }) {
  if (value === null || value === undefined) return <Badge variant="outline">n/a</Badge>
  const good = invert ? !value : value
  return <Badge variant={good ? "success" : "destructive"}>{value ? yes : no}</Badge>
}

export function Pills({ items, empty = "none" }: { items: string[]; empty?: string }) {
  if (!items.length) return <span className="text-sm text-muted-foreground">{empty}</span>
  return (
    <div className="flex flex-wrap gap-1">
      {items.map((i) => (
        <Badge key={i} variant="outline" className="font-mono">
          {i}
        </Badge>
      ))}
    </div>
  )
}

export function Dict({ data, fmt }: { data: Record<string, unknown> | null | undefined; fmt?: (k: string, v: unknown) => ReactNode }) {
  const entries = Object.entries(data ?? {})
  if (!entries.length) return <p className="text-sm text-muted-foreground">No data.</p>
  return <KV items={entries.map(([k, v]) => [k, fmt ? fmt(k, v) : String(v)] as [string, ReactNode])} />
}

export function when(iso?: string | null): string {
  return fmtDate(iso)
}

// ---------------------------------------------------------------------------
// Sortable table
// ---------------------------------------------------------------------------

export interface Col<T> {
  key: string
  label: string
  render?: (row: T) => ReactNode
  sort?: (row: T) => number | string | boolean | null | undefined
  right?: boolean
  className?: string
}

export function SortTable<T>({
  rows,
  cols,
  initialSort,
  desc = false,
  empty = "Nothing to show.",
  maxHeight = "max-h-96",
  limit,
}: {
  rows: T[]
  cols: Col<T>[]
  initialSort?: string
  desc?: boolean
  empty?: string
  maxHeight?: string
  limit?: number
}) {
  const [sortKey, setSortKey] = useState<string | undefined>(initialSort)
  const [descending, setDescending] = useState(desc)
  const [showAll, setShowAll] = useState(false)
  const [filter, setFilter] = useState("")
  const { toast } = useToast()

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return rows
    return rows.filter((r) => JSON.stringify(r).toLowerCase().includes(q))
  }, [rows, filter])

  const sorted = useMemo(() => {
    const col = cols.find((c) => c.key === sortKey)
    if (!col?.sort) return filtered
    const get = col.sort
    return [...filtered].sort((a, b) => {
      const x = get(a)
      const y = get(b)
      if (x == null && y == null) return 0
      if (x == null) return 1
      if (y == null) return -1
      const r = typeof x === "string" || typeof y === "string" ? String(x).localeCompare(String(y)) : Number(x) - Number(y)
      return descending ? -r : r
    })
  }, [filtered, cols, sortKey, descending])

  if (!rows.length) return <p className="text-sm text-muted-foreground">{empty}</p>
  const shown = limit && !showAll ? sorted.slice(0, limit) : sorted
  const showTools = rows.length > 8

  async function copyCsv() {
    const esc = (v: unknown) => {
      const t = v == null ? "" : String(v)
      return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t
    }
    const lines = [cols.map((c) => esc(c.label)).join(",")]
    for (const row of sorted) {
      lines.push(cols.map((c) => esc(c.sort ? c.sort(row) : (row as Record<string, unknown>)[c.key])).join(","))
    }
    try {
      await navigator.clipboard.writeText(lines.join("\n"))
      toast({ title: "Table copied", description: `${sorted.length} rows as CSV.`, variant: "success" })
    } catch {
      toast({ title: "Could not copy", description: "Clipboard access was denied by the browser.", variant: "destructive" })
    }
  }

  return (
    <div className="flex flex-col gap-1.5">
      {showTools ? (
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-full sm:w-64">
            <Search aria-hidden className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-subtle-foreground" />
            <Input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter rows"
              aria-label="Filter rows"
              className="h-[30px] w-full pl-8 text-sm"
            />
          </div>
          <button
            type="button"
            onClick={() => void copyCsv()}
            className="ml-auto inline-flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary-soft"
          >
            <Copy className="size-3" aria-hidden /> Copy CSV
          </button>
        </div>
      ) : null}
      <div className={cn("overflow-auto rounded-lg border border-border [&_[data-slot=table-cell]]:py-1.5 [&_[data-slot=table-cell]]:text-[13px]", maxHeight)}>
        <Table>
          <TableHeader>
            <TableRow>
              {cols.map((c) =>
                c.sort ? (
                  <SortableHead
                    key={c.key}
                    align={c.right ? "right" : "left"}
                    direction={sortKey === c.key ? (descending ? "desc" : "asc") : false}
                    onSort={() => {
                      if (sortKey === c.key) setDescending((d) => !d)
                      else {
                        setSortKey(c.key)
                        setDescending(false)
                      }
                    }}
                  >
                    {c.label}
                  </SortableHead>
                ) : (
                  <TableHead key={c.key} className={cn(c.right && "text-right", "whitespace-nowrap")}>
                    {c.label}
                  </TableHead>
                ),
              )}
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((row, i) => (
              <TableRow key={i}>
                {cols.map((c) => (
                  <TableCell key={c.key} className={cn(c.right && "text-right tabular-nums", "align-top", c.className)}>
                    {c.render ? c.render(row) : String((row as Record<string, unknown>)[c.key] ?? "")}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span aria-live="polite">
          Showing {shown.length} of {rows.length}
          {filter.trim() ? ` (filtered to ${filtered.length})` : ""}
        </span>
        {limit && filtered.length > limit ? (
          <button type="button" className="rounded-sm text-primary hover:underline" onClick={() => setShowAll((v) => !v)}>
            {showAll ? "Show fewer" : `Show all ${filtered.length}`}
          </button>
        ) : null}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sparkline (Recharts)
// ---------------------------------------------------------------------------

export function Spark({
  label,
  data,
  dataKey,
  format,
  zeroBased = true,
  color = CHART.c1,
}: {
  label: string
  data: object[]
  dataKey: string
  format: (n: number) => string
  zeroBased?: boolean
  color?: string
}) {
  const values = data.map((d) => (d as Record<string, unknown>)[dataKey]).filter((v): v is number => typeof v === "number")
  const last = values.length ? values[values.length - 1] : null
  const min = values.length ? Math.min(...values) : null
  const max = values.length ? Math.max(...values) : null
  const gradId = `g-${dataKey}`
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className="text-md font-semibold tabular-nums">{last === null ? "n/a" : format(last)}</span>
      </div>
      <div
        className="h-12"
        role="img"
        aria-label={`${label}. Latest ${last === null ? "not available" : format(last)}${max === null ? "" : `, peak ${format(max)}`}.`}
      >
        {values.length > 1 ? (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={data} margin={{ top: 4, right: 0, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={color} stopOpacity={0.14} />
                  <stop offset="100%" stopColor={color} stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <YAxis hide domain={zeroBased ? [0, "auto"] : ["auto", "auto"]} />
              <Tooltip
                contentStyle={{ fontSize: 12, padding: "4px 8px", background: "var(--popover)", color: "var(--popover-foreground)", border: "1px solid var(--border)", borderRadius: 6, boxShadow: "var(--elev-md)" }}
                labelFormatter={() => ""}
                formatter={(v) => [format(Number(v)), label]}
              />
              <Area type="monotone" dataKey={dataKey} stroke={color} strokeWidth={1.75} fill={`url(#${gradId})`} isAnimationActive={false} connectNulls dot={false} activeDot={{ r: 3.5 }} />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center text-xs text-muted-foreground">collecting samples...</div>
        )}
      </div>
      <div className="mt-1 flex justify-between text-2xs text-subtle-foreground">
        <span>min {min === null ? "-" : format(min)}</span>
        <span>max {max === null ? "-" : format(max)}</span>
      </div>
    </div>
  )
}
