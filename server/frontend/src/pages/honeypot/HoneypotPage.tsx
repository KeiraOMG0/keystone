import { useState } from "react"
import { Bar, BarChart, ResponsiveContainer, Tooltip } from "recharts"
import { Target } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Alert } from "@/components/ui/alert"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useToast } from "@/components/ui/toast"
import { CountBadge } from "@/components/ui/badge"
import { ResolvedChannel } from "@/components/ResolvedChannel"
import { ResolvedUser } from "@/components/ResolvedUser"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { ChartTooltip } from "@/components/patterns/ChartCard"
import { CHART } from "@/lib/chart"
import { EmptyState, ErrorState } from "@/components/patterns/states"
import { LiveCaption } from "@/components/patterns/LivePill"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { ApiError } from "@/api/client"
import { useAuth } from "@/hooks/useAuth"
import { useBotOffline } from "@/hooks/useBotOffline"
import { usePersistentString } from "@/lib/storage"
import { useMediaQuery } from "@/hooks/useMediaQuery"
import {
  useHoneypotAppeals,
  useHoneypotCatches,
  useHoneypotConfig,
  useHoneypotLive,
  useHoneypotStats,
  useUpdateHoneypotConfig,
} from "@/api/honeypot"
import type { ForbiddenError, HoneypotConfig } from "@/types/api"
import { AppealReview, AppealsQueue } from "./AppealsQueue"
import { CatchCard } from "./CatchCard"
import { RejoinCard } from "./RejoinCard"
import { ScrambleCard } from "./ScrambleCard"
import { SettingsPanel } from "./SettingsPanel"
import { SetupCard } from "./SetupCard"
import { StatsDialog } from "./StatsDialog"
import { ACTION_LABEL, apiErrorMessage } from "./format"

const TABS = ["catches", "appeals", "settings"] as const
type Tab = (typeof TABS)[number]

function StatusCard({ config, canManage }: { config: HoneypotConfig; canManage: boolean }) {
  const update = useUpdateHoneypotConfig()
  const { toast } = useToast()
  const bot = useBotOffline()
  // Optimistic: flip immediately, roll back with an error toast if the save fails.
  const [optimistic, setOptimistic] = useState<boolean | null>(null)
  const enabled = optimistic ?? config.enabled

  function toggle(next: boolean) {
    setOptimistic(next)
    update.mutate(
      { enabled: next },
      {
        onSuccess: () => toast({ title: next ? "Honeypot enabled" : "Honeypot paused", variant: "success" }),
        onError: (err) => toast({ title: "Could not update", description: apiErrorMessage(err), variant: "destructive" }),
        onSettled: () => setOptimistic(null),
      }
    )
  }

  return (
    <Card className="gap-3">
      <CardContent className="flex flex-wrap items-center gap-4">
        <div className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-primary-soft text-primary-soft-foreground">
          <Target className="size-5" aria-hidden />
        </div>
        <div className="min-w-0 flex-1 basis-60">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-md font-semibold text-foreground">{enabled ? "Honeypot is active" : "Honeypot is paused"}</h2>
            <Badge variant={enabled ? "success" : "default"} dot>
              {enabled ? "Active" : "Paused"}
            </Badge>
            <Badge variant="outline">{ACTION_LABEL[config.action] ?? config.action}</Badge>
          </div>
          <p className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
            <span>
              Trap channel: {config.channel_id ? <ResolvedChannel channelId={config.channel_id} /> : <span>not set</span>}
            </span>
            {config.log_channel_id ? (
              <span>
                Log channel: <ResolvedChannel channelId={config.log_channel_id} />
              </span>
            ) : null}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <Label htmlFor="hp-enabled">Enabled</Label>
          <Switch
            id="hp-enabled"
            checked={enabled}
            disabled={!canManage || update.isPending || bot.offline}
            title={!canManage ? "Only members with Ban Members can change this" : bot.reason}
            onCheckedChange={toggle}
          />
        </div>
      </CardContent>
      {!config.channel_exists ? (
        <CardContent>
          <Alert variant="warning">
            The honeypot channel no longer exists in Discord, so nothing is being caught. Ask an admin to recreate it.
          </Alert>
        </CardContent>
      ) : null}
    </Card>
  )
}

