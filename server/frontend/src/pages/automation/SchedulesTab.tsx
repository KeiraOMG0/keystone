import { useEffect, useState } from "react"
import { CalendarClock, Plus, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Field } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState } from "@/components/patterns/states"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"
import { describeApiError } from "@/lib/apiError"
import {
  useAutomationOptions,
  useCreateSchedule,
  usePreviewCron,
  useRemoveSchedule,
  useSchedules,
  useToggleSchedule,
  type CronPreview,
  type Schedule,
} from "@/api/automation"
import { NotAllowed, TabState, UtcTime, channelLabel } from "./shared"

/** /schedule add|remove|toggle|list (manage_schedules). Cron is UTC and validated by the bot. */
export function SchedulesTab({ allowed }: { allowed: boolean }) {
  const query = useSchedules(allowed)
  const toggle = useToggleSchedule()
  const remove = useRemoveSchedule()
  const { toast } = useToast()
  const [creating, setCreating] = useState(false)
  const [toRemove, setToRemove] = useState<Schedule | null>(null)

  if (!allowed) return <NotAllowed permission="manage_schedules" />
  const schedules = query.data?.schedules ?? []

  return (
    <TabState query={query} permission="manage_schedules" what="schedules">
      <Card className="gap-0 py-0">
        <CardHeader className="py-4">
          <CardTitle>Scheduled sends</CardTitle>
          <CardDescription>
            Post a template on a cron schedule, in UTC (same as <span className="font-mono">/schedule</span>).
          </CardDescription>
          <div className="col-start-2 row-span-2 row-start-1 self-start justify-self-end">
            <Button size="sm" onClick={() => setCreating(true)}>
              <Plus /> New schedule
            </Button>
          </div>
        </CardHeader>
        {schedules.length === 0 ? (
          <EmptyState icon={CalendarClock} title="No schedules" description="Create one to post a template automatically." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Schedule</TableHead>
                <TableHead>Template</TableHead>
                <TableHead>Channel</TableHead>
                <TableHead>Next runs (UTC)</TableHead>
                <TableHead>On</TableHead>
                <TableHead className="text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {schedules.map((s) => (
                <TableRow key={s.id}>
                  <TableCell>
                    <div className="font-medium text-foreground">{s.label || `Schedule #${s.id}`}</div>
                    <div className="font-mono text-xs text-muted-foreground">{s.cron_expr}</div>
                  </TableCell>
                  <TableCell className="font-mono text-xs">{s.template_name ?? "?"}</TableCell>
                  <TableCell>{channelLabel(s.channel_name, s.channel_id)}</TableCell>
                  <TableCell className="text-xs">
                    {!s.enabled ? (
                      <span className="text-muted-foreground">Paused</span>
                    ) : s.next_run_at ? (
                      <div className="flex flex-col gap-0.5">
                        <UtcTime iso={s.next_run_at} />
                        {s.upcoming.slice(1, 3).map((r) => (
                          <span key={r} className="text-muted-foreground">
                            <UtcTime iso={r} />
                          </span>
                        ))}
                      </div>
                    ) : (
                      "—"
                    )}
                  </TableCell>
                  <TableCell>
                    <Switch
                      checked={s.enabled}
                      aria-label={`Enable ${s.label || `schedule ${s.id}`}`}
                      disabled={toggle.isPending}
                      onCheckedChange={(v) =>
                        toggle.mutate(
                          { id: s.id, enabled: v },
                          { onError: (e) => toast({ title: "Could not change", description: describeApiError(e), variant: "destructive" }) }
                        )
                      }
                    />
                  </TableCell>
                  <TableCell className="text-right">
                    <Button size="sm" variant="ghost" className="text-danger" aria-label="Remove schedule" onClick={() => setToRemove(s)}>
                      <Trash2 />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>
      {schedules.some((s) => !s.enabled) ? (
        <p className="mt-2 text-xs text-muted-foreground">
          Turning a paused schedule back on keeps its stored next run time: if that time has already passed, it posts on the next
          scheduler tick (within 30 s), then continues on schedule.
        </p>
      ) : null}

      {creating ? <CreateScheduleDialog onClose={() => setCreating(false)} /> : null}

      <ConfirmDialog
        open={toRemove !== null}
        onOpenChange={(o) => !o && setToRemove(null)}
        title={`Remove "${toRemove?.label || `schedule #${toRemove?.id ?? ""}`}"?`}
        description="It stops posting. The template is kept."
        confirmLabel="Remove schedule"
        confirmIcon={<Trash2 />}
        onConfirm={() =>
          toRemove &&
          remove.mutate(toRemove.id, {
            onSuccess: () => toast({ title: "Schedule removed", variant: "success" }),
            onError: (e) => toast({ title: "Could not remove", description: describeApiError(e), variant: "destructive" }),
          })
        }
      />
    </TabState>
  )
}

function CreateScheduleDialog({ onClose }: { onClose: () => void }) {
  const options = useAutomationOptions()
  const create = useCreateSchedule()
  const preview = usePreviewCron()
  const { toast } = useToast()
  const [template, setTemplate] = useState("")
  const [channel, setChannel] = useState("")
  const [cron, setCron] = useState("0 9 * * 1")
  const [label, setLabel] = useState("")
  const [result, setResult] = useState<CronPreview | null>(null)
  const dirty = !!template || !!channel || !!label || cron !== "0 9 * * 1"
  useUnsavedChangesGuard(dirty)

  // Debounced bot-side validation (the scheduler's own parser).
  const { mutate } = preview
  useEffect(() => {
    const value = cron.trim()
    if (!value) {
      setResult(null)
      return
    }
    const t = setTimeout(() => mutate(value, { onSuccess: setResult, onError: () => setResult(null) }), 350)
    return () => clearTimeout(t)
  }, [cron, mutate])

  const channels = [...(options.data?.channels ?? [])].sort((a, b) => a.position - b.position)
  const valid = result?.valid === true

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[560px]">
        <DialogHeader>
          <DialogTitle>New schedule</DialogTitle>
          <DialogDescription>The template is posted to the channel every time the cron expression matches (UTC).</DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (!template || !channel || !valid) return
            create.mutate(
              { template_id: Number(template), channel_id: channel, cron: cron.trim(), label: label.trim() || null },
              {
                onSuccess: () => {
                  toast({ title: "Schedule created", variant: "success" })
                  onClose()
                },
                onError: (err) => toast({ title: "Could not create", description: describeApiError(err), variant: "destructive" }),
              }
            )
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Template" required>
              {(p) => (
                <Select {...p} value={template} onChange={(e) => setTemplate(e.target.value)} disabled={options.isPending}>
                  <option value="">{options.isPending ? "Loading…" : "Pick a template"}</option>
                  {(options.data?.templates ?? []).map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Channel" required>
              {(p) => (
                <Select {...p} value={channel} onChange={(e) => setChannel(e.target.value)} disabled={options.isPending}>
                  <option value="">{options.isPending ? "Loading…" : "Pick a channel"}</option>
                  {channels.map((c) => (
                    <option key={c.id} value={c.id}>
                      #{c.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>
          <Field
            label="Cron expression (UTC)"
            required
            hint="minute hour day-of-month month day-of-week, e.g. 0 9 * * 1 = Mondays 09:00 UTC, 30 18 * * * = daily 18:30 UTC."
            error={result && !result.valid ? result.error : null}
          >
            {(p) => <Input {...p} className="font-mono" value={cron} maxLength={100} onChange={(e) => setCron(e.target.value)} />}
          </Field>
          {valid ? (
            <div className="rounded-lg border border-border p-3 text-sm">
              <p className="mb-1 font-medium text-foreground">Next 5 runs</p>
              <ul className="flex flex-col gap-0.5 text-muted-foreground">
                {result.next_runs.map((r) => (
                  <li key={r}>
                    <UtcTime iso={r} />
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          <Field label="Label" hint="Optional, to recognise it in the list.">
            {(p) => <Input {...p} value={label} maxLength={100} onChange={(e) => setLabel(e.target.value)} />}
          </Field>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!template || !channel || !valid} loading={create.isPending}>
              Create schedule
            </Button>
          </DialogFooter>
        </form>
        {!valid && result === null && cron.trim() ? <Badge variant="outline">Checking…</Badge> : null}
      </DialogContent>
    </Dialog>
  )
}
