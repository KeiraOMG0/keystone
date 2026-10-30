import { useMemo, useState } from "react"
import { KeyRound, Plus } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Checkbox } from "@/components/ui/checkbox"
import { Select } from "@/components/ui/select"
import { Field } from "@/components/ui/field"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Tip } from "@/components/ui/tooltip"
import { useToast } from "@/components/ui/toast"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { StaffOnlyBadge } from "@/components/patterns/StaffOnlyBadge"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState, ErrorState, TableSkeleton } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { useAddGrant, useGuildRoles, usePermissionGrants, useRevokeGrant } from "@/api/permissions"
import { useRolesLive } from "@/api/rolesLive"
import { ApiError } from "@/api/client"
import type { ForbiddenError, GuildRole } from "@/types/api"
import { describeApiError } from "@/lib/apiError"
import { INTERNAL_PERMISSION_HELP } from "@/pages/roles/roleUtils"

// Short column labels for the matrix header, one per string in
// services/permission_service.py VALID_PERMISSIONS (keep the 14 in sync, see
// CLAUDE.md). The full permission string and its description (roleUtils'
// INTERNAL_PERMISSION_HELP) are the tooltip. Anything not listed falls back
// to the raw string so a newly added permission never renders blank.
const PERMISSION_LABELS: Record<string, string> = {
  staff_base: "Staff base",
  moderate_members: "Moderate",
  kick_members: "Kick",
  ban_members: "Ban",
  manage_messages_bulk: "Bulk purge",
  verify_members: "Verify",
  view_audit: "Audit log",
  manage_tickets: "Tickets",
  manage_templates: "Templates",
  manage_events: "Events",
  manage_schedules: "Schedules",
  manage_autorole: "Autorole",
  manage_reaction_roles: "Self-roles",
  manage_qotd: "QOTD",
  manage_birthdays: "Birthdays",
  control_music: "Music",
}

/**
 * Grants that need the typed GRANT confirmation (every grant already confirms).
 * Chosen because they act on members or reveal staff activity:
 *   ban_members, kick_members, moderate_members, manage_messages_bulk
 *       punish members or delete their messages;
 *   verify_members, manage_autorole, manage_reaction_roles
 *       hand Discord roles to members (role-granting power);
 *   view_audit       shows every staff action and its details;
 *   staff_base       opens the dashboard overview and member lookups.
 * Content-only grants (tickets, templates, events, schedules, QOTD) just confirm.
 * There is no grantable "manage permissions": this page is require_staff only.
 */
const SENSITIVE_PERMISSIONS = new Set([
  "ban_members",
  "kick_members",
  "moderate_members",
  "manage_messages_bulk",
  "verify_members",
  "manage_autorole",
  "manage_reaction_roles",
  "view_audit",
  "staff_base",
])

function isSensitive(permission: string | undefined): boolean {
  return permission !== undefined && SENSITIVE_PERMISSIONS.has(permission)
}

function permissionLabel(permission: string): string {
  return PERMISSION_LABELS[permission] ?? permission
}

/**
 * STAFF-ONLY. A checkbox matrix: rows = this guild's Discord roles (GET
 * /api/permissions/roles), columns = the VALID_PERMISSIONS strings. Checking a
 * box grants, unchecking revokes. Revoking is immediate; granting asks for
 * confirmation first (it hands out a new capability), and the sensitive grants
 * (SENSITIVE_PERMISSIONS: moderation, role-granting, audit, staff_base) need
 * you to type GRANT. A "Grant permission" dialog
 * offers the same action without hunting through the matrix.
 *
 * Gating: GET/POST/DELETE /api/permissions/* need require_staff, NOT a
 * grantable permission (gating on a grant would let any role holding it
 * self-escalate). Roles in DEFAULT_STAFF_ROLE_IDS already bypass every
 * has_permission() check, so their checkboxes are checked and disabled. The
 * server owner is not a role, so a banner explains them. Managed (bot) roles
 * are left out: bots can never sign in to the dashboard.
 */
