import { useDeferredValue, useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import { Link } from "react-router-dom"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { StaffOnlyBadge } from "@/components/patterns/StaffOnlyBadge"
import { Alert } from "@/components/ui/alert"
import { CHART } from "@/lib/chart"
import { CircleCheck, CircleHelp, CircleX, Search, TriangleAlert } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError } from "@/api/client"
import { useDiagnostics, type DiagnosticsResponse, type Sec } from "@/api/diagnostics"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { apiErrorMessage } from "@/pages/honeypot/format"
import { computeHealth, overall, type Level } from "@/pages/diagnostics/health"
import { fmtBytes, fmtMs, fmtPct, fmtUptime } from "@/pages/diagnostics/fmt"
import { Section, Spark, Tile, Tiles } from "@/pages/diagnostics/widgets"
import { SupervisorPanel } from "@/pages/diagnostics/SupervisorPanel"
import { ClearCacheButton } from "@/pages/diagnostics/ClearCacheButton"
import { useSupervisorRestartWatcher } from "@/pages/diagnostics/supervisorRestartWatch"
import {
  ActivityBody,
  ChildrenBody,
  ConfigBody,
  CookiesBody,
  DashboardBody,
  DatabaseBody,
  DiscordBody,
  LogsBody,
  ProcessBody,
  ServicesBody,
  SystemBody,
} from "@/pages/diagnostics/sections"
import { cn } from "@/lib/utils"

// ---------------------------------------------------------------------------
// Client-side history (kept in state only; seeded from the bot's own sampler)
// ---------------------------------------------------------------------------

const HISTORY_LEN = 60

interface Sample {
  t: number
  cpu?: number
  rss?: number
  lag?: number
  latency?: number
  threads?: number
  handles?: number
  netUp?: number
  netDown?: number
  dashCpu?: number
  dashRss?: number
}

const MB = 1024 * 1024

function useHistory(data: DiagnosticsResponse | undefined, updatedAt: number) {
  const [samples, setSamples] = useState<Sample[]>([])
  const seeded = useRef(false)
  const prevNet = useRef<{ t: number; sent: number; recv: number } | null>(null)
  const lastAt = useRef(0)
  const [netRate, setNetRate] = useState<{ up: number; down: number } | null>(null)

  useEffect(() => {
    if (!data || !updatedAt || updatedAt === lastAt.current) return
    lastAt.current = updatedAt
    const b = data.bot
    const p = b.process
    const sys = b.system
    const net = sys?.net_io
    let up: number | undefined
    let down: number | undefined
    if (net && typeof net.bytes_sent === "number") {
      const prev = prevNet.current
      if (prev && updatedAt > prev.t) {
        const dt = (updatedAt - prev.t) / 1000
        up = Math.max(0, (net.bytes_sent - prev.sent) / dt)
        down = Math.max(0, (net.bytes_recv - prev.recv) / dt)
        setNetRate({ up, down })
      }
      prevNet.current = { t: updatedAt, sent: net.bytes_sent, recv: net.bytes_recv }
    }
    const dp = data.dashboard?.process
    const live: Sample = {
      t: updatedAt,
      cpu: p?.cpu_percent,
      rss: typeof p?.rss_bytes === "number" ? p.rss_bytes / MB : undefined,
      lag: p?.asyncio?.loop_lag_ms,
      latency: b.discord?.latency_ms ?? undefined,
      threads: p?.threads,
      handles: p?.handles,
      netUp: up !== undefined ? up / 1024 : undefined,
      netDown: down !== undefined ? down / 1024 : undefined,
      dashCpu: dp?.cpu_percent,
      dashRss: typeof dp?.rss_bytes === "number" ? dp.rss_bytes / MB : undefined,
    }
    setSamples((prev) => {
      let base = prev
      if (!seeded.current) {
        seeded.current = true
        const hist: Sec[] = b.activity?.history ?? []
        base = hist.map((h) => ({
          t: h.t * 1000,
          cpu: h.cpu,
          rss: typeof h.rss === "number" ? h.rss / MB : undefined,
          lag: h.lag_ms,
          latency: h.latency_ms ?? undefined,
          threads: h.threads,
        }))
      }
      return [...base, live].slice(-HISTORY_LEN)
    })
  }, [data, updatedAt])

  return { samples, netRate }
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

const LEVEL_ICON = {
  ok: { icon: CircleCheck, className: "text-success", label: "OK" },
  warn: { icon: TriangleAlert, className: "text-warning", label: "Warning" },
  bad: { icon: CircleX, className: "text-danger", label: "Problem" },
  na: { icon: CircleHelp, className: "text-subtle-foreground", label: "Unknown" },
} as const

const LEVEL_BADGE: Record<Level, "success" | "warning" | "destructive" | "outline"> = {
  ok: "success",
  warn: "warning",
  bad: "destructive",
  na: "outline",
}

const LEVEL_TEXT: Record<Level, string> = { ok: "Healthy", warn: "Needs attention", bad: "Problem detected", na: "Unknown" }

// ---------------------------------------------------------------------------
// Sections registry (order = sticky nav order)
// ---------------------------------------------------------------------------

const SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "supervisor", label: "Supervisor" },
  { id: "charts", label: "Live charts" },
  { id: "process", label: "Process" },
  { id: "children", label: "Children" },
  { id: "system", label: "System" },
  { id: "discord", label: "Discord" },
  { id: "database", label: "Database" },
  { id: "services", label: "Services" },
  { id: "cookies", label: "Cookies" },
  { id: "config", label: "Config" },
  { id: "logs", label: "Logs" },
  { id: "activity", label: "Activity" },
  { id: "dashboard", label: "Dashboard" },
] as const

