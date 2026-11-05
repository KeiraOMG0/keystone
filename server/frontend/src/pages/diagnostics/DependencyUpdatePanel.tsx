import { useEffect, useState } from "react"
import { CheckCircle2, PackageCheck, PackageSearch, XCircle } from "lucide-react"
import { ApiError } from "@/api/client"
import {
  isSupervisorUnavailable,
  useSupervisorUpdateDeps,
  type DependencyPlan,
  type DependencyRun,
  type DependencyUpdateStatus,
} from "@/api/supervisor"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"
import { apiErrorMessage } from "@/pages/honeypot/format"
import { fmtUptime } from "@/pages/diagnostics/fmt"

/** No result after this long: stop waiting (the request was dropped or the supervisor restarted). */
const WATCH_TIMEOUT_MS = 30 * 60_000

function ago(iso: string | null | undefined): string {
  if (!iso) return ""
  const t = Date.parse(iso)
  return Number.isNaN(t) ? "" : `${fmtUptime(Math.max(0, (Date.now() - t) / 1000))} ago`
}

function runSummary(run: DependencyRun): string {
  if (run.error) return run.error
  if (run.mode === "check") {
    const n = planCount(run.plan)
    return n ? `${n} update(s) can be applied automatically.` : "Nothing to update automatically."
  }
  return run.changes.join(" ") || (run.ok ? "Done." : "Finished with problems.")
}

function planCount(plan: DependencyPlan | null): number {
  if (!plan) return 0
  return plan.python.length + Object.values(plan.npm).reduce((n, p) => n + (p.fixable.length ? 1 : 0), 0)
}

function PlanView({ plan }: { plan: DependencyPlan }) {
  const npm = Object.entries(plan.npm).filter(([, p]) => p.fixable.length)
  if (!plan.python.length && !npm.length) return <p className="text-xs text-muted-foreground">Nothing to update automatically.</p>
  const needsStop = plan.python.length > 0 || !!plan.npm.pot_server?.fixable.length
  return (
    <div className="flex flex-col gap-1.5 text-xs">
      <ul className="flex flex-col gap-1">
        {plan.python.map((u) => (
          <li key={u.package}>
            <span className="font-mono">{u.package}</span> {u.from ?? "?"} → {u.to}{" "}
            <span className="text-muted-foreground">(Python; {u.why})</span>
          </li>
        ))}
        {npm.map(([key, p]) => (
          <li key={key}>
            {p.label}: <span className="font-mono">npm audit fix</span> for {p.fixable.join(", ")}
          </li>
        ))}
      </ul>
      <p className="text-muted-foreground">
        {needsStop
          ? "Applying this stops the bot and the dashboard for a few minutes (Python packages and the PO-token server can't be replaced while they run)."
          : "Applying this rebuilds the dashboard frontend only; nothing is restarted."}
      </p>
    </div>
  )
}