export default function PermissionsPage() {
  const rolesQuery = useGuildRoles()
  // Live role list (created/renamed/reordered/deleted in Discord). Only once
  // the roles loaded once, i.e. the viewer is staff and may receive the topic.
  // Gated on cached data, not isSuccess, so a failed background refetch does
  // not drop the subscription.
  useRolesLive({ enabled: rolesQuery.data !== undefined })
  const grantsQuery = usePermissionGrants()
  const addGrant = useAddGrant()
  const revokeGrant = useRevokeGrant()
  const { toast } = useToast()

  const [pendingKey, setPendingKey] = useState<string | null>(null)
  const [confirmTarget, setConfirmTarget] = useState<{ role: GuildRole; permission: string } | null>(null)
  const [showAllRoles, setShowAllRoles] = useState(false)
  const [pickerOpen, setPickerOpen] = useState(false)
  const [pickRole, setPickRole] = useState("")
  const [pickPerm, setPickPerm] = useState("")

  const grantedSet = useMemo(() => {
    const set = new Set<string>()
    for (const g of grantsQuery.data?.grants ?? []) {
      set.add(`${g.role_id}:${g.permission}`)
    }
    return set
  }, [grantsQuery.data])

  const permissions = grantsQuery.data?.valid_permissions ?? []
  const allRoles = (rolesQuery.data?.roles ?? []).filter((role) => !role.managed)

  const rolesWithGrants = useMemo(() => {
    const set = new Set<string>()
    for (const g of grantsQuery.data?.grants ?? []) {
      set.add(g.role_id)
    }
    return set
  }, [grantsQuery.data])

  // Default view hides cosmetic/self-assignable roles: a role earns a spot by
  // already holding a grant or being staff-by-default; the rest is one click away.
  const managementRoles = useMemo(
    () => allRoles.filter((role) => role.is_default_staff || rolesWithGrants.has(role.id)),
    [allRoles, rolesWithGrants]
  )
  const roles = showAllRoles ? allRoles : managementRoles
  const hiddenCount = allRoles.length - managementRoles.length

  const firstError = [rolesQuery, grantsQuery].find(
    (q) => q.isError && q.error instanceof ApiError && q.error.status === 403
  )
  const forbidden =
    firstError && firstError.error instanceof ApiError && isForbiddenError(firstError.error.body)
      ? (firstError.error.body as ForbiddenError)
      : null

  if (forbidden) {
    return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />
  }

  function applyGrant(role: GuildRole, permission: string) {
    const key = `${role.id}:${permission}`
    setPendingKey(key)
    addGrant.mutate(
      { role_id: role.id, permission },
      {
        onSettled: () => setPendingKey((k) => (k === key ? null : k)),
        onSuccess: () => toast({ title: `Granted ${permissionLabel(permission)} to ${role.name}`, variant: "success" }),
        onError: (error) => {
          toast({
            title: "Failed to grant permission",
            description: describeApiError(error),
            variant: "destructive",
          })
        },
      }
    )
  }

  function applyRevoke(role: GuildRole, permission: string) {
    const key = `${role.id}:${permission}`
    setPendingKey(key)
    revokeGrant.mutate(
      { role_id: role.id, permission },
      {
        onSettled: () => setPendingKey((k) => (k === key ? null : k)),
        onSuccess: () => toast({ title: `Revoked ${permissionLabel(permission)} from ${role.name}`, variant: "success" }),
        onError: (error) => {
          toast({
            title: "Failed to revoke permission",
            description: describeApiError(error),
            variant: "destructive",
          })
        },
      }
    )
  }

  function toggle(role: GuildRole, permission: string, currentlyGranted: boolean) {
    if (role.is_default_staff) return // no-op: they bypass grants anyway
    if (currentlyGranted) {
      applyRevoke(role, permission)
      return
    }
    setConfirmTarget({ role, permission })
  }

  const isLoading = rolesQuery.isPending || grantsQuery.isPending
  const isError = (rolesQuery.isError || grantsQuery.isError) && !forbidden
  const grantable = allRoles.filter((r) => !r.is_default_staff)

  return (
    <PageContainer>
      <PageHeader
        title="Permissions"
        badge={<StaffOnlyBadge />}
        description="Choose what each Discord role can do in the dashboard and the bot. Changes apply immediately."
        actions={
          <Button
            onClick={() => {
              setPickRole("")
              setPickPerm("")
              setPickerOpen(true)
            }}
            disabled={isLoading || isError}
          >
            <Plus /> Grant permission
          </Button>
        }
      />

      <Alert variant="info">
        The server owner and roles marked <Badge variant="info">Default staff</Badge> already have every permission regardless of what is
        checked below: they bypass grants entirely, so their rows are disabled.
      </Alert>

      <Card className="gap-0 py-0">
        <CardHeader className="border-b py-4">
          <CardTitle>Role permission matrix</CardTitle>
          <CardDescription>Rows are this server's roles; columns are the available permissions.</CardDescription>
          {!isLoading && !isError && hiddenCount > 0 ? (
            <div className="col-start-2 row-span-2 row-start-1 self-start justify-self-end">
              <Button type="button" variant="secondary" size="sm" onClick={() => setShowAllRoles((v) => !v)}>
                {showAllRoles ? "Show management roles only" : `Show all ${allRoles.length} roles`}
              </Button>
            </div>
          ) : null}
        </CardHeader>

        {isLoading ? <TableSkeleton rows={4} cols={6} /> : null}

        {isError ? (
          <ErrorState
            title="Could not load roles or grants"
            onRetry={() => {
              void rolesQuery.refetch()
              void grantsQuery.refetch()
            }}
            retrying={rolesQuery.isRefetching || grantsQuery.isRefetching}
            showStatusLink
          />
        ) : null}

        {!isLoading && !isError && roles.length > 0 ? (
          <div className="max-h-[70vh] overflow-auto">
            <Table>
              <TableHeader className="sticky top-0 z-20">
                <TableRow>
                  <TableHead scope="col" className="sticky left-0 z-30 bg-card">
                    Role
                  </TableHead>
                  {permissions.map((perm) => (
                    <TableHead key={perm} className="text-center">
                      <Tip label={INTERNAL_PERMISSION_HELP[perm] ? `${perm}: ${INTERNAL_PERMISSION_HELP[perm]}` : perm}>
                        <span tabIndex={0} className="cursor-help rounded-sm">
                          {permissionLabel(perm)}
                        </span>
                      </Tip>
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {roles.map((role) => (
                  <TableRow key={role.id}>
                    <TableHead scope="row" className="sticky left-0 z-10 bg-card text-base font-normal whitespace-nowrap text-foreground">
                      <div className="flex items-center gap-2">
                        <span
                          aria-hidden
                          className="size-2.5 shrink-0 rounded-full"
                          style={{ backgroundColor: role.color ?? "var(--subtle-foreground)" }}
                        />
                        <span className="text-foreground">{role.name}</span>
                        {role.is_default_staff ? <Badge variant="info">Default staff</Badge> : null}
                      </div>
                    </TableHead>
                    {permissions.map((perm) => {
                      const key = `${role.id}:${perm}`
                      const granted = role.is_default_staff || grantedSet.has(key)
                      const disabled = role.is_default_staff || pendingKey === key
                      return (
                        <TableCell key={perm} className="text-center">
                          <Checkbox
                            checked={granted}
                            disabled={disabled}
                            onCheckedChange={() => toggle(role, perm, grantedSet.has(key))}
                            aria-label={`${role.name}: ${perm}`}
                            className="mx-auto"
                          />
                        </TableCell>
                      )
                    })}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : null}

        {!isLoading && !isError && roles.length === 0 ? (
          <EmptyState icon={KeyRound} title="No roles found" description="This server has no roles that can hold permissions." />
        ) : null}
      </Card>

      {/* Grant permission picker */}
      <Dialog open={pickerOpen} onOpenChange={setPickerOpen}>
        <DialogContent className="max-w-[480px]">
          <DialogHeader>
            <DialogTitle>Grant permission</DialogTitle>
            <DialogDescription>Pick a role and the permission it should get. You will confirm on the next step.</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-4">
            <Field label="Role" required>
              {(p) => (
                <Select {...p} value={pickRole} onChange={(e) => setPickRole(e.target.value)}>
                  <option value="">Select a role…</option>
                  {grantable.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Permission" required>
              {(p) => (
                <Select {...p} value={pickPerm} onChange={(e) => setPickPerm(e.target.value)}>
                  <option value="">Select a permission…</option>
                  {permissions.map((perm) => (
                    <option key={perm} value={perm}>
                      {permissionLabel(perm)} ({perm})
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setPickerOpen(false)}>
              Cancel
            </Button>
            <Button
              disabled={!pickRole || !pickPerm}
              onClick={() => {
                const role = allRoles.find((r) => r.id === pickRole)
                if (role && pickPerm) {
                  setPickerOpen(false)
                  if (grantedSet.has(`${role.id}:${pickPerm}`)) {
                    toast({ title: `${role.name} already has ${permissionLabel(pickPerm)}`, variant: "info" })
                  } else {
                    setConfirmTarget({ role, permission: pickPerm })
                  }
                }
              }}
            >
              Continue
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmTarget !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmTarget(null)
        }}
        destructive={isSensitive(confirmTarget?.permission)}
        title={`Grant ${confirmTarget ? permissionLabel(confirmTarget.permission) : ""}${confirmTarget ? ` to ${confirmTarget.role.name}` : ""}?`}
        description={
          confirmTarget && isSensitive(confirmTarget.permission)
            ? `This is a sensitive grant (${confirmTarget.permission}: ${INTERNAL_PERMISSION_HELP[confirmTarget.permission] ?? permissionLabel(confirmTarget.permission)}). Every member with the ${confirmTarget.role.name} role gets it immediately, and it acts on members or their data.`
            : `This gives every member with the ${confirmTarget?.role.name} role the ability to use ${confirmTarget ? permissionLabel(confirmTarget.permission) : "this feature"}.`
        }
        confirmLabel="Grant permission"
        typeToConfirm={isSensitive(confirmTarget?.permission) ? "GRANT" : undefined}
        onConfirm={() => {
          if (confirmTarget) applyGrant(confirmTarget.role, confirmTarget.permission)
        }}
      />
    </PageContainer>
  )
}

function isForbiddenError(body: unknown): body is ForbiddenError {
  return (
    typeof body === "object" &&
    body !== null &&
    "error" in body &&
    (body as { error: unknown }).error === "forbidden"
  )
}
