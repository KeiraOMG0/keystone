import { useEffect, useMemo, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { useQueryClient } from "@tanstack/react-query"
import { ArrowDown, ArrowUp, Minus, MessageSquareWarning, Sparkles, Ticket, Activity } from "lucide-react"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Segmented } from "@/components/ui/segmented"
import { ResolvedUser } from "@/components/ResolvedUser"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { StatCard } from "@/components/patterns/StatCard"
import { EmptyState, ErrorState, ListSkeleton, StatRowSkeleton } from "@/components/patterns/states"
import { useAuth } from "@/hooks/useAuth"
import { useDashboardStats } from "@/api/stats"
import type { DashboardStatsResponse, HoneypotCatch, Ticket as TicketRow, Warning } from "@/types/api"
import { useModerationLive, useRecentWarnings } from "@/api/moderation"
import { useTickets } from "@/api/tickets"
import { useHoneypotCatches } from "@/api/honeypot"
import { useStatus } from "@/api/status"
import { useLive, useLiveTopic } from "@/live/useLive"
import { GenerateQotdDialog, QuickActionsCard, WarnUserDialog } from "@/components/layout/QuickActions"
import { ServerInfoCard } from "@/pages/dashboard/ServerInfoCard"
import { formatRelativeTime, padTicket } from "@/lib/format"
import { cn } from "@/lib/utils"

const PRESENCE_CARDS = [
  { key: "online" as const, label: "Online", dot: "bg-success" },
  { key: "idle" as const, label: "Idle", dot: "bg-idle" },
  { key: "dnd" as const, label: "Do not disturb", dot: "bg-dnd" },
  { key: "offline" as const, label: "Offline", dot: "bg-offline" },
]

type StatKey = "total" | "online" | "idle" | "dnd" | "offline"
type Trend = "up" | "down" | "same"
type Trends = Partial<Record<StatKey, Trend>>

const TREND_INDICATOR_MS = 3_000

/** Diffs each tracked field against the previously-seen stats snapshot, so tiles
 *  can flash a brief up/down/same indicator. Skips the first successful fetch,
 *  there is nothing to compare against yet. */
function useStatTrends(data: DashboardStatsResponse | undefined) {
  const previousRef = useRef<DashboardStatsResponse | null>(null)
  const [trends, setTrends] = useState<Trends>({})
  const timeoutsRef = useRef<Partial<Record<StatKey, ReturnType<typeof setTimeout>>>>({})

  useEffect(() => {
    if (!data) return
    const previous = previousRef.current
    previousRef.current = data

    if (!previous) return

    const keys: StatKey[] = ["total", "online", "idle", "dnd", "offline"]
    const nextTrends: Trends = {}
    for (const key of keys) {
      const prevValue = previous[key]
      const nextValue = data[key]
      if (prevValue == null || nextValue == null) continue
      if (nextValue === prevValue) nextTrends[key] = "same"
      else nextTrends[key] = nextValue > prevValue ? "up" : "down"
    }
    if (Object.keys(nextTrends).length === 0) return

    setTrends((current) => ({ ...current, ...nextTrends }))

    for (const key of Object.keys(nextTrends) as StatKey[]) {
      const existingTimeout = timeoutsRef.current[key]
      if (existingTimeout) clearTimeout(existingTimeout)
      timeoutsRef.current[key] = setTimeout(() => {
        setTrends((current) => {
          const { [key]: _removed, ...rest } = current
          return rest
        })
      }, TREND_INDICATOR_MS)
    }
  }, [data])

  useEffect(() => {
    const timeouts = timeoutsRef.current
    return () => {
      for (const timeout of Object.values(timeouts)) clearTimeout(timeout)
    }
  }, [])

  return trends
}

function TrendIndicator({ trend }: { trend: Trend | undefined }) {
  if (!trend) return null
  if (trend === "up") return <ArrowUp className="size-3.5 text-success" aria-label="increased" />
  if (trend === "down") return <ArrowDown className="size-3.5 text-danger" aria-label="decreased" />
  return <Minus className="size-3.5 text-muted-foreground" aria-label="unchanged" />
}

/**
 * PROTECTED. Overview: header actions, live stat cards, a unified activity
 * feed built client-side from data other pages already fetch (warnings, open
 * tickets, honeypot catches; each source only with its permission), a rail
 * with quick actions + bot status, and the recent warnings / open tickets cards.
 */
