import { useMemo, useState } from "react"
import { KeyRound, Play } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import { NAV_LINKS } from "@/components/layout/navLinks"
import { OWNER_ONLY_PERMISSION, STAFF_ONLY_PERMISSION } from "@/lib/owner"
import { useLabPermissions, type LabCommand, type LabOptions, type LabPermissionResult } from "@/api/debugLab"
import { MemberPicker, type PickedMember } from "./MemberPicker"

type Mode = "member" | "roles"

/**
 * Lab: permission simulator. Asks the bot (read-only) what a member, or a
 * hypothetical member holding a chosen role set, can do under the BOT's
 * permission system (services/permission_service.py): staff bypass, which
 * grant gives each permission, which Discord commands pass (requirements read
 * from each command's source by services/debug_lab_service.py) and which
 * dashboard nav pages they would see (computed here from NAV_LINKS, the same
 * filter the sidebar uses).
 */
export function PermissionSimulatorCard({ options }: { options: LabOptions | undefined }) {
  const sim = useLabPermissions()
  const { offline, reason } = useBotOffline()
  const [mode, setMode] = useState<Mode>("member")
  const [member, setMember] = useState<PickedMember | null>(null)
  const [roleIds, setRoleIds] = useState<string[]>([])
  const [roleFilter, setRoleFilter] = useState("")

  const roles = useMemo(
    () => (options?.roles ?? []).filter((r) => r.name.toLowerCase().includes(roleFilter.trim().toLowerCase())),
    [options, roleFilter]
  )

  const run = () =>
    sim.mutate(mode === "member" && member ? { user_id: member.id } : { role_ids: roleIds })

  const disabledReason = offline ? reason : mode === "member" && !member ? "Pick a member" : undefined

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="size-4" aria-hidden /> Permission simulator
        </CardTitle>
        <CardDescription>
          Exactly which bot permissions someone has and why, plus the commands and dashboard pages that gives them.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Tabs value={mode} onValueChange={(v) => setMode(v as Mode)}>
          <TabsList>
            <TabsTrigger value="member">A member</TabsTrigger>
            <TabsTrigger value="roles">A role set</TabsTrigger>
          </TabsList>
        </Tabs>
        {mode === "member" ? (
          <MemberPicker value={member} onChange={setMember} />
        ) : (
          <div className="flex flex-col gap-2">
            <Input value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)} placeholder="Filter roles…" aria-label="Filter roles" />
            <div className="flex max-h-56 flex-col gap-1 overflow-auto rounded-lg border border-border p-2">
              {roles.map((r) => (
                <div key={r.id} className="flex items-center gap-2">
                  <Checkbox
                    id={`labrole-${r.id}`}
                    checked={roleIds.includes(r.id)}
                    onCheckedChange={(v) => setRoleIds((ids) => (v ? [...ids, r.id] : ids.filter((x) => x !== r.id)))}
                  />
                  <Label htmlFor={`labrole-${r.id}`} className="font-normal">
                    @{r.name}
                  </Label>
                  {r.staff ? <Badge variant="accent">staff role</Badge> : null}
                </div>
              ))}
              {roles.length === 0 ? <p className="text-xs text-muted-foreground">No roles.</p> : null}
            </div>
            <p className="text-xs text-muted-foreground">
              {roleIds.length} selected (plus @everyone). Leave empty to see what a member with no roles gets.
            </p>
          </div>
        )}
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={run} disabled={Boolean(disabledReason) || sim.isPending} loading={sim.isPending} title={disabledReason}>
            <Play /> Simulate
          </Button>
          {disabledReason ? <span className="text-xs text-muted-foreground">{disabledReason}</span> : null}
        </div>
        {sim.isError ? <Alert variant="danger" title="Simulation failed">{describeApiError(sim.error)}</Alert> : null}
        {sim.isPending ? <Skeleton className="h-40 w-full" /> : null}
        {sim.data && !sim.isPending ? <SimulationResult r={sim.data} /> : null}
      </CardContent>
    </Card>
  )
}

