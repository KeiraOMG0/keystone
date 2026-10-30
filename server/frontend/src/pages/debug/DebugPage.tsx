import { useEffect, useMemo, useState } from "react"
import { Activity, Bug, Database, ExternalLink, Play, RefreshCw, ShoppingBag } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Field } from "@/components/ui/field"
import { Label } from "@/components/ui/label"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { EmptyState, ErrorState, ListSkeleton } from "@/components/patterns/states"
import NotFound404 from "@/pages/notfound/NotFound404"
import { ApiError } from "@/api/client"
import { useAuth } from "@/hooks/useAuth"
import { useBotOffline } from "@/hooks/useBotOffline"
import { OWNER_ONLY_PERMISSION } from "@/lib/owner"
import { readStorage, writeStorage } from "@/lib/storage"
import { useDebugActions, useDebugChannels, useDebugHealth, useFortnitePreview, useRunDebugAction } from "@/api/debug"
import type { DebugAction, DebugRunResult, GuildChannel } from "@/types/api"
import { AppealTestCard } from "./AppealTestCard"
import { LabTab } from "./lab/LabTab"
import { ErrorsTab } from "./errors/ErrorsTab"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useSearchParams } from "react-router-dom"

const CHANNEL_KEY = "kb-debug-channel"
const COOLDOWN_MS = 2000

function is404(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body as { error?: string; message?: string } | null
    if (body?.message) return body.message
    if (body?.error) return body.error.replace(/_/g, " ")
    return error.status ? `Request failed (${error.status})` : "Network error"
  }
  return error instanceof Error ? error.message : "Unknown error"
}

function formatUptime(seconds: number | undefined): string {
  if (seconds === undefined || seconds === null) return "n/a"
  const s = Math.floor(seconds)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  return d ? `${d}d ${h}h ${m}m` : h ? `${h}h ${m}m` : `${m}m ${s % 60}s`
}

/**
 * HIDDEN, OWNER-ONLY test bench (/dashboard/debug). Renders <NotFound404> for
 * everyone but OWNER_ID (from /api/me `is_owner`), and the backend answers 404
 * too (require_owner). Pick a target channel once; every test action posts
 * the feature's real output there with "[TEST]" and no role/everyone pings,
 * and never changes feature state (see services/debug_actions_service.py).
 */