type SectionId = (typeof SECTIONS)[number]["id"]

function flatten(value: unknown, path: string, out: Array<{ path: string; text: string }>, depth = 0) {
  if (out.length > 20000 || depth > 8) return
  if (Array.isArray(value)) {
    value.slice(0, 200).forEach((v, i) => flatten(v, `${path}[${i}]`, out, depth + 1))
  } else if (value && typeof value === "object") {
    for (const [k, v] of Object.entries(value)) flatten(v, path ? `${path}.${k}` : k, out, depth + 1)
  } else {
    out.push({ path, text: String(value) })
  }
}

function sectionOf(path: string): SectionId {
  const parts = path.split(".")
  if (parts[0] === "dashboard") return "dashboard"
  const key = (parts[0] === "bot" ? parts[1] : parts[0])?.replace(/\[.*/, "") as string
  return (SECTIONS.find((s) => s.id === key)?.id ?? "overview") as SectionId
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

/**
 * STAFF-ONLY ops console. Backed by GET /api/diagnostics (require_staff
 * server-side, same bar as Permissions/Settings; the nav link is gated the
 * same way). Kept as its own page rather than merged into the public Status
 * page: that one is unauthenticated and must stay minimal, while this dump
 * exposes paths, command lines and internals.
 */
export default function DiagnosticsPage() {
  // Never paused: the header Pause/Refresh/Copy/Download controls were removed.
  const query = useDiagnostics(false)
  // Watches a requested supervisor restart; lives here (always mounted) so it
  // survives the Services section collapsing or moving.
  useSupervisorRestartWatcher()
  const data = query.data
  const { samples, netRate } = useHistory(data, query.dataUpdatedAt)
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [filter, setFilter] = useState("")
  const deferredFilter = useDeferredValue(filter.trim().toLowerCase())
  const [active, setActive] = useState<SectionId>("overview")

  const health = useMemo(() => (data ? computeHealth(data) : []), [data])
  const level = overall(health)

  // Highlight the section nearest the top in the sticky nav.
  useEffect(() => {
    if (!data) return
    const els = SECTIONS.map((s) => document.getElementById(`sec-${s.id}`)).filter((e): e is HTMLElement => !!e)
    const io = new IntersectionObserver(
      (entries) => {
        const vis = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0]
        if (vis) setActive(vis.target.id.replace("sec-", "") as SectionId)
      },
      { rootMargin: "-100px 0px -65% 0px" },
    )
    els.forEach((e) => io.observe(e))
    return () => io.disconnect()
  }, [data])

  const results = useMemo(() => {
    if (!data || deferredFilter.length < 2) return []
    const flat: Array<{ path: string; text: string }> = []
    flatten(data, "", flat)
    return flat.filter((f) => f.path.toLowerCase().includes(deferredFilter) || f.text.toLowerCase().includes(deferredFilter)).slice(0, 150)
  }, [data, deferredFilter])

  if (query.isError && query.error instanceof ApiError && query.error.status === 403) {
    const body = query.error.body as { error?: string; required_permission?: string } | null
    return <Forbidden403 reason={body?.error === "not_a_member" ? "not_a_member" : "forbidden"} requiredPermission={body?.required_permission} />
  }

  const isCollapsed = (id: string) => !!collapsed[id]
  const toggle = (id: string) => setCollapsed((c) => ({ ...c, [id]: !c[id] }))
  const setAll = (v: boolean) => setCollapsed(Object.fromEntries(SECTIONS.map((s) => [s.id, v])))

  const bot = data?.bot
  const botUp = !!bot?.reachable
  const asOf = (s: Sec | undefined) => (s && typeof s === "object" ? (s.as_of ?? s.heavy_as_of ?? null) : null)
  const ttl = (s: Sec | undefined) => (s && typeof s === "object" ? (s.ttl_seconds ?? s.heavy_ttl_seconds ?? null) : null)
  const err = (s: Sec | undefined) => (s && typeof s.error === "string" ? (s.error as string) : null)

  function sec(id: SectionId, title: string, description: string, s: Sec | undefined, body: ReactNode) {
    return (
      <Section
        id={id}
        title={title}
        description={description}
        asOf={s ? asOf(s) : null}
        ttl={s ? ttl(s) : null}
        error={err(s) ?? (!s ? "Section missing from the response." : null)}
        collapsed={isCollapsed(id)}
        onToggle={() => toggle(id)}
      >
        {body}
      </Section>
    )
  }

  // Restart controls (supervisor.py). Rendered even when the dump failed to
  // load: they talk to the supervisor, not to the bot.
  const supervisorSection = (
    <Section
      id="supervisor"
      title="Services (supervisor.py)"
      description="Bot, dashboard and tunnel as supervisor.py sees them, with restart controls. Works while the bot is down."
      collapsed={isCollapsed("supervisor")}
      onToggle={() => toggle("supervisor")}
    >
      <SupervisorPanel active={!isCollapsed("supervisor")} />
    </Section>
  )

  return (
    <PageContainer width="wide" className="gap-4">
      <PageHeader
        title="Diagnostics"
        badge={<StaffOnlyBadge />}
        description="Health, resource use and internals of the bot and dashboard processes."
      />
      <p className="-mt-2 max-w-3xl text-sm text-muted-foreground">
        Secrets (tokens, client/session secrets, cookie values) are never shown. For the public up/down page see{" "}
        <Link to="/status" className="font-medium text-primary hover:underline">Status</Link>.
      </p>

      {/* Loading / hard error */}
      {!data && query.isPending ? (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : null}
      {!data && query.isError ? (
        <Alert variant="danger" title="Could not load diagnostics" action={<Button variant="secondary" size="sm" onClick={() => query.refetch()}>Try again</Button>}>
          {`${apiErrorMessage(query.error)} (${query.error instanceof ApiError ? `HTTP ${query.error.status}` : "error"})`}
        </Alert>
      ) : null}
      {!data && query.isError ? supervisorSection : null}

      {data ? (
        <>
          {/* Sticky nav + search */}
          <div className="sticky top-14 z-20 -mx-4 flex flex-col gap-2 border-b border-border bg-background/95 px-4 pt-2 backdrop-blur md:-mx-8 md:px-8">
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative min-w-56 flex-1">
                <Search aria-hidden className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-subtle-foreground" />
                <Input
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  data-page-search
                  placeholder="Search the whole dump (paths and values)..."
                  className="h-[30px] pl-8 text-sm max-md:h-11"
                  aria-label="Search diagnostics"
                />
              </div>
              <Button variant="ghost" size="xs" onClick={() => setAll(true)}>Collapse all</Button>
              <Button variant="ghost" size="xs" onClick={() => setAll(false)}>Expand all</Button>
            </div>
            <nav className="flex gap-1 overflow-x-auto [scrollbar-width:none]" aria-label="Diagnostics sections">
              {SECTIONS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => {
                    setCollapsed((c) => ({ ...c, [s.id]: false }))
                    document.getElementById(`sec-${s.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" })
                  }}
                  aria-current={active === s.id ? "true" : undefined}
                  className={cn(
                    "relative shrink-0 rounded-md px-2.5 py-2 text-sm font-medium transition-colors after:absolute after:inset-x-2 after:bottom-0 after:h-0.5 after:rounded-full after:bg-primary after:opacity-0 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring max-md:py-3",
                    active === s.id ? "text-foreground after:opacity-100" : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {s.label}
                </button>
              ))}
            </nav>
          </div>

          {deferredFilter.length >= 2 ? (
            <Card>
              <CardHeader>
                <CardTitle>Search results</CardTitle>
                <CardDescription>
                  {results.length ? `${results.length}${results.length === 150 ? "+" : ""} match(es) for "${filter.trim()}". Click a path to jump to its section.` : `Nothing matches "${filter.trim()}".`}
                </CardDescription>
              </CardHeader>
              {results.length ? (
                <CardContent>
                  <div className="max-h-72 overflow-auto rounded-lg border border-border">
                    {results.map((r, i) => (
                      <button
                        key={i}
                        type="button"
                        className="flex w-full items-baseline gap-3 border-b border-border px-3 py-1.5 text-left last:border-0 hover:bg-hover"
                        onClick={() => {
                          const id = sectionOf(r.path)
                          setCollapsed((c) => ({ ...c, [id]: false }))
                          document.getElementById(`sec-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" })
                        }}
                      >
                        <code className="w-1/2 shrink-0 truncate font-mono text-[11px] text-primary" title={r.path}>{r.path}</code>
                        <span className="min-w-0 flex-1 truncate font-mono text-xs" title={r.text}>{r.text}</span>
                      </button>
                    ))}
                  </div>
                </CardContent>
              ) : null}
            </Card>
          ) : null}

          {/* Overview */}
          <Section
            id="overview"
            title="Overview"
            description="Health checks derived from the dump below."
            collapsed={isCollapsed("overview")}
            onToggle={() => toggle("overview")}
          >
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={LEVEL_BADGE[level]} className="px-3">{LEVEL_TEXT[level]}</Badge>
              {health.filter((h) => h.level === "bad").length ? (
                <Badge variant="destructive">{health.filter((h) => h.level === "bad").length} problem(s)</Badge>
              ) : null}
              {health.filter((h) => h.level === "warn").length ? (
                <Badge variant="warning">{health.filter((h) => h.level === "warn").length} warning(s)</Badge>
              ) : null}
              <Badge variant="success">{health.filter((h) => h.level === "ok").length} healthy</Badge>
              <span className="text-xs text-muted-foreground">Viewing as {data.viewer.username || data.viewer.user_id}</span>
            </div>
            {!botUp ? (
              <Alert variant="danger" title="The bot process is unreachable">
                {bot?.error ?? "unknown"}
                {bot?.status_code ? `, HTTP ${bot.status_code}` : ""}. Only dashboard-side data is available below; the bot sections will return when it is back.
              </Alert>
            ) : null}
            {botUp && bot?.process && !bot.process.error ? (
              <Tiles>
                <Tile label="PID" value={bot.process.pid ?? "n/a"} />
                <Tile label="Uptime" value={fmtUptime(bot.process.uptime_seconds)} />
                <Tile label="CPU" value={fmtPct(bot.process.cpu_percent)} tone={bot.process.cpu_percent > 80 ? "bad" : bot.process.cpu_percent > 50 ? "warn" : undefined} />
                <Tile label="RSS" value={fmtBytes(bot.process.rss_bytes)} />
                <Tile
                  label="Loop lag"
                  value={fmtMs(bot.process.asyncio?.loop_lag_ms)}
                  tone={bot.process.asyncio?.loop_lag_ms > 500 ? "bad" : bot.process.asyncio?.loop_lag_ms > 100 ? "warn" : undefined}
                />
                <Tile label="Asyncio tasks" value={bot.process.asyncio?.tasks?.total ?? "n/a"} />
              </Tiles>
            ) : null}
            <ul className="grid gap-x-8 sm:grid-cols-2">
              {health.map((h) => {
                const L = LEVEL_ICON[h.level]
                return (
                  <li key={h.id} className="border-b border-border">
                    <button
                      type="button"
                      disabled={!h.section}
                      onClick={() => {
                        if (!h.section) return
                        setCollapsed((c) => ({ ...c, [h.section as string]: false }))
                        document.getElementById(`sec-${h.section}`)?.scrollIntoView({ behavior: "smooth", block: "start" })
                      }}
                      className="flex w-full items-start gap-2.5 rounded-md px-1 py-2.5 text-left transition-colors enabled:hover:bg-hover"
                    >
                      <L.icon className={cn("mt-0.5 size-4 shrink-0", L.className)} aria-hidden />
                      <span className="min-w-0 flex-1">
                        <span className="block text-base font-medium text-foreground">{h.label}</span>
                        <span className="block break-words text-xs text-muted-foreground">{h.detail}</span>
                      </span>
                      <Badge variant={LEVEL_BADGE[h.level]} className="shrink-0">{L.label}</Badge>
                    </button>
                  </li>
                )
              })}
            </ul>
          </Section>

          {supervisorSection}

          {/* Live charts */}
          <Section
            id="charts"
            title="Live charts"
            description={`Last ${HISTORY_LEN} samples kept in this browser tab (seeded from the bot's own 10s sampler on first load).`}
            collapsed={isCollapsed("charts")}
            onToggle={() => toggle("charts")}
          >
            <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
              <Spark label="Bot CPU" data={samples} dataKey="cpu" format={(n) => fmtPct(n)} />
              <Spark label="Bot RAM (RSS)" data={samples} dataKey="rss" format={(n) => fmtBytes(n * MB)} />
              <Spark label="Event-loop lag" data={samples} dataKey="lag" format={(n) => fmtMs(n)} color={CHART.c2} />
              <Spark label="Gateway latency" data={samples} dataKey="latency" format={(n) => fmtMs(n)} color={CHART.c2} />
              <Spark label="Bot threads" data={samples} dataKey="threads" format={(n) => String(Math.round(n))} zeroBased={false} />
              <Spark label="Bot handles" data={samples} dataKey="handles" format={(n) => String(Math.round(n))} zeroBased={false} />
              <Spark label="Net upload (host)" data={samples} dataKey="netUp" format={(n) => `${fmtBytes(n * 1024)}/s`} color={CHART.c1} />
              <Spark label="Net download (host)" data={samples} dataKey="netDown" format={(n) => `${fmtBytes(n * 1024)}/s`} color={CHART.c1} />
              <Spark label="Dashboard CPU" data={samples} dataKey="dashCpu" format={(n) => fmtPct(n)} />
              <Spark label="Dashboard RAM (RSS)" data={samples} dataKey="dashRss" format={(n) => fmtBytes(n * MB)} />
            </div>
          </Section>

          {botUp && bot ? (
            <>
              {sec("process", "Process", "The bot process: CPU, memory, threads, sockets, asyncio, GC.", bot.process, bot.process && <ProcessBody s={bot.process} />)}
              {sec("children", "Child processes", "Everything the bot spawned (recursive).", bot.children, bot.children && <ChildrenBody s={bot.children} />)}
              {sec("system", "System", "Host memory, CPU, disks, network and listening ports.", bot.system, bot.system && (
                <>
                  <SystemBody s={bot.system} netRate={netRate} />
                  <ClearCacheButton />
                </>
              ))}
              {sec("discord", "Discord", "Gateway, guild, cogs, commands and the bot's permissions.", bot.discord, bot.discord && <DiscordBody s={bot.discord} />)}
              {sec("database", "Database", "bot.db size, migrations, row counts, integrity.", bot.database, bot.database && <DatabaseBody s={bot.database} />)}
              {sec("services", "Services and background", "Internal API, pot server, task loops, honeypot.", bot.services, bot.services && <ServicesBody s={bot.services} />)}
              {sec("cookies", "Music cookies", "Per-cookie metadata for the yt-dlp cookies file. Values are never shown.", bot.cookies, bot.cookies && <CookiesBody s={bot.cookies} />)}
              {sec("config", "Config and versions", "Allowlisted environment values and package versions.", bot.config, bot.config && <ConfigBody s={bot.config} />)}
              {sec("logs", "Logs", "Live tails and WARNING/ERROR counts of the bot, dashboard, tunnel, supervisor and pot server logs.", bot.logs, bot.logs && <LogsBody s={bot.logs} />)}
              {sec("activity", "Activity", "Command usage, interactions and gateway events since the bot started.", bot.activity, bot.activity && <ActivityBody s={bot.activity} />)}
            </>
          ) : null}

          {sec("dashboard", "Dashboard process", "This web dashboard: process, sessions, requests, tunnel and ports.", data.dashboard, <DashboardBody s={data.dashboard} botReachable={botUp} />)}
        </>
      ) : null}
    </PageContainer>
  )
}