function RunView({ run }: { run: DependencyRun }) {
  const failed = run.steps.filter((s) => !s.ok).length
  return (
    <div className="flex flex-col gap-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{run.mode === "apply" ? "Last update" : "Last check"}</span>
        <Badge variant={run.ok ? "success" : "destructive"}>{run.ok ? "OK" : "Problems"}</Badge>
        <span className="text-xs text-muted-foreground">
          {ago(run.finished_at ?? run.at)}
          {run.requested_by ? ` by ${run.requested_by}` : ""}
          {run.source ? ` via ${run.source}` : ""}
          {run.reason ? `: ${run.reason}` : ""}
        </span>
      </div>
      {run.error ? <p className="text-xs text-danger">{run.error}</p> : null}
      {run.mode === "check" && run.plan ? <PlanView plan={run.plan} /> : null}
      {run.mode === "apply" && run.changes.length ? (
        <ul className="flex list-disc flex-col gap-1 pl-5 text-xs">
          {run.changes.map((c) => (
            <li key={c} className="break-words">{c}</li>
          ))}
        </ul>
      ) : null}
      {run.mode === "apply" && run.restarted.length ? (
        <p className="text-xs text-muted-foreground">Stopped for the update and started again: {run.restarted.join(", ")}.</p>
      ) : null}
      {run.manual.length ? (
        <div className="flex flex-col gap-1 text-xs">
          <p className="font-medium">Needs a manual update (major version or no fix yet)</p>
          <ul className="flex list-disc flex-col gap-0.5 pl-5 text-muted-foreground">
            {run.manual.map((m) => (
              <li key={m} className="break-words">{m}</li>
            ))}
          </ul>
        </div>
      ) : null}
      <details className="text-xs">
        <summary className="cursor-pointer text-muted-foreground select-none">
          Steps ({run.steps.length}{failed ? `, ${failed} failed` : ""})
        </summary>
        <ul className="mt-2 flex flex-col gap-1.5">
          {run.steps.map((s, i) => (
            <li key={`${s.name}-${i}`} className="flex flex-col gap-1">
              <span className="flex items-start gap-1.5">
                {s.ok ? (
                  <CheckCircle2 aria-label="ok" className="mt-0.5 size-3.5 shrink-0 text-success" />
                ) : (
                  <XCircle aria-label="failed" className="mt-0.5 size-3.5 shrink-0 text-danger" />
                )}
                <span className="min-w-0 break-words">
                  <span className="font-medium">{s.name}</span>: {s.detail}{" "}
                  <span className="text-muted-foreground tabular-nums">({s.seconds}s)</span>
                </span>
              </span>
              {s.output?.length ? (
                <details className="ml-5">
                  <summary className="cursor-pointer text-muted-foreground select-none">Output (last {s.output.length} lines)</summary>
                  <pre className="mt-1 max-h-60 overflow-auto rounded-md border border-border bg-muted p-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-words">
                    {s.output.join("\n")}
                  </pre>
                </details>
              ) : null}
            </li>
          ))}
        </ul>
      </details>
    </div>
  )
}

/**
 * Diagnostics -> Services, OWNER ONLY (rendered by SupervisorPanel only when
 * /api/me says is_owner; the API answers 404 to everyone else and GET /status
 * leaves `dependency_update` out for them). Drives supervisor.py's
 * DependencyUpdater: "Check" audits and shows the plan; "Update now" applies
 * the safe fixes (frontend rebuilt in place; Python / PO-token server updates
 * stop the bot and this dashboard, run the tests, roll back on failure, then
 * start both again). Disabled only while the supervisor is unreachable or a
 * run is in progress, never by useBotOffline() (it goes through the supervisor).
 */
