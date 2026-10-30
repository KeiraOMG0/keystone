import { useEffect, useState } from "react"
import { Search, UserMinus, UserPlus, Users } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { CopyButton } from "@/components/ui/copy-button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState, ErrorState, ListSkeleton } from "@/components/patterns/states"
import { useToast } from "@/components/ui/toast"
import {
  roleErrorMessage,
  useAddRoleMember,
  useRemoveRoleMember,
  useRoleMemberSearch,
  useRoleMembers,
} from "@/api/roleAdmin"
import type { AdminRole, MemberSearchResult } from "@/types/api"

const PAGE = 25

function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

/**
 * Members with a role (paginated, searchable) plus add/remove. Add/remove is
 * only offered when the bot can manage the role; giving someone a default
 * staff role asks for confirmation first.
 */
export function RoleMembersDialog({
  role,
  onOpenChange,
  disabledReason,
}: {
  role: AdminRole | null
  onOpenChange: (open: boolean) => void
  disabledReason?: string
}) {
  const { toast } = useToast()
  const [offset, setOffset] = useState(0)
  const [filter, setFilter] = useState("")
  const [pick, setPick] = useState("")
  const [confirmAdd, setConfirmAdd] = useState<MemberSearchResult | null>(null)
  const debouncedFilter = useDebounced(filter)
  const debouncedPick = useDebounced(pick)
  const members = useRoleMembers(role?.id ?? null, offset, debouncedFilter.trim())
  const search = useRoleMemberSearch(role?.manageable ? debouncedPick : "")
  const addMember = useAddRoleMember()
  const removeMember = useRemoveRoleMember()

  useEffect(() => {
    setOffset(0)
    setFilter("")
    setPick("")
  }, [role?.id])
  useEffect(() => setOffset(0), [debouncedFilter])

  const canEdit = Boolean(role?.manageable) && !disabledReason
  const total = members.data?.total ?? 0

  // Clamp the page: after members leave (or are removed) the current offset
  // can point past the end, which showed an empty page with no pager to get
  // back. Only on real (non-placeholder) data for the current offset.
  const realData = members.data && !members.isPlaceholderData ? members.data : null
  useEffect(() => {
    if (!realData || offset === 0 || realData.members.length > 0) return
    const lastPage = realData.total > 0 ? Math.floor((realData.total - 1) / PAGE) * PAGE : 0
    setOffset(lastPage < offset ? lastPage : Math.max(0, offset - PAGE))
  }, [realData, offset])

  function doAdd(user: MemberSearchResult) {
    if (!role) return
    addMember.mutate(
      { roleId: role.id, userId: user.id },
      {
        onSuccess: (res) => {
          toast({
            title: res.changed ? `Gave ${role.name} to ${user.display_name}` : `${user.display_name} already has ${role.name}`,
            variant: res.changed ? "success" : "info",
          })
          setPick("")
        },
        onError: (e) => toast({ title: "Could not add the role", description: roleErrorMessage(e), variant: "destructive" }),
      }
    )
  }

  function add(user: MemberSearchResult) {
    if (role?.is_default_staff) setConfirmAdd(user)
    else doAdd(user)
  }

  return (
    <>
      <Dialog open={role !== null} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-[560px]">
          <DialogHeader>
            <DialogTitle>Members with {role?.name}</DialogTitle>
            <DialogDescription>
              {role ? `${role.member_count} member${role.member_count === 1 ? "" : "s"}.` : null}{" "}
              {role && !role.manageable
                ? `You cannot change who has this role here: ${role.unmanageable_reason ?? "it is read-only."}`
                : null}
            </DialogDescription>
          </DialogHeader>

          {role?.manageable ? (
            <div className="flex flex-col gap-2">
              <div className="relative">
                <UserPlus className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
                <Input
                  value={pick}
                  onChange={(e) => setPick(e.target.value)}
                  placeholder="Add a member: type a name (2+ characters)"
                  aria-label="Find a member to add"
                  className="pl-9"
                  disabled={Boolean(disabledReason)}
                  title={disabledReason}
                />
              </div>
              {debouncedPick.trim().length >= 2 ? (
                <div className="max-h-48 overflow-y-auto rounded-lg border border-border">
                  {search.isPending ? <ListSkeleton rows={2} /> : null}
                  {search.isError ? <p className="px-3 py-2 text-sm text-danger">{roleErrorMessage(search.error)}</p> : null}
                  {search.data && search.data.members.length === 0 ? (
                    <p className="px-3 py-2 text-sm text-muted-foreground">No members match.</p>
                  ) : null}
                  {search.data?.members.map((m) => (
                    <div key={m.id} className="flex items-center gap-3 px-3 py-2 hover:bg-hover">
                      <Avatar size="sm">
                        <AvatarImage src={m.avatar_url} alt="" />
                        <AvatarFallback>{m.display_name.slice(0, 1)}</AvatarFallback>
                      </Avatar>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm text-foreground">{m.display_name}</p>
                        <p className="truncate text-xs text-muted-foreground">@{m.username}</p>
                      </div>
                      <Button size="sm" variant="secondary" disabled={!canEdit || addMember.isPending} onClick={() => add(m)}>
                        Add
                      </Button>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}

          {disabledReason ? <Alert variant="warning">{disabledReason}</Alert> : null}

          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter members"
              aria-label="Filter members"
              className="pl-9"
            />
          </div>

          <div className="min-h-40 rounded-lg border border-border">
            {members.isPending ? <ListSkeleton rows={4} /> : null}
            {members.isError ? (
              <ErrorState title="Could not load members" message={roleErrorMessage(members.error)} onRetry={() => void members.refetch()} />
            ) : null}
            {members.data && members.data.members.length === 0 ? (
              <EmptyState icon={Users} title={filter ? "No members match" : "Nobody has this role"} />
            ) : null}
            {members.data && members.data.members.length > 0 ? (
              <ul className="divide-y divide-border">
                {members.data.members.map((m) => (
                  <li key={m.user_id} className="flex items-center gap-3 px-3 py-2">
                    <Avatar size="sm">
                      <AvatarImage src={m.avatar_url} alt="" />
                      <AvatarFallback>{m.display_name.slice(0, 1)}</AvatarFallback>
                    </Avatar>
                    <div className="min-w-0 flex-1">
                      <p className="flex items-center gap-1.5 truncate text-sm text-foreground">
                        {m.display_name} {m.bot ? <Badge variant="outline">bot</Badge> : null}
                      </p>
                      <p className="truncate text-xs text-muted-foreground">@{m.username}</p>
                    </div>
                    <CopyButton value={m.user_id} label="Copy user ID" />
                    {role?.manageable ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={!canEdit || removeMember.isPending}
                        title={disabledReason}
                        onClick={() =>
                          role &&
                          removeMember.mutate(
                            { roleId: role.id, userId: m.user_id },
                            {
                              onSuccess: () => toast({ title: `Removed ${role.name} from ${m.display_name}`, variant: "success" }),
                              onError: (e) =>
                                toast({ title: "Could not remove the role", description: roleErrorMessage(e), variant: "destructive" }),
                            }
                          )
                        }
                      >
                        <UserMinus /> Remove
                      </Button>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>

          {total > PAGE ? (
            <div className="flex items-center justify-between gap-2 text-sm text-muted-foreground">
              <span className="tabular-nums">
                {offset + 1}-{Math.min(offset + PAGE, total)} of {total}
              </span>
              <div className="flex gap-2">
                <Button size="sm" variant="secondary" disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE))}>
                  Previous
                </Button>
                <Button size="sm" variant="secondary" disabled={offset + PAGE >= total} onClick={() => setOffset((o) => o + PAGE)}>
                  Next
                </Button>
              </div>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={confirmAdd !== null}
        onOpenChange={(o) => !o && setConfirmAdd(null)}
        title={`Make ${confirmAdd?.display_name ?? "this member"} default staff?`}
        description={`${role?.name} is a default staff role: members with it pass every bot permission check and can open staff-only dashboard pages.`}
        confirmLabel="Add role"
        onConfirm={() => confirmAdd && doAdd(confirmAdd)}
      />
    </>
  )
}