export default function DashboardPage() {
  const { data, isPending, isError, error, refetch, isRefetching } = useDashboardStats()
  const { permissions } = useAuth()
  const trends = useStatTrends(data)

  return (
    <PageContainer>
      <PageHeader
        title="Overview"
        description="Live activity across the server."
        actions={
          <>
            {permissions.manage_qotd ? (
              <GenerateQotdDialog
                trigger={
                  <Button variant="secondary">
                    <Sparkles /> Post QOTD
                  </Button>
                }
              />
            ) : null}
            {permissions.moderate_members ? (
              <WarnUserDialog
                trigger={
                  <Button>
                    <MessageSquareWarning /> Warn a user
                  </Button>
                }
              />
            ) : null}
          </>
        }
      />

      {/* Skeleton only on the very first load: refetches keep the last numbers (no flash). */}
      {isPending && !data ? <StatRowSkeleton /> : null}

      {isError && !data ? (
        <Card>
          <ErrorState
            title="Could not load stats"
            message={error instanceof Error ? error.message : "The dashboard stats request failed."}
            onRetry={() => void refetch()}
            retrying={isRefetching}
            showStatusLink
          />
        </Card>
      ) : null}

      {data ? (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-5">
          <StatCard
            className="col-span-2 sm:col-span-1"
            label="Total members"
            value={<>{data.total.toLocaleString()}</>}
            delta={<TrendIndicator trend={trends.total} />}
          />
          {!data.presence_available ? (
            <Card size="sm" className="col-span-2 flex justify-center px-5 sm:col-span-2 xl:col-span-4">
              <CardContent className="px-0 text-sm text-muted-foreground">
                Presence breakdown unavailable right now.
              </CardContent>
            </Card>
          ) : (
            PRESENCE_CARDS.map(({ key, label, dot }) => (
              <StatCard
                key={key}
                label={label}
                dotClassName={dot}
                value={<>{(data[key] ?? 0).toLocaleString()}</>}
                delta={<TrendIndicator trend={trends[key]} />}
              />
            ))
          )}
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <ActivityFeed />
        <div className="flex flex-col gap-4">
          <QuickActionsCard />
          <BotStatusCard />
          <ServerInfoCard />
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {permissions.moderate_members ? <RecentWarningsFeed /> : null}
        {permissions.manage_tickets ? <OpenTicketsFeed /> : null}
      </div>
    </PageContainer>
  )
}

// ---------------------------------------------------------------------------
// Live activity feed
// ---------------------------------------------------------------------------

type ActivityKind = "moderation" | "tickets" | "honeypot"
interface ActivityItem {
  key: string
  kind: ActivityKind
  at: string
  userId: string
  sentence: string
  detail: string | null
  badge: { label: string; variant: "success" | "warning" | "destructive" | "info" | "outline" | "default" }
  to: string
}

function warningItem(w: Warning): ActivityItem {
  return {
    key: `w${w.id}`,
    kind: "moderation",
    at: w.created_at,
    userId: w.user_id,
    sentence: "was warned",
    detail: w.reason,
    badge: { label: "Warning", variant: "warning" },
    to: "/dashboard/moderation",
  }
}
function ticketItem(t: TicketRow): ActivityItem {
  return {
    key: `t${t.id}`,
    kind: "tickets",
    at: t.opened_at,
    userId: t.opened_by,
    sentence: `opened ticket ${padTicket(t.ticket_number)}`,
    detail: t.panel_purpose,
    badge: t.claimed_by ? { label: "Claimed", variant: "outline" } : { label: "Open", variant: "success" },
    to: `/dashboard/tickets/${t.id}`,
  }
}
function catchItem(c: HoneypotCatch): ActivityItem {
  return {
    key: `h${c.id}`,
    kind: "honeypot",
    at: c.created_at,
    userId: c.user_id,
    sentence: "was caught in the honeypot",
    detail: c.content || null,
    badge: {
      label: `${c.action[0].toUpperCase()}${c.action.slice(1)} (${c.action_result})`,
      variant: c.action_result === "ok" ? "destructive" : c.action_result === "partial" ? "warning" : "default",
    },
    to: "/dashboard/honeypot",
  }
}

const FILTER_LABEL: Record<ActivityKind | "all", string> = {
  all: "All",
  moderation: "Moderation",
  tickets: "Tickets",
  honeypot: "Honeypot",
}

function ActivityFeed() {
  const { permissions } = useAuth()
  const qc = useQueryClient()
  const canMod = Boolean(permissions.moderate_members)
  const canTix = Boolean(permissions.manage_tickets)
  const [filter, setFilter] = useState<ActivityKind | "all">("all")

  // Keep the merged feed live using the existing topics; each is enabled only
  // when its data source is permitted. Same socket, ref-counted subscriptions.
  useLiveTopic("moderation", () => void qc.invalidateQueries({ queryKey: ["moderation", "warnings", "recent"] }), { enabled: canMod })
  useLiveTopic("tickets", () => void qc.invalidateQueries({ queryKey: ["tickets"] }), { enabled: canTix })
  useLiveTopic("honeypot", () => void qc.invalidateQueries({ queryKey: ["honeypot", "catches"] }), { enabled: canMod })

  const warnings = useRecentWarnings(canMod)
  const tickets = useTickets("open", canTix)
  const catches = useHoneypotCatches(canMod)

  const sources: ActivityKind[] = [...(canMod ? (["moderation", "honeypot"] as const) : []), ...(canTix ? (["tickets"] as const) : [])]
  const loading = (canMod && (warnings.isPending || catches.isPending)) || (canTix && tickets.isPending)
  const failed = (canMod && warnings.isError && catches.isError) || (canTix && tickets.isError && !canMod)

  const items = useMemo<ActivityItem[]>(() => {
    const all: ActivityItem[] = [
      ...(canMod ? (warnings.data?.warnings ?? []).map(warningItem) : []),
      ...(canMod ? (catches.data?.catches ?? []).map(catchItem) : []),
      ...(canTix ? (tickets.data ?? []).map(ticketItem) : []),
    ]
    return all.sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime())
  }, [canMod, canTix, warnings.data, catches.data, tickets.data])

  // Rows that arrive after the first paint get the "new" tint.
  const seen = useRef<Set<string> | null>(null)
  const fresh = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (loading) return
    if (seen.current === null) {
      seen.current = new Set(items.map((i) => i.key))
      return
    }
    for (const i of items) {
      if (!seen.current.has(i.key)) {
        seen.current.add(i.key)
        fresh.current.add(i.key)
      }
    }
  }, [items, loading])

  const visible = (filter === "all" ? items : items.filter((i) => i.kind === filter)).slice(0, 8)
  const filterOptions = (["all", ...sources] as (ActivityKind | "all")[]).map((v) => ({ value: v, label: FILTER_LABEL[v] }))

  if (sources.length === 0) return null

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>Live activity</CardTitle>
        <CardDescription>Recent warnings, tickets and honeypot catches. Updates as they happen.</CardDescription>
        {sources.length > 1 ? (
          <CardAction className="max-sm:col-start-1 max-sm:row-start-3 max-sm:mt-2 max-sm:justify-self-start">
            <Segmented size="sm" label="Filter activity" value={filter} onValueChange={setFilter} options={filterOptions} />
          </CardAction>
        ) : null}
      </CardHeader>
      {loading ? (
        <ListSkeleton rows={5} />
      ) : failed ? (
        <ErrorState title="Could not load activity" message="The activity sources did not respond." showStatusLink />
      ) : visible.length === 0 ? (
        <EmptyState icon={Activity} title="No recent activity" description="New warnings, tickets and catches will show up here." />
      ) : (
        <ul aria-live="polite" className="flex flex-col divide-y divide-border">
          {visible.map((i) => (
            <li key={i.key} className={cn(fresh.current.has(i.key) && "kb-new")}>
              <Link
                to={i.to}
                className="flex min-h-12 items-center gap-3 px-5 py-3 transition-colors duration-[120ms] hover:bg-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
              >
                <div className="min-w-0 flex-1">
                  <p className="flex flex-wrap items-center gap-x-1.5 text-base text-foreground">
                    <ResolvedUser userId={i.userId} />
                    <span className="text-muted-foreground">{i.sentence}</span>
                  </p>
                  {i.detail ? <p className="mt-0.5 truncate text-sm text-muted-foreground">{i.detail}</p> : null}
                </div>
                <Badge variant={i.badge.variant} className="shrink-0">
                  {i.badge.label}
                </Badge>
                <span className="w-16 shrink-0 text-right text-xs text-subtle-foreground tabular-nums">{formatRelativeTime(i.at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Rail: bot status
// ---------------------------------------------------------------------------

function BotStatusCard() {
  const { isStaff } = useAuth()
  const { data, isError } = useStatus()
  const { bot, state } = useLive()
  const botDown = bot.online === false || data?.bot.status === "down"
  const known = data !== undefined || bot.online !== null
  const latency = bot.latencyMs ?? data?.bot.latency_ms ?? null
  return (
    <Card className="gap-3">
      <CardHeader>
        <CardTitle>Bot status</CardTitle>
        <CardAction>
          {!known && !isError ? (
            <Badge>Checking</Badge>
          ) : botDown || isError ? (
            <Badge variant="destructive" dot>
              {isError ? "Unknown" : "Offline"}
            </Badge>
          ) : (
            <Badge variant="success" dot>
              Online
            </Badge>
          )}
        </CardAction>
      </CardHeader>
      <CardContent>
        <dl className="flex flex-col gap-2 text-sm">
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Gateway latency</dt>
            <dd className="font-medium text-foreground tabular-nums">{latency != null ? `${Math.round(latency)} ms` : "n/a"}</dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Live updates</dt>
            <dd className="font-medium text-foreground">{state === "live" ? "Connected" : state === "offline" ? "Polling" : "Connecting"}</dd>
          </div>
        </dl>
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-sm">
          <Link to="/status" className="font-medium text-primary hover:underline">
            Service status
          </Link>
          {isStaff ? (
            <Link to="/dashboard/diagnostics" className="font-medium text-primary hover:underline">
              Diagnostics
            </Link>
          ) : null}
        </div>
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Bottom row cards
// ---------------------------------------------------------------------------

function RecentWarningsFeed() {
  useModerationLive()
  const { data, isPending, isError, refetch } = useRecentWarnings()
  const warnings = (data?.warnings ?? []).slice(0, 5)

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>Recent warnings</CardTitle>
        <CardDescription>Latest moderation activity across the server.</CardDescription>
        <CardAction>
          <Button asChild variant="link" size="sm">
            <Link to="/dashboard/moderation">View all</Link>
          </Button>
        </CardAction>
      </CardHeader>
      {isPending ? <ListSkeleton rows={3} /> : null}
      {isError ? <ErrorState title="Could not load recent warnings" onRetry={() => void refetch()} className="py-6" /> : null}
      {data && warnings.length === 0 ? (
        <EmptyState icon={MessageSquareWarning} title="No warnings yet" description="Warnings issued recently will appear here." className="py-8" />
      ) : null}
      <ul className="flex flex-col divide-y divide-border">
        {warnings.map((w) => (
          <li key={w.id} className="flex items-center justify-between gap-3 px-5 py-3 text-base">
            <div className="flex min-w-0 items-center gap-2">
              <ResolvedUser userId={w.user_id} />
              {w.reason ? <span className="truncate text-muted-foreground">{w.reason}</span> : null}
            </div>
            <span className="shrink-0 text-xs text-subtle-foreground">{formatRelativeTime(w.created_at)}</span>
          </li>
        ))}
      </ul>
    </Card>
  )
}

function OpenTicketsFeed() {
  const { data, isPending, isError, refetch } = useTickets("open")
  const tickets = (data ?? []).slice(0, 5)

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>Open tickets</CardTitle>
        <CardDescription>Tickets currently awaiting staff.</CardDescription>
        <CardAction>
          <Button asChild variant="link" size="sm">
            <Link to="/dashboard/tickets">View all</Link>
          </Button>
        </CardAction>
      </CardHeader>
      {isPending ? <ListSkeleton rows={3} /> : null}
      {isError ? <ErrorState title="Could not load tickets" onRetry={() => void refetch()} className="py-6" /> : null}
      {data && tickets.length === 0 ? (
        <EmptyState icon={Ticket} title="No open tickets" description="Nothing is waiting on staff right now." className="py-8" />
      ) : null}
      <ul className="flex flex-col divide-y divide-border">
        {tickets.map((t) => (
          <li key={t.id}>
            <Link
              to={`/dashboard/tickets/${t.id}`}
              className="flex items-center justify-between gap-3 px-5 py-3 text-base transition-colors duration-[120ms] hover:bg-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
            >
              <div className="flex min-w-0 items-center gap-2">
                <span className="font-mono text-xs font-medium text-foreground">{padTicket(t.ticket_number)}</span>
                <ResolvedUser userId={t.opened_by} />
              </div>
              {t.claimed_by ? (
                <Badge variant="outline" className="shrink-0">
                  Claimed
                </Badge>
              ) : (
                <Badge variant="warning" className="shrink-0">
                  Unclaimed
                </Badge>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  )
}