function SimulationResult({ r }: { r: LabPermissionResult }) {
  const [cmdFilter, setCmdFilter] = useState("")
  const [onlyAllowed, setOnlyAllowed] = useState(false)
  const allowed = useMemo(() => new Set(r.permissions.filter((p) => p.allowed).map((p) => p.permission)), [r])
  const pages = NAV_LINKS.map((l) => {
    const req = l.requiredPermission
    const ok = !req ? true : req === STAFF_ONLY_PERMISSION ? r.is_staff : req === OWNER_ONLY_PERMISSION ? r.is_owner : allowed.has(req)
    const why = !req ? "everyone" : req === STAFF_ONLY_PERMISSION ? "staff only" : req === OWNER_ONLY_PERMISSION ? "owner only" : req
    return { label: l.label, ok, why }
  })
  const commands = r.commands.filter(
    (c) => c.name.toLowerCase().includes(cmdFilter.trim().toLowerCase()) && (!onlyAllowed || c.access !== "no")
  )
  const who = r.subject.mode === "member" ? (r.subject.display_name ?? r.subject.user_id) : "This role set"

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium text-foreground">{who}</span>
        {r.is_owner ? <Badge variant="accent">Owner</Badge> : null}
        {r.is_staff ? (
          <Badge variant="success">
            Staff ({r.staff_reason === "owner" ? "OWNER_ID" : `role ${r.staff_roles.map((s) => s.name).join(", ")}`}): bypasses every check
          </Badge>
        ) : (
          <Badge variant="outline">Not staff</Badge>
        )}
        {r.subject.is_bot ? <Badge variant="warning">Bot account</Badge> : null}
      </div>
      {r.notes.map((n) => (
        <p key={n} className="text-xs text-warning">
          {n}
        </p>
      ))}

      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold text-foreground">Bot permissions</h3>
        <ul className="grid gap-1.5 sm:grid-cols-2">
          {r.permissions.map((p) => (
            <li key={p.permission} className="flex flex-wrap items-center gap-2 text-xs">
              <Badge variant={p.allowed ? "success" : "outline"}>{p.allowed ? "Yes" : "No"}</Badge>
              <span className="font-mono text-foreground">{p.permission}</span>
              <span className="text-muted-foreground">
                {p.via === "owner"
                  ? "owner bypass"
                  : p.via === "staff_role"
                    ? "staff bypass"
                    : p.grants.length
                      ? `granted to @${p.grants.map((g) => g.role_name).join(", @")}`
                      : "no grant"}
              </span>
            </li>
          ))}
        </ul>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold text-foreground">Dashboard pages in the nav</h3>
        <div className="flex flex-wrap gap-1.5">
          {pages.map((p) => (
            <Badge key={p.label} variant={p.ok ? "success" : "outline"} title={p.why}>
              {p.label}
            </Badge>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          Every page is also enforced server-side with the same permission. A member who has left the server gets no pages.
        </p>
      </section>

      <section className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold text-foreground">Discord commands</h3>
        <div className="flex flex-wrap items-center gap-3">
          <Input className="max-w-xs" value={cmdFilter} onChange={(e) => setCmdFilter(e.target.value)} placeholder="Filter commands…" aria-label="Filter commands" />
          <div className="flex items-center gap-2">
            <Checkbox id="lab-only-allowed" checked={onlyAllowed} onCheckedChange={(v) => setOnlyAllowed(v === true)} />
            <Label htmlFor="lab-only-allowed" className="font-normal">
              Only usable ones
            </Label>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          Requirements are read from each command's source (has_permission / is_staff calls), so treat them as a strong hint. "Partly" means the
          entry check passes but some sub-actions need more.
        </p>
        <div className="max-h-[420px] overflow-auto rounded-lg border border-border">
          <ul className="divide-y divide-border">
            {commands.map((c) => (
              <CommandRow key={`${c.kind}:${c.name}`} c={c} />
            ))}
            {commands.length === 0 ? <li className="p-3 text-xs text-muted-foreground">No commands match.</li> : null}
          </ul>
        </div>
      </section>
    </div>
  )
}

function CommandRow({ c }: { c: LabCommand }) {
  return (
    <li className="flex flex-wrap items-center gap-2 px-3 py-1.5 text-xs">
      <Badge variant={c.access === "yes" ? "success" : c.access === "partial" ? "warning" : "outline"}>
        {c.access === "yes" ? "Yes" : c.access === "partial" ? "Partly" : "No"}
      </Badge>
      <span className="font-mono text-foreground">{c.name}</span>
      {c.cog ? <span className="text-muted-foreground">{c.cog}</span> : null}
      <span className="ml-auto flex flex-wrap gap-1">
        {c.checks.length === 0 ? <Badge variant="outline">anyone</Badge> : null}
        {c.checks.map((ch, i) => (
          <Badge key={i} variant={ch.passes ? "default" : "destructive"}>
            {ch.type === "permission" ? ch.permission : ch.type}
          </Badge>
        ))}
        {c.custom_checks ? <Badge variant="info">+{c.custom_checks} decorator check</Badge> : null}
        {c.default_permissions.length ? (
          <Badge variant={c.discord_visible ? "outline" : "warning"} title="Discord-side default_permissions (visibility)">
            Discord: {c.default_permissions.join(", ")}
          </Badge>
        ) : null}
      </span>
    </li>
  )
}
