import { useMemo, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { Bot, ChevronDown, ChevronRight, FlaskConical, Globe, RefreshCw, Server, Trash2 } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert } from "@/components/ui/alert"
import { Select } from "@/components/ui/select"
import { EmptyState, ErrorState, ListSkeleton } from "@/components/patterns/states"
import { describeApiError } from "@/lib/apiError"
import { formatDateTime, formatRelativeTime } from "@/lib/format"
import { reportClientError } from "@/lib/errorReport"
import { useClearDebugErrors, useDebugErrors, type ErrorGroup, type ErrorSource } from "@/api/debugErrors"

/**
 * Debug > Errors (owner only; also reachable as /admin). Every error the site
 * and bot have seen, grouped (same error = one row with a count):
 *  - Browser: crashes/uncaught errors visitors' browsers reported (unverified:
 *    anyone can post one), stored 30 days.
 *  - Website: unhandled exceptions in the dashboard backend, stored 30 days.
 *  - Bot: ERROR log records of the bot process since it last started (memory).
 * Refreshes every 30 s.
 */

const SOURCE_LABEL: Record<ErrorSource, string> = { browser: "Browser", server: "Website", bot: "Bot" }
const SOURCE_ICON = { browser: Globe, server: Server, bot: Bot } as const

type Filter = "all" | ErrorSource

