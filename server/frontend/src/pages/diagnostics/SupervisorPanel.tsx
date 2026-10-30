import { useEffect, useState } from "react"
import { Archive, RotateCw, ScrollText, ServerOff, X } from "lucide-react"
import { ApiError } from "@/api/client"
import {
  isSupervisorUnavailable,
  useSupervisorLogs,
  useSupervisorRestart,
  useSupervisorRotateLogs,
  useSupervisorStatus,
  type LogRotationRun,
  type SupervisorStatusWithRotation,
} from "@/api/supervisor"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState, ErrorState } from "@/components/patterns/states"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { useToast } from "@/components/ui/toast"
import { useAuth } from "@/hooks/useAuth"
import { OWNER_ONLY_PERMISSION } from "@/lib/owner"
import { apiErrorMessage } from "@/pages/honeypot/format"
import { fmtBytes, fmtDate, fmtUptime } from "@/pages/diagnostics/fmt"
import {
  clearRestartTimeout,
  RESTART_LABEL as LABEL,
  startRestartWatch,
  useRestartWatchState,
  type PendingRestart,
} from "@/pages/diagnostics/supervisorRestartWatch"
import { cn } from "@/lib/utils"
import type { SupervisorService, SupervisorServiceName, SupervisorTarget } from "@/types/api"

const TITLE: Record<SupervisorServiceName, string> = { bot: "Bot", dashboard: "Dashboard", tunnel: "Cloudflare tunnel" }
const NAMES: Record<SupervisorTarget, SupervisorServiceName[]> = {
  bot: ["bot"],
  dashboard: ["dashboard"],
  tunnel: ["tunnel"],
  all: ["bot", "dashboard", "tunnel"],
}
const WARN = {
  bot: "Restarting the bot drops every music/voice session, and the dashboard shows 502 errors for about 10-20 seconds while the bot starts again.",
  dashboard: "Restarting the dashboard briefly disconnects this page; it reconnects by itself.",
  tunnel: "Restarting the tunnel cuts off remote access (dash.idlesys.xyz) for a few seconds; this page reconnects by itself.",
}

function warningFor(target: SupervisorTarget): string[] {
  return target === "all" ? [WARN.bot, WARN.dashboard, WARN.tunnel] : [WARN[target]]
}

function ago(iso: string | null | undefined): string {
  if (!iso) return ""
  const t = Date.parse(iso)
  return Number.isNaN(t) ? "" : `${fmtUptime(Math.max(0, (Date.now() - t) / 1000))} ago`
}

/** One-line summary of a finished log rotation, for the toast and the owner's status line. */
function rotationSummary(run: LogRotationRun): string {
  const by = (status: string) => run.files.filter((f) => f.status === status)
  const rotated = by("rotated")
  const parts = [
    rotated.length
      ? `Archived ${rotated.map((f) => `${f.file} (${fmtBytes(f.bytes)})`).join(", ")}`
      : "Nothing to archive",
  ]
  const deferred = by("deferred")
  if (deferred.length) parts.push(`${deferred.map((f) => f.file).join(", ")} deferred until the bot's next restart (the bot holds it open)`)
  const failed = [...by("failed"), ...by("compress_failed")]
  if (failed.length) parts.push(`problems: ${failed.map((f) => `${f.file} ${f.status}`).join(", ")}`)
  if (run.deleted.length) parts.push(`deleted ${run.deleted.length} old archive(s)`)
  if (run.error) parts.push(run.error)
  return parts.join("; ") + "."
}

function stateOf(s: SupervisorService): { text: string; dot: string; tone: "success" | "warning" | "destructive" } {
  if (s.running) return { text: "Running", dot: "bg-success", tone: "success" }
  if (s.skip_reason === "port in use") return { text: "Not managed (port held by another process)", dot: "bg-warning", tone: "warning" }
  if (s.waiting_on_dependency) return { text: "Waiting for the bot's internal API", dot: "bg-warning", tone: "warning" }
  return {
    text: s.next_start_in_s ? `Down, starting in ${Math.ceil(s.next_start_in_s)}s` : "Down, starting",
    dot: "bg-danger",
    tone: "destructive",
  }
}