export default function DebugPage() {
  const { permissions } = useAuth()
  const isOwner = Boolean(permissions[OWNER_ONLY_PERMISSION])
  const actionsQuery = useDebugActions(isOwner)
  const channelsQuery = useDebugChannels(isOwner)
  const [channelId, setChannelIdState] = useState<string>(() => readStorage(CHANNEL_KEY) ?? "")
  // ?tab=lab / ?tab=errors select those tabs (deep-linkable; /admin -> errors); anything else = Actions.
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get("tab")
  const tab = tabParam === "lab" || tabParam === "errors" ? tabParam : "actions"
  const selectTab = (next: string) =>
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev)
        if (next === "lab" || next === "errors") params.set("tab", next)
        else params.delete("tab")
        return params
      },
      { replace: true }
    )

  const setChannelId = (v: string) => {
    setChannelIdState(v)
    writeStorage(CHANNEL_KEY, v)
  }

  // Forget a remembered channel that no longer exists.
  useEffect(() => {
    if (channelsQuery.data && channelId && !channelsQuery.data.some((c) => c.id === channelId)) setChannelId("")
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelsQuery.data])

  const groups = useMemo(() => {
    const out = new Map<string, DebugAction[]>()
    for (const a of actionsQuery.data?.actions ?? []) {
      out.set(a.group, [...(out.get(a.group) ?? []), a])
    }
    return [...out.entries()]
  }, [actionsQuery.data])

  if (!isOwner || is404(actionsQuery.error) || is404(channelsQuery.error)) return <NotFound404 />

  const channelName = channelsQuery.data?.find((c) => c.id === channelId)?.name

  return (
    <PageContainer>
      <PageHeader
        title="Debug"
        badge={<Badge variant="accent"><Bug /> Owner only</Badge>}
        description="Fire any feature's channel output into a test channel, use the Lab's read-only test tools, or see every error the site and bot have hit. Nothing here changes real state or pings roles."
      />

      <Tabs value={tab} onValueChange={selectTab}>
        <TabsList>
          <TabsTrigger value="actions">Actions</TabsTrigger>
          <TabsTrigger value="lab">Lab</TabsTrigger>
          <TabsTrigger value="errors">Errors</TabsTrigger>
        </TabsList>
      </Tabs>

      {tab === "errors" ? (
        <ErrorsTab enabled={isOwner} />
      ) : tab === "lab" ? (
        <LabTab channelId={channelId} channelName={channelName} />
      ) : (
      <>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <TargetChannelCard
          channels={channelsQuery.data}
          loading={channelsQuery.isPending}
          error={channelsQuery.error}
          onRetry={() => channelsQuery.refetch()}
          value={channelId}
          onChange={setChannelId}
        />
        <HealthCard />
      </div>

      {actionsQuery.isPending ? (
        <Card>
          <CardContent>
            <ListSkeleton rows={4} avatar={false} />
          </CardContent>
        </Card>
      ) : actionsQuery.isError ? (
        <Card>
          <ErrorState
            title="Could not load test actions"
            message={errorText(actionsQuery.error)}
            onRetry={() => actionsQuery.refetch()}
            retrying={actionsQuery.isFetching}
            showStatusLink
          />
        </Card>
      ) : groups.length === 0 ? (
        <Card>
          <EmptyState icon={Bug} title="No test actions" description="The bot reported no debug actions." />
        </Card>
      ) : (
        groups.map(([group, actions]) => (
          <section key={group} className="flex flex-col gap-3" aria-labelledby={`dbg-${group}`}>
            <h2 id={`dbg-${group}`} className="text-md font-semibold text-foreground">
              {group}
            </h2>
            <div className="grid gap-4 md:grid-cols-2">
              {actions.map((a) => (
                <ActionCard key={a.id} action={a} channelId={channelId} channelName={channelName} />
              ))}
            </div>
          </section>
        ))
      )}

      <FortnitePreviewCard />

      <AppealTestCard />

      {actionsQuery.data?.skipped?.length ? (
        <Card>
          <CardHeader>
            <CardTitle>Not testable here</CardTitle>
            <CardDescription>These need real side effects, so they are skipped on purpose.</CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-col gap-2 text-sm">
              {actionsQuery.data.skipped.map((s) => (
                <li key={s.feature}>
                  <span className="font-medium text-foreground">{s.feature}</span>
                  <span className="text-muted-foreground">: {s.reason}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}
      </>
      )}
    </PageContainer>
  )
}

function TargetChannelCard({
  channels,
  loading,
  error,
  onRetry,
  value,
  onChange,
}: {
  channels: GuildChannel[] | undefined
  loading: boolean
  error: unknown
  onRetry: () => void
  value: string
  onChange: (v: string) => void
}) {
  const sorted = useMemo(() => [...(channels ?? [])].sort((a, b) => a.position - b.position), [channels])
  return (
    <Card>
      <CardHeader>
        <CardTitle>Target channel</CardTitle>
        <CardDescription>Every test below posts here. Remembered in this browser.</CardDescription>
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-9 w-full" />
        ) : error ? (
          <Alert
            variant="danger"
            title="Could not load channels"
            action={
              <Button size="sm" variant="secondary" onClick={onRetry}>
                Retry
              </Button>
            }
          >
            {errorText(error)}
          </Alert>
        ) : (
          <Field label="Text channel" hint={value ? undefined : "Pick a channel to enable the test buttons."}>
            {(p) => (
              <Select {...p} value={value} onChange={(e) => onChange(e.target.value)}>
                <option value="">Select a channel…</option>
                {sorted.map((c) => (
                  <option key={c.id} value={c.id}>
                    #{c.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        )}
      </CardContent>
    </Card>
  )
}

function HealthCard() {
  const q = useDebugHealth(true)
  const h = q.data
  const failedLoops = (h?.loops ?? []).filter((l) => l.failed || l.error || l.running === false)
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Activity className="size-4" aria-hidden /> Bot health
        </CardTitle>
        <CardDescription>Refreshes every 15 seconds. Full detail lives on Diagnostics.</CardDescription>
      </CardHeader>
      <CardContent>
        {q.isPending ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-5 w-1/2" />
            <Skeleton className="h-5 w-3/4" />
          </div>
        ) : q.isError || !h ? (
          <Alert variant="danger" title="Bot unreachable">
            {errorText(q.error)}
          </Alert>
        ) : (
          <div className="flex flex-col gap-3 text-sm">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5">
              <dt className="text-muted-foreground">Gateway</dt>
              <dd>{h.ready ? <Badge variant="success" dot>Ready</Badge> : <Badge variant="warning" dot>Not ready</Badge>}</dd>
              <dt className="text-muted-foreground">Latency</dt>
              <dd className="font-mono">{h.latency_ms !== null ? `${h.latency_ms} ms` : "n/a"}</dd>
              <dt className="text-muted-foreground">Process uptime</dt>
              <dd className="font-mono">{h.process.error ? "n/a" : formatUptime(h.process.uptime_seconds)}</dd>
              <dt className="text-muted-foreground">Extensions</dt>
              <dd className="font-mono">
                {h.extensions_loaded.length}
                {h.extensions_expected.length ? ` / ${h.extensions_expected.length}` : ""} loaded
              </dd>
              <dt className="text-muted-foreground">Cogs</dt>
              <dd className="font-mono">{h.cogs.length}</dd>
              <dt className="text-muted-foreground">Task loops</dt>
              <dd className="font-mono">
                {h.loops.length} ({failedLoops.length} stopped/failed)
              </dd>
            </dl>
            {h.extensions_failed.length ? (
              <Alert variant="danger" title="Failed to load">
                <ul className="flex flex-col gap-1">
                  {h.extensions_failed.map((f) => (
                    <li key={f.extension}>
                      <span className="font-mono">{f.extension}</span>: {f.error}
                    </li>
                  ))}
                </ul>
              </Alert>
            ) : (
              <Alert variant="success">Every expected extension is loaded.</Alert>
            )}
            {failedLoops.length ? (
              <Alert variant="warning" title="Loops not running">
                {failedLoops.map((l) => `${l.cog}.${l.name}`).join(", ")}
              </Alert>
            ) : null}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function ActionCard({ action, channelId, channelName }: { action: DebugAction; channelId: string; channelName?: string }) {
  const run = useRunDebugAction()
  const { offline, reason } = useBotOffline()
  const [values, setValues] = useState<Record<string, string | boolean>>({})
  const [result, setResult] = useState<DebugRunResult | null>(null)
  const [requestError, setRequestError] = useState<string | null>(null)
  const [coolingDown, setCoolingDown] = useState(false)

  const missingRequired = action.params.some((p) => p.required && !values[p.name])
  const disabledReason = !action.available
    ? action.unavailable_reason ?? "Unavailable"
    : offline
      ? reason
      : !channelId
        ? "Pick a target channel first"
        : missingRequired
          ? "Fill in the required options"
          : undefined

  const onRun = () => {
    setResult(null)
    setRequestError(null)
    run.mutate(
      { action: action.id, channel_id: channelId, params: values },
      {
        onSuccess: (data) => setResult(data),
        onError: (err) => setRequestError(errorText(err)),
        onSettled: () => {
          setCoolingDown(true)
          window.setTimeout(() => setCoolingDown(false), COOLDOWN_MS)
        },
      }
    )
  }

  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          {action.label}
          {!action.available ? <Badge variant="warning">Unavailable</Badge> : null}
        </CardTitle>
        <CardDescription>{action.description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {action.params.map((p) =>
          p.type === "bool" ? (
            <div key={p.name} className="flex items-center gap-2">
              <Switch
                id={`${action.id}-${p.name}`}
                checked={values[p.name] === true}
                onCheckedChange={(v) => setValues((s) => ({ ...s, [p.name]: v }))}
              />
              <Label htmlFor={`${action.id}-${p.name}`}>{p.label}</Label>
            </div>
          ) : (
            <Field key={p.name} label={p.label} required={p.required}>
              {(fp) =>
                p.type === "select" ? (
                  <Select
                    {...fp}
                    value={String(values[p.name] ?? "")}
                    onChange={(e) => setValues((s) => ({ ...s, [p.name]: e.target.value }))}
                  >
                    <option value="">{p.options?.length ? "Choose…" : "Nothing to choose"}</option>
                    {(p.options ?? []).map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </Select>
                ) : p.type === "textarea" ? (
                  <Textarea
                    {...fp}
                    rows={2}
                    maxLength={1800}
                    placeholder={p.placeholder}
                    value={String(values[p.name] ?? "")}
                    onChange={(e) => setValues((s) => ({ ...s, [p.name]: e.target.value }))}
                  />
                ) : (
                  <Input
                    {...fp}
                    maxLength={1800}
                    placeholder={p.placeholder}
                    value={String(values[p.name] ?? "")}
                    onChange={(e) => setValues((s) => ({ ...s, [p.name]: e.target.value }))}
                  />
                )
              }
            </Field>
          )
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            onClick={onRun}
            loading={run.isPending}
            disabled={Boolean(disabledReason) || run.isPending || coolingDown}
            title={disabledReason}
          >
            <Play /> {run.isPending ? "Sending" : channelName ? `Send to #${channelName}` : "Send"}
          </Button>
          {disabledReason && action.available ? (
            <span className="text-xs text-muted-foreground">{disabledReason}</span>
          ) : null}
        </div>
        {requestError ? <Alert variant="danger" title="Request failed">{requestError}</Alert> : null}
        {result ? (
          result.ok ? (
            <Alert
              variant="success"
              title="Sent"
              action={
                result.jump_url ? (
                  <Button asChild size="sm" variant="secondary">
                    <a href={result.jump_url} target="_blank" rel="noreferrer">
                      <ExternalLink /> Open
                    </a>
                  </Button>
                ) : undefined
              }
            >
              {result.message_ids.length} message{result.message_ids.length === 1 ? "" : "s"}
              {result.note ? `. ${result.note}` : ""}
            </Alert>
          ) : (
            <Alert variant="danger" title="Test failed">
              {result.error ?? "Unknown error"}
            </Alert>
          )
        ) : null}
      </CardContent>
    </Card>
  )
}

function FortnitePreviewCard() {
  const preview = useFortnitePreview()
  const { offline, reason } = useBotOffline()
  const data = preview.data
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShoppingBag className="size-4" aria-hidden /> Fortnite shop data
        </CardTitle>
        <CardDescription>
          The parsed shop the bot posts from (fortnite-api.com). "Load" reuses the bot's cached copy when it is fresh.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="secondary"
            onClick={() => preview.mutate(false)}
            loading={preview.isPending && preview.variables === false}
            disabled={preview.isPending || offline}
            title={reason}
          >
            <Database /> Load
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => preview.mutate(true)}
            loading={preview.isPending && preview.variables === true}
            disabled={preview.isPending || offline}
            title={reason}
          >
            <RefreshCw /> Fetch fresh
          </Button>
        </div>
        {preview.isError ? <Alert variant="danger" title="Could not load the shop">{errorText(preview.error)}</Alert> : null}
        {preview.isPending ? <Skeleton className="h-40 w-full" /> : null}
        {data ? (
          <>
            <div className="flex flex-wrap gap-2 text-sm">
              <Badge>Date {data.shop_date}</Badge>
              <Badge variant="outline">Hash {data.hash}</Badge>
              <Badge variant="info">{data.entry_count} offers</Badge>
              <Badge variant="outline">{data.sections.length} sections</Badge>
            </div>
            <pre className="max-h-[480px] overflow-auto rounded-lg border border-border bg-muted p-3 font-mono text-xs leading-5 text-foreground">
              {JSON.stringify(data, null, 2)}
            </pre>
          </>
        ) : !preview.isPending && !preview.isError ? (
          <p className="text-sm text-muted-foreground">Nothing loaded yet.</p>
        ) : null}
      </CardContent>
    </Card>
  )
}