export function DependencyUpdatePanel({
  status,
  reachable,
  onRefetch,
}: {
  /** undefined: the supervisor predates v2.2 (no `dependency_update` in /status). */
  status: DependencyUpdateStatus | null | undefined
  reachable: boolean
  onRefetch: () => void
}) {
  const { toast } = useToast()
  const update = useSupervisorUpdateDeps()
  const [confirmOpen, setConfirmOpen] = useState(false)
  // last_run.at seen before our request; a different, finished run is ours.
  const [watch, setWatch] = useState<{ since: string | null; mode: "check" | "apply" } | null>(null)
  const [now, setNow] = useState(() => Date.now())

  const last = status?.last_run ?? null
  const running = !!status && (status.running || status.pending)
  const busy = running || update.isPending || !!watch
  const startedAt = status?.current?.at ? Date.parse(status.current.at) : null

  useEffect(() => {
    if (!running) return
    setNow(Date.now())
    const tick = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(tick)
  }, [running])

  useEffect(() => {
    if (!watch || !last || running) return
    if (last.at === watch.since || !last.finished_at) return
    setWatch(null)
    toast({
      title:
        last.mode === "check"
          ? "Dependency check finished"
          : last.ok
            ? "Dependencies updated"
            : "Dependency update had problems",
      description: runSummary(last),
      variant: last.ok ? (last.mode === "check" ? "info" : "success") : "destructive",
    })
  }, [watch, last, running, toast])

  useEffect(() => {
    if (!watch) return
    const t = setTimeout(() => setWatch(null), WATCH_TIMEOUT_MS)
    return () => clearTimeout(t)
  }, [watch])

  function start(mode: "check" | "apply") {
    const since = last?.at ?? null
    update.mutate(
      { mode },
      {
        onSuccess: () => {
          setWatch({ since, mode })
          toast({
            title: mode === "check" ? "Checking dependencies" : "Dependency update started",
            description:
              mode === "check"
                ? "Runs pip-audit and npm audit; the result appears here in a minute or so."
                : "Progress shows here. If Python packages change, this page loses its connection for a few minutes.",
            variant: "info",
          })
          onRefetch()
        },
        onError: (err) => {
          const body = err instanceof ApiError ? (err.body as { message?: string; retry_after_s?: number } | null) : null
          const description =
            err instanceof ApiError && err.status === 429
              ? `Requested moments ago. Try again in ${body?.retry_after_s ?? 60}s.`
              : err instanceof ApiError && err.status === 409
                ? (body?.message ?? "A dependency update is already running.")
                : isSupervisorUnavailable(err)
                  ? "Not running under supervisor.py."
                  : apiErrorMessage(err)
          toast({ title: "Not started", description, variant: "destructive" })
          onRefetch()
        },
      },
    )
  }

  const tooOld = reachable && status === undefined
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border p-3" aria-labelledby="dep-update-title">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p id="dep-update-title" className="text-sm font-medium">
            Dependency updates
          </p>
          <p className="text-xs text-muted-foreground">
            Only you see this. Applies the fixes the weekly audit reports when no major version is involved, checks them
            (type-check and build, pip check, the test suite) and rolls back if anything fails.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="secondary"
            size="sm"
            disabled={!reachable || tooOld || busy}
            loading={update.isPending && update.variables?.mode === "check"}
            onClick={() => start("check")}
            title={tooOld ? "Needs supervisor.py v2.2 or newer (restart the supervisor once)" : undefined}
          >
            <PackageSearch /> Check
          </Button>
          <Button
            size="sm"
            disabled={!reachable || tooOld || busy}
            loading={(update.isPending && update.variables?.mode === "apply") || (running && status?.current?.mode === "apply")}
            onClick={() => setConfirmOpen(true)}
            title={tooOld ? "Needs supervisor.py v2.2 or newer (restart the supervisor once)" : undefined}
          >
            <PackageCheck /> Update now
          </Button>
        </div>
      </div>

      {tooOld ? (
        <p className="text-xs text-muted-foreground">The running supervisor predates dependency updates. Restart the supervisor once to enable them.</p>
      ) : null}

      {!reachable && (running || watch) ? (
        <Alert variant="info" title="Waiting for the dashboard to come back...">
          The update stops the dashboard while it replaces Python packages and runs the tests. The result shows here when it is back.
        </Alert>
      ) : running ? (
        <Alert
          variant="info"
          title={`${status?.current?.mode === "apply" ? "Updating" : "Checking"}...${startedAt ? ` (${fmtUptime(Math.max(0, (now - startedAt) / 1000))})` : ""}`}
        >
          {status?.step ? `${status.step.charAt(0).toUpperCase()}${status.step.slice(1)}.` : "Starting."}
          {status?.held.length
            ? ` The ${status.held.join(" and ")} ${status.held.length > 1 ? "are" : "is"} stopped until it finishes. This page loses its connection meanwhile and shows the result once the dashboard is back.`
            : ""}
        </Alert>
      ) : null}

      {last ? <RunView run={last} /> : status && !running ? <p className="text-xs text-muted-foreground">No check has run yet.</p> : null}

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Update dependencies now?"
        description={
          <span className="flex flex-col gap-1.5">
            <span>Runs pip-audit and npm audit, then applies only fixes that need no major version change:</span>
            <span>
              Dashboard frontend: npm audit fix, type-check and build, then the new build replaces the old one. Nothing restarts;
              reload open dashboard tabs afterwards.
            </span>
            <span>
              Python packages (and an outdated yt-dlp) or the PO-token server: the bot AND the dashboard are stopped (music and
              voice sessions drop, this page disconnects), the fixed versions are installed, then pip check and the full test
              suite run. If anything fails, the previous versions are put back. New versions are pinned in requirements.txt.
              Both start again afterwards; this usually takes 2 to 5 minutes.
            </span>
            {last?.mode === "check" && last.plan ? (
              <span>Last check ({ago(last.finished_at ?? last.at)}): {runSummary(last)}</span>
            ) : (
              <span>Tip: run Check first to see what would change.</span>
            )}
          </span>
        }
        confirmLabel="Update now"
        confirmIcon={<PackageCheck />}
        onConfirm={() => start("apply")}
      />
    </section>
  )
}
