import { Fragment, useMemo, useState } from "react"
import {
  ArrowDown,
  ArrowUp,
  Bot,
  Copy,
  CopyPlus,
  Lock,
  MoreHorizontal,
  Pencil,
  Plus,
  Search,
  Shapes,
  Trash2,
  Users,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Tip } from "@/components/ui/tooltip"
import { CopyButton } from "@/components/ui/copy-button"
import { useToast } from "@/components/ui/toast"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { StaffOnlyBadge } from "@/components/patterns/StaffOnlyBadge"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState, ErrorState, ListSkeleton } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { ApiError } from "@/api/client"
import {
  roleErrorMessage,
  useDeleteRole,
  useDuplicateRole,
  useMoveRole,
  useRoleAdminOverview,
  useRoleAdminLive,
} from "@/api/roleAdmin"
import { useBotOffline } from "@/hooks/useBotOffline"
import type { AdminRole, ForbiddenError } from "@/types/api"
import {
  ROLE_FILTERS,
  grantablePermissions,
  matchesFilter,
  matchesSearch,
  maxManagedPosition,
  type RoleFilter,
} from "./roleUtils"
import { RoleEditorDialog } from "./RoleEditorDialog"
import { RoleMembersDialog } from "./RoleMembersDialog"

/**
 * STAFF-ONLY role manager. Lists every Discord role (highest first) with a
 * divider under the bot's own top role: roles below it are manageable, roles
 * above it (and managed/@everyone) are read-only. Create / edit / move /
 * duplicate / delete roles, view and change members, and set each role's
 * internal bot permissions (the Permissions page's grants).
 *
 * Gating: every /api/roles route is require_staff (a grantable permission here
 * would allow self-escalation). Live: the `roles` topic re-fetches the list;
 * while the socket is down the list polls every 30 s.
 */