export function ErrorsTab({ enabled }: { enabled: boolean }) {
  const query = useDebugErrors(enabled)
  const clear = useClearDebugErrors()
  const qc = useQueryClient()
  const [filter, setFilter] = useState<Filter>("all")
  const [confirmClear, setConfirmClear] = useState(false)
  const [testSent, setTestSent] = useState(false)

  const groups = useMemo(
    () => (query.data?.groups ?? []).filter((g) => filter === "all" || g.source === filter),
    [query.data, filter]
  )
  const counts = useMemo(() => {
    const out: Record<ErrorSource, number> = { browser: 0, server: 0, bot: 0 }
    for (const g of query.data?.groups ?? []) out[g.source] += g.count
    return out
  }, [query.data])

  const sendTest = () => {
    const sent = reportClientError("test", new Error(`Test error from the Errors tab (${new Date().toISOString()})`))
    setTestSent(sent)
    window.setTimeout(() => qc.invalidateQueries({ queryKey: ["debug", "errors"] }), 1000)
  }

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle>Errors</CardTitle>
          <CardDescription>
            Crashes reported by visitors&apos; browsers, website server errors and bot errors. The same error is grouped
            into one row. Browser and website errors are kept {query.data?.retention_days ?? 30} days; bot errors are
            kept since the bot last started. Updates every 30 seconds.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <Select
              aria-label="Show"
              value={filter}
              onChange={(e) => setFilter(e.target.value as Filter)}
              className="w-auto"
            >
              <option value="all">All sources</option>
              <option value="browser">Browser ({counts.browser})</option>
              <option value="server">Website ({counts.server})</option>
              <option value="bot">Bot ({counts.bot})</option>
            </Select>
            <Button size="sm" variant="outline" onClick={() => query.refetch()} loading={query.isFetching}>
              <RefreshCw /> Refresh
            </Button>
            <Button size="sm" variant="outline" onClick={sendTest}>
              <FlaskConical /> Send test error
            </Button>
            {confirmClear ? (
              <>
                <Button
                  size="sm"
                  variant="destructive"
                  loading={clear.isPending}
                  onClick={() => clear.mutate({ source: filter }, { onSettled: () => setConfirmClear(false) })}
                >
                  <Trash2 /> Confirm clear {filter === "all" ? "everything" : SOURCE_LABEL[filter].toLowerCase()}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirmClear(false)}>
                  Cancel
                </Button>
              </>
            ) : (
              <Button size="sm" variant="ghost" onClick={() => setConfirmClear(true)} disabled={!groups.length}>
                <Trash2 /> Clear
              </Button>
            )}
          </div>
          {testSent ? (
            <p className="text-xs text-muted-foreground">
              Test error sent as a Browser report. It should appear below within a second.
            </p>
          ) : null}
          {clear.isError ? <Alert variant="danger" title="Could not clear">{describeApiError(clear.error)}</Alert> : null}
          {clear.data?.bot_error ? (
            <Alert variant="warning" title="Bot errors not cleared">The bot is not answering right now.</Alert>
          ) : null}
          {query.data && !query.data.bot.available ? (
            <Alert variant="warning" title="Bot errors unavailable">
              The bot is not answering, so its errors can&apos;t be shown. Browser and website errors are still listed.
            </Alert>
          ) : null}
          {query.data?.bot.available && query.data.bot.started_at ? (
            <p className="text-xs text-muted-foreground">Bot errors since {formatDateTime(query.data.bot.started_at)}.</p>
          ) : null}
        </CardContent>
      </Card>

      {query.isPending ? (
        <ListSkeleton rows={4} />
      ) : query.isError ? (
        <ErrorState title="Couldn't load errors" message={describeApiError(query.error)} onRetry={() => query.refetch()} />
      ) : groups.length === 0 ? (
        <EmptyState icon={Globe} title="No errors recorded" description="Nothing has gone wrong that we know of." />
      ) : (
        <div className="flex flex-col gap-3">
          {groups.map((g) => (
            <ErrorRow
              key={`${g.source}:${g.id}`}
              group={g}
              dismissing={clear.isPending && clear.variables?.id === g.id}
              onDismiss={() => clear.mutate({ source: g.source, id: g.id })}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function ErrorRow({ group: g, onDismiss, dismissing }: { group: ErrorGroup; onDismiss: () => void; dismissing: boolean }) {
  const [open, setOpen] = useState(false)
  const Icon = SOURCE_ICON[g.source]
  return (
    <Card>
      <CardContent className="flex flex-col gap-3 p-4">
        <div className="flex items-start gap-3">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="mt-0.5 shrink-0 text-muted-foreground"
            aria-label={open ? "Hide details" : "Show details"}
            aria-expanded={open}
          >
            {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          </button>
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={g.source === "bot" ? "info" : g.source === "server" ? "warning" : "accent"}>
                <Icon /> {SOURCE_LABEL[g.source]}
              </Badge>
              <Badge variant="outline">{g.kind}</Badge>
              {g.count > 1 ? <Badge variant="destructive">×{g.count}</Badge> : null}
              <span className="text-xs text-muted-foreground" title={formatDateTime(g.last_seen)}>
                {formatRelativeTime(g.last_seen)}
              </span>
            </div>
            <button type="button" onClick={() => setOpen((v) => !v)} className="text-left">
              <span className="font-mono text-sm font-medium break-words">
                {g.name}
                {g.message ? `: ${g.message}` : ""}
              </span>
            </button>
            <p className="text-xs text-muted-foreground break-words">
              {g.sample.path ? `${g.source === "bot" ? "At" : "Page"}: ${g.sample.path}` : null}
              {g.users.length ? ` · Signed in: ${g.users.map((u) => u.username ?? u.id).join(", ")}` : null}
            </p>
          </div>
          <Button size="sm" variant="ghost" onClick={onDismiss} loading={dismissing} aria-label="Dismiss">
            <Trash2 />
          </Button>
        </div>
        {open ? <ErrorDetails group={g} /> : null}
      </CardContent>
    </Card>
  )
}

function ErrorDetails({ group: g }: { group: ErrorGroup }) {
  const rows: [string, string][] = [
    ["First seen", formatDateTime(g.first_seen)],
    ["Last seen", formatDateTime(g.last_seen)],
  ]
  if (g.sample.logger) rows.push(["Logger", g.sample.logger])
  if (g.paths.length > 1) rows.push(["Pages", g.paths.join("\n")])
  if (g.users.length) rows.push(["Users", g.users.map((u) => `${u.username ?? "?"} (${u.id})`).join("\n")])
  if (g.user_agents.length) rows.push(["Browsers", g.user_agents.join("\n")])
  return (
    <div className="flex flex-col gap-3 pl-7">
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-xs">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="whitespace-pre-wrap break-words">{v}</dd>
          </div>
        ))}
      </dl>
      {g.sample.stack ? <Pre label="Stack trace" text={g.sample.stack} /> : null}
      {g.sample.component_stack ? <Pre label="Component stack" text={g.sample.component_stack.trim()} /> : null}
      {g.source === "browser" ? (
        <p className="text-xs text-muted-foreground">
          Browser reports come from visitors&apos; browsers and are not verified.
        </p>
      ) : null}
    </div>
  )
}

function Pre({ label, text }: { label: string; text: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 font-mono text-xs">
        {text}
      </pre>
    </div>
  )
}