function AtAGlance({ onFullStats }: { onFullStats: () => void }) {
  const stats = useHoneypotStats(true)
  const s = stats.data
  const data = (s?.timeline_30d ?? []).slice(-14).map((d) => ({ label: d.date.slice(5), count: d.count }))
  const total14 = data.reduce((a, d) => a + d.count, 0)
  return (
    <Card>
      <CardHeader>
        <CardTitle>At a glance</CardTitle>
        <CardAction>
          <button type="button" onClick={onFullStats} className="rounded-sm text-sm font-medium text-primary hover:underline">
            Full stats
          </button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {stats.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : s ? (
          <>
            <dl className="grid grid-cols-3 gap-2 text-center">
              {[
                ["24 hours", s.totals.last_24h],
                ["7 days", s.totals.last_7d],
                ["30 days", s.totals.last_30d],
              ].map(([label, value]) => (
                <div key={label as string}>
                  <dd className="text-xl font-semibold tracking-tight tabular-nums">{value}</dd>
                  <dt className="text-xs text-muted-foreground">{label}</dt>
                </div>
              ))}
            </dl>
            <div role="img" aria-label={`Bar chart of catches per day over the last 14 days. Total ${total14}.`} className="h-14 w-full">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }} barCategoryGap="30%">
                  <Tooltip cursor={{ fill: "var(--hover)" }} content={<ChartTooltip unit="catches" />} />
                  <Bar dataKey="count" fill={CHART.c1} radius={[2, 2, 0, 0]} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </>
        ) : (
          <p className="text-sm text-muted-foreground">Stats are unavailable right now.</p>
        )}
      </CardContent>
    </Card>
  )
}