export default function RolesPage() {
  const overview = useRoleAdminOverview()
  // Subscribe once the overview loaded (the viewer is staff, so the hub allows
  // `roles`); gated on cached data so a failed refetch keeps the subscription.
  useRoleAdminLive({ enabled: overview.data !== undefined })
  const bot = useBotOffline()
  const { toast } = useToast()
  const moveRole = useMoveRole()
  const duplicateRole = useDuplicateRole()
  const deleteRole = useDeleteRole()

  const [q, setQ] = useState("")
  const [filter, setFilter] = useState<RoleFilter>("all")
  const [editor, setEditor] = useState<{ mode: "create" | "edit"; roleId: string | null } | null>(null)
  const [membersRoleId, setMembersRoleId] = useState<string | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<AdminRole | null>(null)
  const [adminDuplicate, setAdminDuplicate] = useState<AdminRole | null>(null)
  const [movingId, setMovingId] = useState<string | null>(null)

  const data = overview.data
  const roles = useMemo(
    () => (data?.roles ?? []).filter((r) => matchesFilter(r, filter) && matchesSearch(r, q)),
    [data, filter, q]
  )
  const editorRole = editor?.roleId ? data?.roles.find((r) => r.id === editor.roleId) ?? null : null
  const membersRole = membersRoleId ? data?.roles.find((r) => r.id === membersRoleId) ?? null : null

  if (overview.isError && overview.error instanceof ApiError && overview.error.status === 403) {
    const body = overview.error.body as ForbiddenError | { error?: string } | null
    return (
      <Forbidden403
        reason={body?.error === "not_a_member" ? "not_a_member" : "forbidden"}
        requiredPermission={(body as ForbiddenError | null)?.required_permission ?? "staff"}
      />
    )
  }

  const offlineReason = bot.reason
  const canCreate = Boolean(data?.bot.manage_roles) && !offlineReason
  const maxPos = data ? maxManagedPosition(data) : 1
  // Divider: the first listed role that sits below the bot's top role.
  const dividerBeforeId = data ? roles.find((r) => r.position < data.bot.top_position)?.id : undefined

  function move(role: AdminRole, position: number) {
    setMovingId(role.id)
    moveRole.mutate(
      { roleId: role.id, position },
      {
        onSettled: () => setMovingId(null),
        onError: (e) => toast({ title: `Could not move ${role.name}`, description: roleErrorMessage(e), variant: "destructive" }),
      }
    )
  }

  function duplicate(role: AdminRole, confirmAdministrator = false) {
    // A copy that would carry Administrator (the bot and the viewer may grant
    // it) needs the same typed ADMINISTRATOR confirmation as create/edit.
    if (
      !confirmAdministrator &&
      data &&
      role.permissions.includes("administrator") &&
      grantablePermissions(data).has("administrator")
    ) {
      setAdminDuplicate(role)
      return
    }
    duplicateRole.mutate(
      { roleId: role.id, confirmAdministrator },
      {
        onSuccess: (res) =>
          toast({
            title: `Created ${res.role.name}`,
            description: res.dropped_permissions?.length
              ? `Left out permissions that you or the bot cannot grant: ${res.dropped_permissions.join(", ")}.`
              : undefined,
            variant: res.dropped_permissions?.length ? "info" : "success",
            action: { label: "Edit", onClick: () => setEditor({ mode: "edit", roleId: res.role.id }) },
          }),
        onError: (e) => toast({ title: `Could not duplicate ${role.name}`, description: roleErrorMessage(e), variant: "destructive" }),
      }
    )
  }

  return (
    <PageContainer>
      <PageHeader
        title="Roles"
        badge={<StaffOnlyBadge />}
        description="Create, edit, reorder and assign Discord roles, and set what each role may do in the bot."
        actions={
          <Tip label={offlineReason ?? (data && !data.bot.manage_roles ? "The bot needs Manage Roles" : "New role")}>
            <span>
              <Button onClick={() => setEditor({ mode: "create", roleId: null })} disabled={!canCreate}>
                <Plus /> Create role
              </Button>
            </span>
          </Tip>
        }
      />

      {data && !data.bot.manage_roles ? (
        <Alert variant="warning" title="The bot cannot manage roles">
          Give the bot's role the Manage Roles permission in Discord. Until then every role here is read-only.
        </Alert>
      ) : null}
      {data?.actor && !data.actor.is_owner ? (
        <Alert variant="info" title="Discord's role hierarchy applies to you">
          You can change only roles below your highest role
          {data.actor.top_role_id ? ` (${data.roles.find((r) => r.id === data.actor?.top_role_id)?.name ?? "unknown"})` : ""},
          grant only permissions you have yourself, and cannot give roles to yourself. Administrator is owner-only.
        </Alert>
      ) : null}

      <Card className="gap-0 py-0">
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
          <div className="relative min-w-48 flex-1">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search by name or ID" aria-label="Search roles" className="pl-9" />
          </div>
          <div className="w-full sm:w-56">
            <Select value={filter} onChange={(e) => setFilter(e.target.value as RoleFilter)} aria-label="Filter roles">
              {ROLE_FILTERS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </Select>
          </div>
          {data ? (
            <span className="text-xs text-muted-foreground tabular-nums">
              {data.guild.role_count} / {data.guild.role_limit} roles
            </span>
          ) : null}
        </div>

        {overview.isPending ? <ListSkeleton rows={6} avatar={false} /> : null}
        {overview.isError ? (
          <ErrorState
            title="Could not load roles"
            message={roleErrorMessage(overview.error)}
            onRetry={() => void overview.refetch()}
            retrying={overview.isRefetching}
            showStatusLink
          />
        ) : null}
        {data && roles.length === 0 ? (
          <EmptyState icon={Shapes} title="No roles match" description="Try another search or filter." />
        ) : null}

        {data && roles.length > 0 ? (
          <ul className="divide-y divide-border">
            {roles.map((role) => (
              <Fragment key={role.id}>
                {role.id === dividerBeforeId ? (
                  <li className="flex items-center gap-2 bg-primary-soft px-4 py-1.5 text-xs font-medium text-primary-soft-foreground" role="separator">
                    <Bot className="size-3.5" aria-hidden />
                    Roles below this line are manageable by the bot ({data.bot.top_role_name})
                  </li>
                ) : null}
                <RoleRow
                  role={role}
                  maxPos={maxPos}
                  moving={movingId === role.id}
                  offlineReason={offlineReason}
                  onEdit={() => setEditor({ mode: "edit", roleId: role.id })}
                  onMembers={() => setMembersRoleId(role.id)}
                  onDuplicate={() => duplicate(role)}
                  onDelete={() => setDeleteTarget(role)}
                  onMove={(pos) => move(role, pos)}
                />
              </Fragment>
            ))}
          </ul>
        ) : null}
      </Card>

      {data && editor ? (
        <RoleEditorDialog
          open={editor !== null}
          onOpenChange={(o) => !o && setEditor(null)}
          mode={editor.mode}
          role={editorRole}
          data={data}
          disabledReason={offlineReason}
        />
      ) : null}

      <RoleMembersDialog role={membersRole} onOpenChange={(o) => !o && setMembersRoleId(null)} disabledReason={offlineReason} />

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
        title={`Delete ${deleteTarget?.name ?? "role"}?`}
        description={
          deleteTarget
            ? `This permanently deletes the role in Discord and removes it from ${deleteTarget.member_count} member${deleteTarget.member_count === 1 ? "" : "s"}. ${
                deleteTarget.internal_permissions.length ? "Its bot permissions are removed too. " : ""
              }This cannot be undone.`
            : ""
        }
        confirmLabel="Delete role"
        confirmIcon={<Trash2 />}
        typeToConfirm={deleteTarget?.name}
        onConfirm={() => {
          const target = deleteTarget
          if (!target) return
          deleteRole.mutate(
            { roleId: target.id, confirmName: target.name },
            {
              onSuccess: () => toast({ title: `Deleted ${target.name}`, variant: "success" }),
              onError: (e) => toast({ title: `Could not delete ${target.name}`, description: roleErrorMessage(e), variant: "destructive" }),
            }
          )
        }}
      />

      <ConfirmDialog
        open={adminDuplicate !== null}
        onOpenChange={(o) => !o && setAdminDuplicate(null)}
        title={`Duplicate ${adminDuplicate?.name ?? "role"} with Administrator?`}
        description="This role has Administrator, so the copy will have it too. Administrator bypasses every channel permission and lets members with the role do anything in the server. Only create it if you mean to give it to fully trusted staff."
        confirmLabel="Duplicate with Administrator"
        confirmIcon={<CopyPlus />}
        typeToConfirm="ADMINISTRATOR"
        onConfirm={() => {
          const target = adminDuplicate
          if (target) duplicate(target, true)
        }}
      />
    </PageContainer>
  )
}

function RoleRow({
  role,
  maxPos,
  moving,
  offlineReason,
  onEdit,
  onMembers,
  onDuplicate,
  onDelete,
  onMove,
}: {
  role: AdminRole
  maxPos: number
  moving: boolean
  offlineReason?: string
  onEdit: () => void
  onMembers: () => void
  onDuplicate: () => void
  onDelete: () => void
  onMove: (position: number) => void
}) {
  const canChange = role.manageable && !offlineReason
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-2.5 sm:flex-nowrap">
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        <span
          aria-hidden
          className="size-3 shrink-0 rounded-full border border-border"
          style={{ backgroundColor: role.color ?? "var(--subtle-foreground)" }}
        />
        {role.icon_url ? <img src={role.icon_url} alt="" className="size-4 shrink-0 rounded-sm" /> : null}
        {role.unicode_emoji ? <span aria-hidden>{role.unicode_emoji}</span> : null}
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={onEdit}
              className="truncate rounded-sm text-left text-base font-medium text-foreground outline-none hover:underline focus-visible:outline-2 focus-visible:outline-ring"
              style={role.color ? { color: role.color } : undefined}
            >
              {role.name}
            </button>
            {role.is_bot_top_role ? <Badge variant="accent"><Bot /> Bot's top role</Badge> : null}
            {role.is_default_staff ? <Badge variant="info">Default staff</Badge> : null}
            {role.is_premium_subscriber ? <Badge variant="outline">Booster</Badge> : null}
            {role.managed && !role.is_bot_top_role && !role.is_premium_subscriber ? <Badge variant="outline">Managed</Badge> : null}
            {role.hoist ? <Badge variant="outline">Separate</Badge> : null}
            {role.mentionable ? <Badge variant="outline">@mentionable</Badge> : null}
            {role.permissions.includes("administrator") ? <Badge variant="destructive">Administrator</Badge> : null}
          </div>
          <div className="flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
            <span className="tabular-nums">Position {role.position}</span>
            <span className="tabular-nums">
              {role.member_count} member{role.member_count === 1 ? "" : "s"}
            </span>
            {role.internal_permissions.length ? (
              <Tip label={role.internal_permissions.join(", ")}>
                <span tabIndex={0} className="cursor-help underline decoration-dotted">
                  {role.internal_permissions.length} bot permission{role.internal_permissions.length === 1 ? "" : "s"}
                </span>
              </Tip>
            ) : null}
            <span className="inline-flex items-center gap-0.5 font-mono">
              {role.id}
              <CopyButton value={role.id} label={`Copy ID of ${role.name}`} />
            </span>
          </div>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-1">
        {role.manageable ? (
          <>
            <Tip label={offlineReason ?? "Move up"}>
              <span>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Move ${role.name} up`}
                  disabled={!canChange || moving || role.position >= maxPos}
                  onClick={() => onMove(role.position + 1)}
                >
                  <ArrowUp />
                </Button>
              </span>
            </Tip>
            <Tip label={offlineReason ?? "Move down"}>
              <span>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Move ${role.name} down`}
                  disabled={!canChange || moving || role.position <= 1}
                  onClick={() => onMove(role.position - 1)}
                >
                  <ArrowDown />
                </Button>
              </span>
            </Tip>
          </>
        ) : (
          <Tip label={role.unmanageable_reason ?? "Read-only"}>
            <span tabIndex={0} className="inline-flex size-[30px] items-center justify-center rounded-md text-subtle-foreground" aria-label={`Read-only: ${role.unmanageable_reason ?? ""}`}>
              <Lock className="size-4" />
            </span>
          </Tip>
        )}
        <Button variant="ghost" size="sm" onClick={onMembers} aria-label={`Members of ${role.name}`}>
          <Users /> <span className="max-sm:sr-only">Members</span>
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label={`More actions for ${role.name}`}>
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuItem onSelect={onEdit}>
              <Pencil /> {role.manageable ? "Edit" : "View / bot permissions"}
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={onMembers}>
              <Users /> View members
            </DropdownMenuItem>
            <DropdownMenuItem disabled={role.is_default || Boolean(offlineReason)} onSelect={onDuplicate}>
              <CopyPlus /> Duplicate
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void navigator.clipboard?.writeText(role.id)}>
              <Copy /> Copy role ID
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem danger disabled={!canChange} onSelect={onDelete}>
              <Trash2 /> Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </li>
  )
}