/**
 * Diagnostics -> Services: supervisor.py's view of the bot, dashboard and
 * tunnel, with restart buttons. Talks to /api/supervisor/* (staff), which the
 * dashboard proxies straight to the supervisor, so it works while the bot is
 * down. Restart buttons deliberately do NOT use useBotOffline(): restarting
 * goes through the supervisor, not the bot. The "Restarting..." watch lives in
 * supervisorRestartWatch.ts (polled by DiagnosticsPage), not here, so it
 * survives this panel unmounting (section collapsed, page layout switch).
 *
 * OWNER ONLY: "Rotate logs now" (POST /api/supervisor/rotate-logs, 404 for
 * everyone else), its confirm dialog and the log-rotation status line are not
 * rendered at all unless /api/me says is_owner (the __owner_only
 * pseudo-permission). Like the restart buttons it is disabled only while the
 * supervisor is unreachable, never by useBotOffline().
 */
export function SupervisorPanel({ active }: { active: boolean }) {
  const { toast } = useToast()
  const query = useSupervisorStatus(active)
  const restart = useSupervisorRestart()
  const [confirm, setConfirm] = useState<SupervisorTarget | null>(null)
  const [reason, setReason] = useState("")
  const { pending, timedOut } = useRestartWatchState()
  const [now, setNow] = useState(() => Date.now())
  const [logsFor, setLogsFor] = useState<SupervisorServiceName | null>(null)
  const logs = useSupervisorLogs(logsFor, active && !!logsFor)
  const { permissions } = useAuth()
  const isOwner = Boolean(permissions[OWNER_ONLY_PERMISSION])
  const rotate = useSupervisorRotateLogs()
  const [rotateOpen, setRotateOpen] = useState(false)
  // last_run.at seen before our request; a different, finished run is ours.
  const [rotateWatch, setRotateWatch] = useState<{ since: string | null } | null>(null)

  const data = query.data
  const rotation = (data as SupervisorStatusWithRotation | undefined)?.log_rotation ?? null
  const unavailable = !data && isSupervisorUnavailable(query.error)
  const forbidden = !data && query.error instanceof ApiError && query.error.status === 403

  // Ticks the "Restarting... (Ns)" counter; the watch itself runs elsewhere.
  useEffect(() => {
    if (!pending) return
    setNow(Date.now())
    const tick = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(tick)
  }, [pending])
  const elapsed = pending ? Math.max(0, Math.round((now - pending.startedAt) / 1000)) : 0

  // The supervisor answers 202 "queued"; the result shows up in /status (polled every 5s).
  const lastRun = rotation?.last_run ?? null
  const rotationBusy = !!rotation && (rotation.running || rotation.pending)
  useEffect(() => {
    if (!rotateWatch || !lastRun || rotationBusy) return
    if (lastRun.at === rotateWatch.since || !lastRun.finished_at) return
    setRotateWatch(null)
    toast({
      title: lastRun.ok ? "Logs rotated" : "Log rotation finished with problems",
      description: rotationSummary(lastRun),
      variant: lastRun.ok ? "success" : "destructive",
    })
  }, [rotateWatch, lastRun, rotationBusy, toast])
  // Never leave the button stuck if no result arrives (e.g. the request was dropped).
  useEffect(() => {
    if (!rotateWatch) return
    const t = setTimeout(() => setRotateWatch(null), 120_000)
    return () => clearTimeout(t)
  }, [rotateWatch])

  function requestRotate() {
    const since = lastRun?.at ?? null
    rotate.mutate(
      {},
      {
        onSuccess: () => {
          setRotateWatch({ since })
          toast({ title: "Log rotation started", description: "The result appears here in a few seconds.", variant: "info" })
          void query.refetch()
        },
        onError: (err) => {
          const body = err instanceof ApiError ? (err.body as { error?: string; message?: string; retry_after_s?: number } | null) : null
          const description =
            err instanceof ApiError && err.status === 429
              ? `Logs were rotated moments ago. Try again in ${body?.retry_after_s ?? 60}s.`
              : err instanceof ApiError && err.status === 409
                ? (body?.message ?? "A log rotation is already in progress.")
                : isSupervisorUnavailable(err)
                  ? "Not running under supervisor.py, so nothing was rotated."
                  : apiErrorMessage(err)
          toast({ title: "Logs not rotated", description, variant: "destructive" })
          void query.refetch()
        },
      },
    )
  }

  function request(target: SupervisorTarget, why: string) {
    const oldPids: PendingRestart["oldPids"] = {}
    for (const s of data?.services ?? []) oldPids[s.name] = s.running ? s.pid : null
    clearRestartTimeout()
    restart.mutate(
      { target, reason: why.trim() || null },
      {
        onSuccess: (res) => {
          if (res.accepted.length) {
            startRestartWatch({ target, names: res.accepted, oldPids, startedAt: Date.now() })
          }
          toast({
            title: `Restarting ${res.accepted.join(", ")}`,
            description: res.skipped.length
              ? `Skipped ${res.skipped.map((s) => `${s.name}: ${s.message}`).join("; ")}`
              : `The supervisor relaunches it in about ${Math.ceil(res.eta_s)}s.`,
            variant: "info",
          })
        },
        onError: (err) => {
          const body = err instanceof ApiError ? (err.body as { error?: string; message?: string; retry_after_s?: number } | null) : null
          const description =
            err instanceof ApiError && err.status === 429
              ? `Restarted on request moments ago. Try again in ${body?.retry_after_s ?? 30}s.`
              : err instanceof ApiError && err.status === 409
                ? (body?.message ?? "A restart is already in progress.")
                : isSupervisorUnavailable(err)
                  ? "Not running under supervisor.py, so nothing was restarted."
                  : apiErrorMessage(err)
          toast({ title: "Restart not requested", description, variant: "destructive" })
          void query.refetch()
        },
      },
    )
  }

  const byName = new Map((data?.services ?? []).map((s) => [s.name, s]))
  const cooldown = (t: SupervisorTarget) => Math.max(0, ...NAMES[t].map((n) => byName.get(n)?.cooldown_remaining_s ?? 0))
  const buttonsDisabled = !data || !!pending || restart.isPending

  const buttons = (
    <div className="flex flex-wrap gap-2">
      {(["bot", "dashboard", "tunnel", "all"] as const).map((t) => {
        const cd = cooldown(t)
        return (
          <Button
            key={t}
            variant="destructive-outline"
            size="sm"
            disabled={buttonsDisabled || cd > 0}
            loading={restart.isPending && restart.variables?.target === t}
            onClick={() => {
              setReason("")
              setConfirm(t)
            }}
            title={cd > 0 ? `Cooldown: ${Math.ceil(cd)}s` : undefined}
          >
            <RotateCw /> Restart {LABEL[t]}
            {cd > 0 ? <span className="tabular-nums">({Math.ceil(cd)}s)</span> : null}
          </Button>
        )
      })}
      {isOwner ? (
        <Button
          variant="secondary"
          size="sm"
          disabled={!data || !rotation || rotationBusy || rotate.isPending || !!rotateWatch}
          loading={rotate.isPending || !!rotation?.running || !!rotateWatch}
          onClick={() => setRotateOpen(true)}
          title={data && !rotation ? "Needs supervisor.py v2.1 or newer (restart the supervisor once)" : undefined}
        >
          <Archive /> Rotate logs now
        </Button>
      ) : null}
    </div>
  )

  if (forbidden) {
    return <p className="text-sm text-muted-foreground">Restart controls are restricted to default staff roles or the server owner.</p>
  }

  return (
    <div className="flex flex-col gap-4">
      {buttons}

      {pending ? (
        <Alert variant="info" title={`Restarting ${LABEL[pending.target]}... (${elapsed}s)`}>
          Waiting for {pending.names.join(", ")} to come back.
          {pending.names.some((n) => n !== "bot") ? " This page may lose its connection for a moment; it keeps checking." : ""}
        </Alert>
      ) : null}
      {timedOut ? (
        <Alert variant="danger" title="Restart not confirmed" action={<Button variant="ghost" size="icon-xs" aria-label="Dismiss" onClick={clearRestartTimeout}><X /></Button>}>
          {timedOut}
        </Alert>
      ) : null}

      {!data && query.isPending ? (
        <div className="grid gap-3 md:grid-cols-3" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-36 w-full" />
          ))}
        </div>
      ) : null}

      {unavailable ? (
        <EmptyState
          icon={ServerOff}
          title="Not running under supervisor.py — restart controls unavailable"
          description="Start the stack with supervisor.py (or restart the running supervisor once so it loads the control API)."
          className="py-6"
        />
      ) : null}

      {!data && query.isError && !unavailable ? (
        <ErrorState
          title="Could not load supervisor status"
          message={
            query.error instanceof ApiError && query.error.status === 404
              ? "This dashboard process predates the restart controls. Restart the dashboard once to load /api/supervisor."
              : apiErrorMessage(query.error)
          }
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
          className="py-6"
        />
      ) : null}

      {data ? (
        <>
          <p className="text-xs text-muted-foreground">
            supervisor.py v{data.supervisor.version}, pid <span className="font-mono">{data.supervisor.pid}</span>, up since{" "}
            {fmtDate(data.supervisor.started_at)}. Requested restarts have a {Math.round(data.supervisor.restart_cooldown_s)}s cooldown per service.
            {query.isError ? " (Last refresh failed; showing the previous status.)" : ""}
          </p>
          {isOwner && rotation ? (
            <p className="text-xs text-muted-foreground">
              Log rotation: {rotation.schedule}; archives in <span className="font-mono">{rotation.archive_dir}</span>, kept{" "}
              {rotation.retention_days} days. Next: {rotation.overdue ? "now (overdue)" : fmtDate(rotation.next_run_at)}.{" "}
              {rotation.last_run
                ? `Last: ${ago(rotation.last_run.at)} (${rotation.last_run.trigger}${rotation.last_run.requested_by ? ` by ${rotation.last_run.requested_by}` : ""}): ${rotationSummary(rotation.last_run)}`
                : "No rotation has run yet."}
              {rotation.deferred.length ? ` Waiting for the bot's next restart: ${rotation.deferred.join(", ")}.` : ""}
            </p>
          ) : null}
          <ul className="grid gap-3 md:grid-cols-3">
            {data.services.map((s) => {
              const st = stateOf(s)
              return (
                <li key={s.name} className="flex flex-col gap-2 rounded-lg border border-border p-3 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-2 font-medium text-foreground">
                      <span aria-hidden className={cn("size-2 rounded-full", st.dot)} />
                      {TITLE[s.name] ?? s.name}
                    </span>
                    <Badge variant={st.tone}>{s.running ? "Running" : "Down"}</Badge>
                  </div>
                  <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                    <dt className="text-muted-foreground">State</dt>
                    <dd className="min-w-0 break-words">{st.text}{s.restart_pending ? " (restart in progress)" : ""}</dd>
                    <dt className="text-muted-foreground">PID</dt>
                    <dd className="font-mono tabular-nums">{s.pid ?? "n/a"}</dd>
                    <dt className="text-muted-foreground">Uptime</dt>
                    <dd className="tabular-nums">{s.running ? fmtUptime(s.uptime_s) : "n/a"}</dd>
                    <dt className="text-muted-foreground">Restarts</dt>
                    <dd className="tabular-nums">{s.restarts}{s.health_fails ? `, ${s.health_fails} failed health check(s)` : ""}</dd>
                    <dt className="text-muted-foreground">Last exit</dt>
                    <dd className="min-w-0 break-words">{s.last_exit ? `${s.last_exit.reason} (${ago(s.last_exit.at)})` : "none"}</dd>
                    <dt className="text-muted-foreground">Last request</dt>
                    <dd className="min-w-0 break-words">
                      {s.last_request
                        ? `${s.last_request.by} via ${s.last_request.source}, ${ago(s.last_request.at)}${s.last_request.reason ? `: ${s.last_request.reason}` : ""}`
                        : "none"}
                    </dd>
                    {s.cooldown_remaining_s > 0 ? (
                      <>
                        <dt className="text-muted-foreground">Cooldown</dt>
                        <dd className="tabular-nums">{Math.ceil(s.cooldown_remaining_s)}s</dd>
                      </>
                    ) : null}
                  </dl>
                  <Button
                    variant="ghost"
                    size="xs"
                    className="self-start"
                    aria-expanded={logsFor === s.name}
                    onClick={() => setLogsFor((cur) => (cur === s.name ? null : s.name))}
                  >
                    <ScrollText /> {logsFor === s.name ? "Hide output" : "Recent output"}
                  </Button>
                </li>
              )
            })}
          </ul>
          {logsFor ? (
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between gap-2">
                <p className="text-sm font-medium">Recent output: {TITLE[logsFor]}</p>
                <span className="text-xs text-muted-foreground">Last 120 lines (the supervisor keeps 200), refreshed every 5s. Secrets, appeal tokens, OAuth codes and client IPs are scrubbed.</span>
              </div>
              {logs.isPending ? (
                <Skeleton className="h-40 w-full" />
              ) : logs.isError ? (
                <p className="text-sm text-danger">Could not load output: {apiErrorMessage(logs.error)}</p>
              ) : (
                <pre className="max-h-80 overflow-auto rounded-lg border border-border bg-muted p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words">
                  {logs.data?.lines.length ? logs.data.lines.join("\n") : "(no output captured yet)"}
                </pre>
              )}
            </div>
          ) : null}
        </>
      ) : null}

      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(o) => {
          if (!o) setConfirm(null)
        }}
        title={confirm ? `Restart ${LABEL[confirm]}?` : ""}
        description={
          confirm ? (
            <span className="flex flex-col gap-1.5">
              {warningFor(confirm).map((w) => (
                <span key={w}>{w}</span>
              ))}
            </span>
          ) : (
            ""
          )
        }
        confirmLabel={confirm ? `Restart ${LABEL[confirm]}` : "Restart"}
        confirmIcon={<RotateCw />}
        onConfirm={() => {
          if (confirm) request(confirm, reason)
        }}
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="restart-reason">Reason (optional)</Label>
          <Textarea
            id="restart-reason"
            value={reason}
            maxLength={300}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Shown in the supervisor log and the Discord alert"
            className="min-h-16"
          />
        </div>
      </ConfirmDialog>

      {isOwner ? (
        <ConfirmDialog
          open={rotateOpen}
          onOpenChange={setRotateOpen}
          title="Rotate logs now?"
          description={
            <span className="flex flex-col gap-1.5">
              <span>
                Archives the current bot, dashboard, tunnel and supervisor logs and bot_startup.log to gzip files in{" "}
                {rotation?.archive_dir ?? "logs/archive"}, starts fresh empty files, and deletes archives older than 4 weeks.
                No service is restarted and no log line is lost.
              </span>
              <span>If the bot was started outside supervisor.py it holds bot_startup.log open, and that file is archived at the bot's next restart instead.</span>
              <span>kicks.log (the kick counter) and pot_server.log are never touched.</span>
            </span>
          }
          confirmLabel="Rotate logs"
          confirmIcon={<Archive />}
          destructive={false}
          onConfirm={requestRotate}
        />
      ) : null}
    </div>
  )
}