function PendingAppealsCard({ canManage, onViewAll }: { canManage: boolean; onViewAll: () => void }) {
  const pending = useHoneypotAppeals("pending")
  const appeals = (pending.data?.appeals ?? []).slice(0, 3)
  const total = pending.data?.appeals.length ?? 0
  return (
    <Card>
      <CardHeader>
        <CardTitle>Pending appeals</CardTitle>
        {total > 0 ? (
          <CardAction>
            <button type="button" onClick={onViewAll} className="rounded-sm text-sm font-medium text-primary hover:underline">
              View all ({total})
            </button>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {pending.isPending ? <Skeleton className="h-24 w-full" /> : null}
        {pending.isError ? <p className="text-sm text-muted-foreground">Could not load appeals.</p> : null}
        {pending.data && appeals.length === 0 ? (
          <p className="text-sm text-muted-foreground">No appeals waiting for review.</p>
        ) : null}
        {appeals.map((a) => (
          <div key={a.id} className="flex flex-col gap-2 border-b border-border pb-4 last:border-0 last:pb-0">
            <ResolvedUser userId={a.user_id} />
            <blockquote className="line-clamp-3 border-l-2 border-border pl-3 text-sm text-muted-foreground">
              {a.message || "No message provided."}
            </blockquote>
            <AppealReview appeal={a} canManage={canManage} compact />
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

/**
 * PROTECTED (requires "moderate_members" to view; "ban_members" to change
 * anything). Backed by /api/honeypot/* in server/routes/honeypot.py.
 * useHoneypotLive() is mounted ONCE here (never inside a tab panel).
 */
export default function HoneypotPage() {
  const { permissions } = useAuth()
  const canManage = permissions.ban_members === true
  const configQuery = useHoneypotConfig()
  const configured = configQuery.data?.configured === true
  const { freshIds } = useHoneypotLive()
  const catchesQuery = useHoneypotCatches(configured)
  const pendingAppeals = useHoneypotAppeals("pending", configured)
  const [tab, setTab] = usePersistentString<Tab>("kb-tab-honeypot", "catches", TABS)
  const [statsOpen, setStatsOpen] = useState(false)
  const wide = useMediaQuery("(min-width: 1024px)")

  if (configQuery.isError && configQuery.error instanceof ApiError && configQuery.error.status === 403) {
    const body = configQuery.error.body as ForbiddenError | null
    return <Forbidden403 reason="forbidden" requiredPermission={body?.required_permission ?? "moderate_members"} />
  }

  const pendingCount = pendingAppeals.data?.appeals.length ?? 0
  const catches = catchesQuery.data?.catches ?? []
  const newest = catches.find((c) => freshIds.has(c.id))

  return (
    <PageContainer>
      <PageHeader
        title="Honeypot"
        description="A trap channel that catches hacked accounts and spam bots."
        actions={configured ? <StatsDialog /> : null}
      />

      {configQuery.isPending ? (
        <div className="flex flex-col gap-4" aria-busy="true">
          <Skeleton className="h-24 w-full" />
          <Skeleton className="h-9 w-72" />
          <Skeleton className="h-44 w-full max-w-[520px]" />
          <Skeleton className="h-44 w-full max-w-[520px]" />
        </div>
      ) : configQuery.isError ? (
        <Card>
          <ErrorState
            title="Could not load the honeypot"
            message={apiErrorMessage(configQuery.error)}
            onRetry={() => void configQuery.refetch()}
            retrying={configQuery.isRefetching}
            showStatusLink
          />
        </Card>
      ) : !configured ? (
        <SetupCard canManage={canManage} />
      ) : (
        <>
          <StatusCard config={configQuery.data} canManage={canManage} />
          <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
            <TabsList>
              <TabsTrigger value="catches">Catches</TabsTrigger>
              <TabsTrigger value="appeals">
                Appeals
                {pendingCount > 0 ? <CountBadge count={pendingCount} /> : null}
              </TabsTrigger>
              <TabsTrigger value="settings">Settings</TabsTrigger>
            </TabsList>

            <div className={tab === "catches" ? "grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]" : ""}>
              <div className="min-w-0">
                <TabsContent value="catches" className="flex flex-col gap-4">
                  {/* Below 1024px the stats card sits above the feed (the rail stacks). */}
                  {!wide ? <AtAGlance onFullStats={() => setStatsOpen(true)} /> : null}
                  <LiveCaption
                    className="text-sm text-muted-foreground"
                    liveText="New catches appear here live."
                    fallbackText="Live updates paused. Checking for new catches every 15 s."
                  />
                  <div aria-live="polite" className="sr-only">
                    {newest ? `New catch: ${newest.username}` : ""}
                  </div>
                  {catchesQuery.isPending ? (
                    <div className="flex flex-col gap-3">
                      <Skeleton className="h-44 w-full max-w-[520px]" />
                      <Skeleton className="h-44 w-full max-w-[520px]" />
                    </div>
                  ) : catchesQuery.isError ? (
                    <Card>
                      <ErrorState
                        title="Could not load catches"
                        message={apiErrorMessage(catchesQuery.error)}
                        onRetry={() => void catchesQuery.refetch()}
                        retrying={catchesQuery.isRefetching}
                        showStatusLink
                      />
                    </Card>
                  ) : catches.length === 0 ? (
                    <Card>
                      <EmptyState icon={Target} title="Nothing caught yet" description="Catches show up here live." />
                    </Card>
                  ) : (
                    <div className="flex flex-col gap-6 px-2 py-2">
                      {catches.map((c) => (
                        <CatchCard key={c.id} item={c} isNew={freshIds.has(c.id)} />
                      ))}
                    </div>
                  )}
                </TabsContent>

                <TabsContent value="appeals">
                  <AppealsQueue catches={catches} canManage={canManage} />
                </TabsContent>

                <TabsContent value="settings">
                  <div className="flex max-w-xl flex-col gap-4">
                    {/* No remount key: SettingsPanel keeps a dirty draft through live config updates. */}
                    <SettingsPanel
                      config={configQuery.data}
                      canManage={canManage}
                    />
                    <RejoinCard config={configQuery.data} canManage={canManage} />
                    <ScrambleCard config={configQuery.data} canManage={canManage} />
                  </div>
                </TabsContent>
              </div>

              {tab === "catches" ? (
                <aside className="flex flex-col gap-4" aria-label="Honeypot summary">
                  {wide ? <AtAGlance onFullStats={() => setStatsOpen(true)} /> : null}
                  <PendingAppealsCard canManage={canManage} onViewAll={() => setTab("appeals")} />
                </aside>
              ) : null}
            </div>
          </Tabs>
          <StatsSheetBridge open={statsOpen} onOpenChange={setStatsOpen} />
        </>
      )}
    </PageContainer>
  )
}

/** Opens the (self-contained) stats dialog from the rail's "Full stats" link. */
function StatsSheetBridge({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  return <StatsDialog controlled={{ open, onOpenChange }} />
}
