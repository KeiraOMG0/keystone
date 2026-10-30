import { useState, type ReactNode } from "react"
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { BarChart3 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { ChartCard, ChartTooltip } from "@/components/patterns/ChartCard"
import { CHART } from "@/lib/chart"
import { EmptyState, ErrorState } from "@/components/patterns/states"
import { Skeleton } from "@/components/ui/skeleton"
import { useHoneypotStats } from "@/api/honeypot"
import type { HoneypotStats } from "@/types/api"
import { apiErrorMessage, formatDateTime, formatDuration, pct } from "./format"

function Tile({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="flex flex-col gap-0.5 rounded-lg border border-border bg-card p-3">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-xl font-semibold tracking-tight tabular-nums">{value}</span>
      {hint ? <span className="text-xs text-muted-foreground">{hint}</span> : null}
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  )
}

function SignalBar({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="grid grid-cols-[minmax(0,8rem)_1fr_3.5rem] items-center gap-3 text-sm">
      <span className="truncate text-muted-foreground">{label}</span>
      <Progress value={value ?? 0} aria-label={label} />
      <span className="text-right tabular-nums">{pct(value)}</span>
    </div>
  )
}

function StatsBody({ s }: { s: HoneypotStats }) {
  const timeline = s.timeline_30d.map((d) => ({ ...d, label: d.date.slice(5) }))
  const hours = s.by_hour_utc.map((count, h) => ({ label: `${String(h).padStart(2, "0")}:00`, count }))
  const peakHour = hours.reduce((a, b) => (b.count > a.count ? b : a), hours[0] ?? { label: "n/a", count: 0 })

  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile label="All time" value={s.totals.all} />
        <Tile label="Last 24 hours" value={s.totals.last_24h} />
        <Tile label="Last 7 days" value={s.totals.last_7d} />
        <Tile label="Last 30 days" value={s.totals.last_30d} />
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile label="Bans" value={s.by_action.ban} />
        <Tile label="Softbans" value={s.by_action.softban} />
        <Tile label="Kicks" value={s.by_action.kick} />
        <Tile
          label="Fully applied"
          value={`${s.by_result.ok}/${s.totals.all}`}
          hint={`${s.by_result.partial} partial, ${s.by_result.failed} failed`}
        />
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <ChartCard title="Catches per day (30 days, UTC)" caption={`Total ${s.totals.last_30d} in the last 30 days.`}>
          <div role="img" aria-label={`Bar chart of catches per day over the last 30 days. Total ${s.totals.last_30d}.`} className="h-[180px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={timeline} margin={{ top: 4, right: 4, bottom: 0, left: -20 }} barCategoryGap="30%">
                <CartesianGrid stroke={CHART.grid} strokeDasharray="3 4" vertical={false} />
                <XAxis dataKey="label" tick={CHART.tick} tickLine={false} axisLine={false} interval={4} />
                <YAxis tick={CHART.tick} tickLine={false} axisLine={false} allowDecimals={false} />
                <Tooltip cursor={{ fill: "var(--hover)" }} content={<ChartTooltip unit="catches" />} />
                <Bar dataKey="count" fill={CHART.c1} radius={[2, 2, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </ChartCard>

        <ChartCard title="Catches by hour (UTC)" caption={`Busiest hour ${peakHour.label} with ${peakHour.count}.`}>
          <div role="img" aria-label={`Bar chart of catches by hour of day in UTC. Busiest hour ${peakHour.label} with ${peakHour.count}.`} className="h-[180px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={hours} margin={{ top: 4, right: 4, bottom: 0, left: -20 }} barCategoryGap="30%">
                <CartesianGrid stroke={CHART.grid} strokeDasharray="3 4" vertical={false} />
                <XAxis dataKey="label" tick={CHART.tick} tickLine={false} axisLine={false} interval={3} />
                <YAxis tick={CHART.tick} tickLine={false} axisLine={false} allowDecimals={false} />
                <Tooltip cursor={{ fill: "var(--hover)" }} content={<ChartTooltip unit="catches" />} />
                <Bar dataKey="count" radius={[2, 2, 0, 0]} isAnimationActive={false}>
                  {hours.map((h) => (
                    <Cell key={h.label} fill={h.label === peakHour.label ? CHART.c2 : CHART.c1} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </ChartCard>
      </div>

      <div className="grid gap-6 md:grid-cols-3">
        <Section title="Account age at catch">
          <div className="grid grid-cols-2 gap-3">
            <Tile label="Median" value={s.account_age.median_days === null ? "n/a" : `${s.account_age.median_days}d`} />
            <Tile label="Average" value={s.account_age.avg_days === null ? "n/a" : `${s.account_age.avg_days}d`} />
            <Tile label="Under 7 days" value={pct(s.account_age.under_7d_pct)} />
            <Tile label="Under 30 days" value={pct(s.account_age.under_30d_pct)} />
          </div>
        </Section>
        <Section title="Time from join to catch">
          <div className="grid grid-cols-2 gap-3">
            <Tile label="Median" value={formatDuration(s.time_to_catch.median_seconds)} />
            <Tile label="Within 1 minute" value={pct(s.time_to_catch.under_60s_pct)} />
            <Tile label="Within 1 hour" value={pct(s.time_to_catch.under_1h_pct)} />
          </div>
        </Section>
        <Section title="Signals">
          <div className="flex flex-col gap-2">
            <SignalBar label="Default avatar" value={s.signals.default_avatar_pct} />
            <SignalBar label="Contains link" value={s.signals.link_pct} />
            <SignalBar label="Discord invite" value={s.signals.invite_pct} />
            <SignalBar label="@everyone ping" value={s.signals.everyone_pct} />
            <SignalBar label="Attachment" value={s.signals.attachment_pct} />
            <SignalBar label="DM delivered" value={s.signals.dm_delivered_pct} />
          </div>
        </Section>
      </div>

      <Section title="Most repeated messages">
        {s.top_messages.length === 0 ? (
          <p className="text-sm text-muted-foreground">No message has been posted more than once yet.</p>
        ) : (
          <ol className="flex flex-col gap-1.5">
            {s.top_messages.map((m, i) => (
              <li key={i} className="flex items-start justify-between gap-3 rounded-md border border-border bg-card px-3 py-2 text-sm">
                <span className="min-w-0 break-words text-muted-foreground">{m.preview}</span>
                <span className="shrink-0 font-medium tabular-nums">x{m.count}</span>
              </li>
            ))}
          </ol>
        )}
      </Section>

      <Section title="Appeals">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <Tile label="Total" value={s.appeals.total} />
          <Tile label="Pending" value={s.appeals.pending} />
          <Tile label="Approved" value={s.appeals.approved} />
          <Tile label="Denied" value={s.appeals.denied} />
          <Tile label="False positives" value={pct(s.appeals.false_positive_pct)} hint="approved / total bans" />
        </div>
      </Section>

      <p className="text-xs text-muted-foreground">
        First catch {formatDateTime(s.first_catch_at)}. Latest catch {formatDateTime(s.last_catch_at)}.
      </p>
    </div>
  )
}

/** "View stats" button + dialog. Stats are only fetched once the dialog opens. */
export function StatsDialog({ controlled }: { controlled?: { open: boolean; onOpenChange: (o: boolean) => void } }) {
  const [ownOpen, setOwnOpen] = useState(false)
  const open = controlled ? controlled.open : ownOpen
  const setOpen = controlled ? controlled.onOpenChange : setOwnOpen
  const query = useHoneypotStats(open)

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {controlled ? null : (
        <DialogTrigger asChild>
          <Button variant="secondary">
            <BarChart3 aria-hidden />
            View stats
          </Button>
        </DialogTrigger>
      )}
      <DialogContent className="max-w-[880px]">
        <DialogHeader>
          <DialogTitle>Honeypot stats</DialogTitle>
          <DialogDescription>What the trap has caught and how well it is working.</DialogDescription>
        </DialogHeader>
        {query.isPending ? (
          <div className="flex flex-col gap-3">
            <Skeleton className="h-20 w-full" />
            <Skeleton className="h-52 w-full" />
          </div>
        ) : query.isError ? (
          <ErrorState title="Could not load stats" message={apiErrorMessage(query.error)} onRetry={() => void query.refetch()} retrying={query.isRefetching} />
        ) : query.data.totals.all === 0 ? (
          <EmptyState icon={BarChart3} title="Nothing caught yet" description="Stats appear after the first catch." />
        ) : (
          <StatsBody s={query.data} />
        )}
      </DialogContent>
    </Dialog>
  )
}
