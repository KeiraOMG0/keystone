import { useState } from "react"
import { AlertTriangle, Plus, Trash2, UserPlus } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Field } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState } from "@/components/patterns/states"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"
import { useRolesLive } from "@/api/rolesLive"
import { describeApiError } from "@/lib/apiError"
import { useAutoroleRoles, useAutoroles, useCreateAutorole, useRemoveAutorole, type AutoroleRule } from "@/api/automation"
import { NotAllowed, TabState, formatDelay } from "./shared"

const TRIGGERS: Record<string, string> = {
  join: "Member joins",
  boost: "Member boosts",
  role_add: "Member gets a role",
}

/**
 * /autorole add|remove|list (manage_autorole). Roles go through the bot's
 * self-role policy with YOU as the configuring member: staff roles, roles
 * holding a bot permission grant, roles with moderator permissions and roles
 * above you or the bot are refused (and greyed out in the picker).
 */
export function AutorolesTab({ allowed }: { allowed: boolean }) {
  const query = useAutoroles(allowed)
  const remove = useRemoveAutorole()
  const { toast } = useToast()
  const [creating, setCreating] = useState(false)
  const [toRemove, setToRemove] = useState<AutoroleRule | null>(null)

  if (!allowed) return <NotAllowed permission="manage_autorole" />
  const rules = query.data?.rules ?? []

  return (
    <TabState query={query} permission="manage_autorole" what="autorole rules">
      <Card className="gap-0 py-0">
        <CardHeader className="py-4">
          <CardTitle>Autoroles</CardTitle>
          <CardDescription>
            Give a role automatically (same as <span className="font-mono">/autorole</span>). Staff roles, granted roles and roles
            with moderator permissions can never be autoroles.
          </CardDescription>
          <div className="col-start-2 row-span-2 row-start-1 self-start justify-self-end">
            <Button size="sm" onClick={() => setCreating(true)}>
              <Plus /> New rule
            </Button>
          </div>
        </CardHeader>
        {rules.length === 0 ? (
          <EmptyState icon={UserPlus} title="No autorole rules" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>When</TableHead>
                <TableHead>Give role</TableHead>
                <TableHead>Delay</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rules.map((r) => (
                <TableRow key={r.id}>
                  <TableCell>
                    {TRIGGERS[r.trigger_type] ?? r.trigger_type}
                    {r.trigger_type === "role_add" ? (
                      <span className="text-muted-foreground"> : @{r.trigger_role_name ?? r.trigger_role_id ?? "?"}</span>
                    ) : null}
                  </TableCell>
                  <TableCell>@{r.role_name ?? `deleted-role (${r.role_id})`}</TableCell>
                  <TableCell>{formatDelay(r.delay_seconds)}</TableCell>
                  <TableCell>
                    {r.blocked_now ? (
                      <span className="flex items-start gap-1 text-xs text-warning" title={r.blocked_now}>
                        <AlertTriangle className="mt-px size-3 shrink-0" aria-hidden /> Refused at grant time: {r.blocked_now}
                      </span>
                    ) : r.enabled ? (
                      <Badge variant="success" dot>On</Badge>
                    ) : (
                      <Badge variant="outline">Off</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button size="sm" variant="ghost" className="text-danger" aria-label="Remove rule" onClick={() => setToRemove(r)}>
                      <Trash2 />
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>

      {creating ? <CreateAutoroleDialog maxDelay={query.data?.max_delay_seconds ?? 604800} onClose={() => setCreating(false)} /> : null}

      <ConfirmDialog
        open={toRemove !== null}
        onOpenChange={(o) => !o && setToRemove(null)}
        title={`Remove the rule for @${toRemove?.role_name ?? ""}?`}
        description="New members stop getting this role. Members who already have it keep it."
        confirmLabel="Remove rule"
        confirmIcon={<Trash2 />}
        onConfirm={() =>
          toRemove &&
          remove.mutate(toRemove.id, {
            onSuccess: () => toast({ title: "Rule removed", variant: "success" }),
            onError: (e) => toast({ title: "Could not remove", description: describeApiError(e), variant: "destructive" }),
          })
        }
      />
    </TabState>
  )
}

function CreateAutoroleDialog({ maxDelay, onClose }: { maxDelay: number; onClose: () => void }) {
  const roles = useAutoroleRoles(true)
  useRolesLive()
  const create = useCreateAutorole()
  const { toast } = useToast()
  const [trigger, setTrigger] = useState("join")
  const [role, setRole] = useState("")
  const [triggerRole, setTriggerRole] = useState("")
  const [delay, setDelay] = useState("0")
  const dirty = !!role || !!triggerRole || delay !== "0" || trigger !== "join"
  useUnsavedChangesGuard(dirty)

  const delayNum = Number(delay)
  const delayError = !Number.isInteger(delayNum) || delayNum < 0 || delayNum > maxDelay ? `Whole seconds, 0 to ${maxDelay} (7 days).` : null
  const list = (roles.data?.roles ?? []).filter((r) => !r.managed)
  const chosen = list.find((r) => r.id === role)
  const canSubmit = !!role && !chosen?.blocked_reason && !delayError && (trigger !== "role_add" || !!triggerRole)

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[540px]">
        <DialogHeader>
          <DialogTitle>New autorole rule</DialogTitle>
          <DialogDescription>Roles you may not hand out are greyed out with the reason. The bot re-checks when it saves.</DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault()
            if (!canSubmit) return
            create.mutate(
              { trigger, role_id: role, delay_seconds: delayNum, trigger_role_id: trigger === "role_add" ? triggerRole : null },
              {
                onSuccess: () => {
                  toast({ title: "Rule created", variant: "success" })
                  onClose()
                },
                onError: (err) => toast({ title: "Could not create", description: describeApiError(err), variant: "destructive" }),
              }
            )
          }}
        >
          {roles.isError ? <p className="text-sm text-danger">{describeApiError(roles.error, "Could not load roles.")}</p> : null}
          <Field label="When">
            {(p) => (
              <Select {...p} value={trigger} onChange={(e) => setTrigger(e.target.value)}>
                {Object.entries(TRIGGERS).map(([v, l]) => (
                  <option key={v} value={v}>
                    {l}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          {trigger === "role_add" ? (
            <Field label="Trigger role" required hint="Getting this role triggers the rule.">
              {(p) => (
                <Select {...p} value={triggerRole} onChange={(e) => setTriggerRole(e.target.value)} disabled={roles.isPending}>
                  <option value="">Pick a role</option>
                  {list.map((r) => (
                    <option key={r.id} value={r.id}>
                      @{r.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          ) : null}
          <Field label="Role to give" required error={chosen?.blocked_reason ?? null}>
            {(p) => (
              <Select {...p} value={role} onChange={(e) => setRole(e.target.value)} disabled={roles.isPending}>
                <option value="">{roles.isPending ? "Loading…" : "Pick a role"}</option>
                {list.map((r) => (
                  <option key={r.id} value={r.id} disabled={!!r.blocked_reason}>
                    @{r.name}
                    {r.blocked_reason ? ` (not allowed: ${r.blocked_reason})` : ""}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field label="Delay (seconds)" hint="0 = immediately. A bot restart during the delay skips that one grant." error={delayError}>
            {(p) => <Input {...p} type="number" min={0} max={maxDelay} value={delay} onChange={(e) => setDelay(e.target.value)} />}
          </Field>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit} loading={create.isPending}>
              Create rule
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
