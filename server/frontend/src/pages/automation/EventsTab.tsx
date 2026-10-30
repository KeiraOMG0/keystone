import { useEffect, useState } from "react"
import { FlaskConical, Pencil, Plus, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { Field } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { useBotOffline } from "@/hooks/useBotOffline"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"
import { describeApiError } from "@/lib/apiError"
import {
  useAutomationOptions,
  useClearEventBinding,
  useEventBindings,
  useSetEventBinding,
  useTestEvent,
  type EventBinding,
} from "@/api/automation"
import { NotAllowed, TabState, channelLabel } from "./shared"

const EVENT_INFO: Record<string, { label: string; hint: string }> = {
  member_join: { label: "Member joins", hint: "When someone joins the server." },
  member_leave: { label: "Member leaves", hint: "When someone leaves (or is kicked)." },
  boost: { label: "Boost", hint: "When someone boosts the server." },
  role_add: { label: "Role added", hint: "When a member gets any role." },
  role_remove: { label: "Role removed", hint: "When a member loses any role." },
}
const ORDER = ["member_join", "member_leave", "boost", "role_add", "role_remove"]

/** /event set|clear|list|test (manage_events). */
export function EventsTab({ allowed }: { allowed: boolean }) {
  const query = useEventBindings(allowed)
  const clear = useClearEventBinding()
  const test = useTestEvent()
  const { offline, reason } = useBotOffline()
  const { toast } = useToast()
  const [editing, setEditing] = useState<{ eventType: string; binding?: EventBinding } | null>(null)
  const [toClear, setToClear] = useState<string | null>(null)
  const [toTest, setToTest] = useState<EventBinding | null>(null)

  if (!allowed) return <NotAllowed permission="manage_events" />

  const byType = new Map((query.data?.bindings ?? []).map((b) => [b.event_type, b]))

  return (
    <TabState query={query} permission="manage_events" what="event bindings">
      <Card className="gap-0 py-0">
        <CardHeader className="py-4">
          <CardTitle>Event messages</CardTitle>
          <CardDescription>
            Send a template to a channel when something happens (same as <span className="font-mono">/event</span>). One
            template per event.
          </CardDescription>
        </CardHeader>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Event</TableHead>
              <TableHead>Channel</TableHead>
              <TableHead>Template</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">
                <span className="sr-only">Actions</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {ORDER.map((type) => {
              const b = byType.get(type)
              const info = EVENT_INFO[type]
              return (
                <TableRow key={type}>
                  <TableCell>
                    <div className="font-medium text-foreground">{info.label}</div>
                    <div className="text-xs text-muted-foreground">{info.hint}</div>
                  </TableCell>
                  <TableCell className={b?.channel_name ? undefined : "text-muted-foreground"}>
                    {b ? channelLabel(b.channel_name, b.channel_id) : "Not set"}
                  </TableCell>
                  <TableCell className="font-mono text-xs">{b ? b.templates.map((t) => t.name).join(", ") || "None" : ""}</TableCell>
                  <TableCell>
                    {!b ? (
                      <Badge variant="outline">Off</Badge>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {b.enabled ? <Badge variant="success" dot>On</Badge> : <Badge variant="outline">Disabled</Badge>}
                        {b.send_dm ? <Badge variant="info">+ DM</Badge> : null}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-1">
                      <Button size="sm" variant="secondary" onClick={() => setEditing({ eventType: type, binding: b })}>
                        {b ? <Pencil /> : <Plus />} {b ? "Edit" : "Set up"}
                      </Button>
                      {b ? (
                        <>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={offline || test.isPending}
                            title={reason ?? "Fire this event with you as the member"}
                            onClick={() => setToTest(b)}
                          >
                            <FlaskConical /> Test
                          </Button>
                          <Button size="sm" variant="ghost" className="text-danger" aria-label={`Clear ${info.label}`} onClick={() => setToClear(type)}>
                            <Trash2 />
                          </Button>
                        </>
                      ) : null}
                    </div>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </Card>

      {editing ? <EventDialog eventType={editing.eventType} binding={editing.binding} onClose={() => setEditing(null)} /> : null}

      <ConfirmDialog
        open={toClear !== null}
        onOpenChange={(o) => !o && setToClear(null)}
        title={`Clear "${toClear ? EVENT_INFO[toClear].label : ""}"?`}
        description="The bot stops posting a message for this event. The template itself is kept."
        confirmLabel="Clear binding"
        confirmIcon={<Trash2 />}
        loading={clear.isPending}
        onConfirm={() =>
          toClear &&
          clear.mutate(toClear, {
            onSuccess: () => toast({ title: "Binding cleared", variant: "success" }),
            onError: (e) => toast({ title: "Could not clear", description: describeApiError(e), variant: "destructive" }),
          })
        }
      />

      <ConfirmDialog
        open={toTest !== null}
        onOpenChange={(o) => !o && setToTest(null)}
        destructive={false}
        title={`Test "${toTest ? EVENT_INFO[toTest.event_type].label : ""}" for real?`}
        description={
          <>
            This posts the template to <strong>{toTest ? channelLabel(toTest.channel_name, toTest.channel_id) : ""}</strong> right
            now, with <strong>you</strong> as the member (it pings you there)
            {toTest?.send_dm ? ", and DMs it to you" : ""}. Everyone who can see that channel will see it. Same as{" "}
            <span className="font-mono">/event test</span>.
          </>
        }
        confirmLabel="Post test message"
        confirmIcon={<FlaskConical />}
        onConfirm={() =>
          toTest &&
          test.mutate(toTest.event_type, {
            onSuccess: (r) => toast({ title: "Test message sent", description: r.channel_name ? `Posted in #${r.channel_name}.` : undefined, variant: "success" }),
            onError: (e) => toast({ title: "Test failed", description: describeApiError(e), variant: "destructive" }),
          })
        }
      />
    </TabState>
  )
}

function EventDialog({ eventType, binding, onClose }: { eventType: string; binding?: EventBinding; onClose: () => void }) {
  const options = useAutomationOptions()
  const save = useSetEventBinding()
  const { toast } = useToast()
  const initial = {
    channel: binding?.channel_id ?? "",
    template: binding?.templates[0]?.id ? String(binding.templates[0].id) : "",
    dm: binding?.send_dm ?? false,
  }
  const [channel, setChannel] = useState(initial.channel)
  const [template, setTemplate] = useState(initial.template)
  const [dm, setDm] = useState(initial.dm)
  const dirty = channel !== initial.channel || template !== initial.template || dm !== initial.dm
  useUnsavedChangesGuard(dirty)
  useEffect(() => save.reset(), []) // eslint-disable-line react-hooks/exhaustive-deps

  const channels = [...(options.data?.channels ?? [])].sort((a, b) => a.position - b.position)
  const templates = options.data?.templates ?? []

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[520px]">
        <DialogHeader>
          <DialogTitle>{EVENT_INFO[eventType].label}</DialogTitle>
          <DialogDescription>{EVENT_INFO[eventType].hint} The template's placeholders are filled with that member.</DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (!channel || !template) return
            save.mutate(
              { eventType, channel_id: channel, template_id: Number(template), send_dm: dm },
              {
                onSuccess: () => {
                  toast({ title: "Event binding saved", variant: "success" })
                  onClose()
                },
                onError: (err) => toast({ title: "Could not save", description: describeApiError(err), variant: "destructive" }),
              }
            )
          }}
        >
          {options.isError ? <p className="text-sm text-danger">{describeApiError(options.error, "Could not load channels and templates.")}</p> : null}
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
          <Field label="Template" required hint="Create and edit templates on the Templates page.">
            {(p) => (
              <Select {...p} value={template} onChange={(e) => setTemplate(e.target.value)} disabled={options.isPending}>
                <option value="">{options.isPending ? "Loading…" : "Pick a template"}</option>
                {templates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                    {t.category ? ` (${t.category})` : ""}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <div className="flex items-center gap-2">
            <Switch id="ev-dm" checked={dm} onCheckedChange={setDm} />
            <Label htmlFor="ev-dm">Also DM the member</Label>
          </div>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!channel || !template} loading={save.isPending}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
