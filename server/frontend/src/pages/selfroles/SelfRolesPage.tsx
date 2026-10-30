import { useMemo, useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { ChevronDown, ChevronRight, Pencil, Plus, RefreshCw, Search, Send, Smile, Trash2, TriangleAlert, X } from "lucide-react"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { Field } from "@/components/ui/field"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { useToast } from "@/components/ui/toast"
import { ResolvedRole } from "@/components/ResolvedRole"
import { ResolvedChannel } from "@/components/ResolvedChannel"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState, ErrorState } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { ApiError } from "@/api/client"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import {
  useAddReactionRoleEntry,
  useCreateReactionRolePanel,
  useDeleteReactionRolePanel,
  useGuildChannels,
  usePostReactionRolePanel,
  useReactionRoleEditorRoles,
  useReactionRolePanels,
  useRemoveReactionRoleEntry,
  useUpdateReactionRolePanel,
} from "@/api/reactionRoles"
import { invalidateRoleQueries, useRolesLive } from "@/api/rolesLive"
import type { ForbiddenError, ReactionRolePanel } from "@/types/api"

/**
 * PROTECTED. Reaction-role ("self-roles") panel builder. Backed by
 * /api/reaction-roles/* (needs "manage_reaction_roles", the same permission
 * the /reactionrole slash commands require).
 *
 * "Unique role" = the pre-existing `exclusive` flag: picking one role removes
 * any other from the SAME panel (radio-button style). Every add/remove/rename/
 * toggle automatically edits the LIVE Discord message in place if the panel was
 * already posted; only the first post (or an explicit re-post, which deletes
 * and re-sends so the message link changes) needs the separate Post button.
 * Removing an entry is immediate with an Undo toast that re-adds the same
 * mapping (it lands at the end of the list).
 *
 * The role list is LIVE: the bot publishes the `roles` topic on guild role
 * create/update/reorder/delete (cogs/live_roles.py) and useRolesLive re-fetches
 * the picker plus every <ResolvedRole> cache. "Refresh roles" is the manual
 * fallback. Entries whose role no longer exists get a "Deleted role" badge.
 */
export default function SelfRolesPage() {
  const panelsQuery = useReactionRolePanels()
  const rolesQuery = useReactionRoleEditorRoles()
  const queryClient = useQueryClient()
  const bot = useBotOffline()
  const [creating, setCreating] = useState(false)
  const [search, setSearch] = useState("")
  const [refreshingRoles, setRefreshingRoles] = useState(false)

  // null until the role list loaded: never flag entries as deleted on a failed
  // or in-flight fetch.
  const knownRoleIds = useMemo(
    () => (rolesQuery.isSuccess ? new Set(rolesQuery.data.map((r) => r.id)) : null),
    [rolesQuery.isSuccess, rolesQuery.data]
  )

  function handleRefreshRoles() {
    setRefreshingRoles(true)
    void invalidateRoleQueries(queryClient).finally(() => setRefreshingRoles(false))
  }

  const filteredPanels = useMemo(() => {
    const query = search.trim().toLowerCase()
    if (!query) return panelsQuery.data ?? []
    return (panelsQuery.data ?? []).filter((panel) => {
      return (
        panel.title.toLowerCase().includes(query) ||
        panel.entries.some((entry) => entry.label?.toLowerCase().includes(query) ?? false)
      )
    })
  }, [panelsQuery.data, search])

  const forbidden =
    panelsQuery.isError &&
    panelsQuery.error instanceof ApiError &&
    panelsQuery.error.status === 403 &&
    isForbiddenError(panelsQuery.error.body)
      ? (panelsQuery.error.body as ForbiddenError)
      : null

  // Subscribe once the panels have loaded at least once (so the viewer has
  // manage_reaction_roles) and while not forbidden. Gated on cached data, NOT
  // isSuccess: a failed background refetch (e.g. 502 during a bot restart)
  // flips status to "error" but keeps the data, and must not drop the
  // subscription (reaction-role-panels is not a resync root to revive it).
  useRolesLive({ enabled: panelsQuery.data !== undefined && !forbidden })

  if (forbidden) {
    return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />
  }

  return (
    <PageContainer width="form">
      <PageHeader
        title="Self-roles"
        description="Role panels members can pick from, using buttons on a Discord message."
        actions={
          <>
            <Button
              variant="secondary"
              onClick={handleRefreshRoles}
              loading={refreshingRoles}
              disabled={bot.offline}
              title={bot.reason ?? "Re-fetch the role list from Discord (it also updates live)"}
            >
              {refreshingRoles ? null : <RefreshCw />} Refresh roles
            </Button>
            <Button onClick={() => setCreating(true)}>
              <Plus /> New panel
            </Button>
          </>
        }
      />

      <CreatePanelDialog open={creating} onOpenChange={setCreating} />

      {panelsQuery.isPending ? (
        <div className="flex flex-col gap-3" aria-busy="true">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : null}

      {panelsQuery.isError && !forbidden ? (
        <Card>
          <ErrorState
            title="Could not load panels"
            message={describeApiError(panelsQuery.error, "The panels request failed.")}
            onRetry={() => void panelsQuery.refetch()}
            retrying={panelsQuery.isRefetching}
            showStatusLink
          />
        </Card>
      ) : null}

      {panelsQuery.data && panelsQuery.data.length > 0 ? (
        <div className="relative w-full sm:w-72">
          <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-subtle-foreground" />
          <Input
            type="text"
            data-page-search
            aria-label="Search panels"
            placeholder="Search by title or role label"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-9"
          />
        </div>
      ) : null}

      {panelsQuery.data ? (
        panelsQuery.data.length === 0 ? (
          <Card>
            <EmptyState
              icon={Smile}
              title="No self-role panels yet"
              description="A panel is a message with buttons members click to get or drop a role."
              action={
                <Button onClick={() => setCreating(true)}>
                  <Plus /> New panel
                </Button>
              }
            />
          </Card>
        ) : filteredPanels.length === 0 ? (
          <Card>
            <EmptyState icon={Search} title="No matches" description={`No panels match "${search.trim()}".`} />
          </Card>
        ) : (
          <div className="flex flex-col gap-4">
            {filteredPanels.map((panel, i) => (
              <PanelCard key={panel.id} panel={panel} defaultOpen={i === 0} knownRoleIds={knownRoleIds} />
            ))}
          </div>
        )
      ) : null}
    </PageContainer>
  )
}

function CreatePanelDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const channelsQuery = useGuildChannels()
  const createPanel = useCreateReactionRolePanel()
  const { toast } = useToast()
  const [channelId, setChannelId] = useState("")
  const [title, setTitle] = useState("React for a role!")
  const [exclusive, setExclusive] = useState(false)

  const canSubmit = channelId.length > 0 && title.trim().length > 0 && !createPanel.isPending

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    // channelId is a Discord snowflake: pass the string through unchanged, never Number().
    createPanel.mutate(
      { channel_id: channelId, title: title.trim(), exclusive },
      {
        onSuccess: () => {
          toast({ title: "Panel created", description: "Add role mappings, then post it.", variant: "success" })
          onOpenChange(false)
        },
        onError: (error) => {
          toast({
            title: "Failed to create panel",
            description: describeApiError(error),
            variant: "destructive",
          })
        },
      }
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[560px]">
        <DialogHeader>
          <DialogTitle>New panel</DialogTitle>
          <DialogDescription>Pick a channel and a title, then add role mappings after creating it.</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <Field label="Channel" required>
            {(p) =>
              channelsQuery.isPending ? (
                <Skeleton className="h-9 w-full" />
              ) : (
                <Select {...p} value={channelId} onChange={(e) => setChannelId(e.target.value)}>
                  <option value="">Select a channel…</option>
                  {(channelsQuery.data ?? []).map((c) => (
                    <option key={c.id} value={c.id}>
                      #{c.name}
                    </option>
                  ))}
                </Select>
              )
            }
          </Field>
          <Field label="Embed title" required>
            {(p) => <Input {...p} value={title} onChange={(e) => setTitle(e.target.value)} maxLength={256} />}
          </Field>
          <div className="flex items-start justify-between gap-4">
            <div>
              <Label htmlFor="new-exclusive">Unique role</Label>
              <p className="text-xs text-muted-foreground">Picking one role removes any other from this panel.</p>
            </div>
            <Switch id="new-exclusive" checked={exclusive} onCheckedChange={setExclusive} />
          </div>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit} loading={createPanel.isPending}>
              {createPanel.isPending ? "Creating" : "Create panel"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function PanelCard({
  panel,
  defaultOpen,
  knownRoleIds,
}: {
  panel: ReactionRolePanel
  defaultOpen: boolean
  /** Live guild role ids, or null while unknown (loading / failed). */
  knownRoleIds: Set<string> | null
}) {
  const updatePanel = useUpdateReactionRolePanel()
  const deletePanel = useDeleteReactionRolePanel()
  const postPanel = usePostReactionRolePanel()
  const { toast } = useToast()
  const bot = useBotOffline()
  const [open, setOpen] = useState(defaultOpen)
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleDraft, setTitleDraft] = useState(panel.title)
  const [addingEntry, setAddingEntry] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)

  const isExclusive = Boolean(panel.exclusive)
  const isPosted = panel.message_id !== null
  const deletedCount = knownRoleIds ? panel.entries.filter((e) => !knownRoleIds.has(e.role_id)).length : 0

  const failToast = (title: string) => (error: unknown) =>
    toast({ title, description: describeApiError(error), variant: "destructive" })

  function handleToggleExclusive() {
    updatePanel.mutate({ panelId: panel.id, exclusive: !isExclusive }, { onError: failToast("Failed to update panel") })
  }

  function handleSaveTitle() {
    const trimmed = titleDraft.trim()
    if (!trimmed || trimmed === panel.title) {
      setEditingTitle(false)
      return
    }
    updatePanel.mutate(
      { panelId: panel.id, title: trimmed },
      { onSuccess: () => setEditingTitle(false), onError: failToast("Failed to update panel") }
    )
  }

  function handlePost() {
    postPanel.mutate(panel.id, {
      onSuccess: () => toast({ title: isPosted ? "Panel re-posted" : "Panel posted", variant: "success" }),
      onError: failToast("Failed to post panel"),
    })
  }

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="flex items-center gap-3 py-4">
        <button
          type="button"
          aria-expanded={open}
          aria-label={open ? "Collapse panel" : "Expand panel"}
          onClick={() => setOpen((o) => !o)}
          className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </button>
        <div className="min-w-0 flex-1">
          {editingTitle ? (
            <form
              className="flex items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                handleSaveTitle()
              }}
            >
              <Input aria-label="Panel title" value={titleDraft} onChange={(e) => setTitleDraft(e.target.value)} maxLength={256} autoFocus className="h-[30px] flex-1" />
              <Button type="submit" size="sm" loading={updatePanel.isPending}>
                Save
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => {
                  setTitleDraft(panel.title)
                  setEditingTitle(false)
                }}
              >
                Cancel
              </Button>
            </form>
          ) : (
            <div className="flex items-center gap-1">
              <h2 className="truncate text-md font-semibold text-foreground">{panel.title}</h2>
              <Button variant="ghost" size="icon-sm" aria-label="Rename panel" onClick={() => setEditingTitle(true)}>
                <Pencil />
              </Button>
            </div>
          )}
          <p className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
            <span className="rounded-md bg-muted px-1.5 py-0.5 text-xs">
              <ResolvedChannel channelId={panel.channel_id} />
            </span>
            <Badge variant={isPosted ? "success" : "default"}>{isPosted ? "Posted" : "Not posted"}</Badge>
            {isExclusive ? <Badge variant="accent">Unique role</Badge> : null}
            {deletedCount > 0 ? (
              <Badge variant="destructive" title="These roles no longer exist in Discord; their buttons do nothing. Remove the mappings.">
                <TriangleAlert /> {deletedCount} deleted {deletedCount === 1 ? "role" : "roles"}
              </Badge>
            ) : null}
            <span>{panel.entries.length} {panel.entries.length === 1 ? "role" : "roles"}</span>
          </p>
        </div>
      </CardHeader>

      {open ? (
        <>
          <CardContent className="flex flex-col gap-4 border-t border-border py-4">
            <div className="flex items-center justify-between gap-4">
              <div>
                <Label htmlFor={`excl-${panel.id}`}>Unique role</Label>
                <p className="text-xs text-muted-foreground">Picking one role removes any other from this panel.</p>
              </div>
              <Switch id={`excl-${panel.id}`} checked={isExclusive} onCheckedChange={handleToggleExclusive} disabled={updatePanel.isPending} />
            </div>

            {panel.entries.length === 0 ? (
              <p className="rounded-lg border border-dashed border-border p-4 text-center text-sm text-muted-foreground">No role mappings yet. Add the first one below.</p>
            ) : (
              <ul className="flex flex-col divide-y divide-border rounded-lg border border-border">
                {panel.entries.map((entry) => (
                  <EntryRow key={entry.id} panelId={panel.id} entry={entry} roleDeleted={knownRoleIds !== null && !knownRoleIds.has(entry.role_id)} />
                ))}
              </ul>
            )}

            {addingEntry ? (
              <AddEntryForm panelId={panel.id} onDone={() => setAddingEntry(false)} />
            ) : (
              <Button size="sm" variant="secondary" className="w-fit" onClick={() => setAddingEntry(true)}>
                <Plus /> Add role
              </Button>
            )}
          </CardContent>
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-5 py-3">
            <Button
              size="sm"
              variant="secondary"
              onClick={handlePost}
              loading={postPanel.isPending}
              disabled={panel.entries.length === 0 || bot.offline}
              title={bot.reason ?? (isPosted ? "Delete the old message and send a new one" : "Send this panel to Discord")}
            >
              <Send /> {postPanel.isPending ? "Posting" : isPosted ? "Re-post to channel" : "Post to channel"}
            </Button>
            <Button size="sm" variant="destructive-outline" onClick={() => setConfirmDelete(true)} disabled={deletePanel.isPending || bot.offline} title={bot.reason}>
              <Trash2 /> Delete panel
            </Button>
          </div>
        </>
      ) : null}

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete panel "${panel.title}"?`}
        description="This also deletes the live Discord message, if there is one. Members keep any roles they already picked."
        confirmLabel="Delete panel"
        confirmIcon={<Trash2 />}
        onConfirm={() =>
          deletePanel.mutate(panel.id, {
            onSuccess: () => toast({ title: "Panel deleted", variant: "success" }),
            onError: failToast("Failed to delete panel"),
          })
        }
      />
    </Card>
  )
}

function EntryRow({
  panelId,
  entry,
  roleDeleted,
}: {
  panelId: number
  entry: ReactionRolePanel["entries"][number]
  /** The mapped role is gone from the guild's live role list. */
  roleDeleted: boolean
}) {
  const removeEntry = useRemoveReactionRoleEntry()
  const addEntry = useAddReactionRoleEntry()
  const { toast } = useToast()

  function handleRemove() {
    removeEntry.mutate(
      { panelId, entryId: entry.id },
      {
        onSuccess: () =>
          toast({
            title: "Role mapping removed",
            description: `${entry.emoji}${entry.label ? ` ${entry.label}` : ""}`,
            action: {
              label: "Undo",
              onClick: () =>
                // role_id is a Discord snowflake string: passed through unchanged.
                addEntry.mutate(
                  { panelId, emoji: entry.emoji, role_id: entry.role_id, label: entry.label },
                  {
                    onSuccess: () => toast({ title: "Role mapping restored", variant: "success" }),
                    onError: (error) => toast({ title: "Could not restore it", description: describeApiError(error), variant: "destructive" }),
                  }
                ),
            },
          }),
        onError: (error) => {
          toast({
            title: "Failed to remove role mapping",
            description: describeApiError(error),
            variant: "destructive",
          })
        },
      }
    )
  }

  return (
    <li className="flex min-h-12 items-center justify-between gap-3 px-3 py-2">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-base">
        <span className="text-lg">{entry.emoji}</span>
        {entry.label ? <span className="font-medium text-foreground">{entry.label}</span> : null}
        <span className="rounded-md bg-muted px-1.5 py-0.5 text-sm text-muted-foreground">
          <ResolvedRole roleId={entry.role_id} />
        </span>
        {roleDeleted ? (
          <Badge variant="destructive" title="This role was deleted in Discord. The button does nothing; remove this mapping.">
            <TriangleAlert /> Deleted role
          </Badge>
        ) : null}
      </div>
      <Button size="icon-sm" variant="ghost" disabled={removeEntry.isPending} aria-label={`Remove role mapping ${entry.label ?? entry.emoji}`} onClick={handleRemove} className="max-md:size-11">
        <X />
      </Button>
    </li>
  )
}

function AddEntryForm({ panelId, onDone }: { panelId: number; onDone: () => void }) {
  const rolesQuery = useReactionRoleEditorRoles()
  const addEntry = useAddReactionRoleEntry()
  const { toast } = useToast()
  const [emoji, setEmoji] = useState("")
  const [roleId, setRoleId] = useState("")
  const [label, setLabel] = useState("")

  // The picked role can vanish live (deleted in Discord) or become blocked: treat it as unpicked.
  const pickedRoleId = roleId && rolesQuery.data?.some((r) => r.id === roleId && !r.blocked_reason) ? roleId : ""
  const blockedCount = (rolesQuery.data ?? []).filter((r) => !!r.blocked_reason && !r.managed).length
  const canSubmit = emoji.trim().length > 0 && pickedRoleId.length > 0 && !addEntry.isPending

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    // roleId is a Discord snowflake: pass the string through unchanged, never Number().
    addEntry.mutate(
      { panelId, emoji: emoji.trim(), role_id: pickedRoleId, label: label.trim() || null },
      {
        onSuccess: () => {
          setEmoji("")
          setRoleId("")
          setLabel("")
          onDone()
        },
        onError: (error) => {
          toast({
            title: "Failed to add role mapping",
            description: describeApiError(error),
            variant: "destructive",
          })
        },
      }
    )
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3 rounded-lg border border-border bg-muted/40 p-3">
      <div className="grid gap-3 sm:grid-cols-[80px_minmax(0,1fr)_minmax(0,1fr)]">
        <Field label="Emoji" required>
          {(p) => <Input {...p} value={emoji} onChange={(e) => setEmoji(e.target.value)} placeholder="🎨" />}
        </Field>
        <Field
          label="Role"
          required
          hint={blockedCount > 0 ? "Staff roles, roles with bot permissions or moderator powers, and roles above yours can't be self-roles." : undefined}
        >
          {(p) =>
            rolesQuery.isPending ? (
              <Skeleton className="h-9 w-full" />
            ) : rolesQuery.isError ? (
              <p className="flex h-9 items-center gap-2 text-sm text-danger">
                Could not load roles.
                <Button type="button" size="sm" variant="link" onClick={() => void rolesQuery.refetch()}>
                  Retry
                </Button>
              </p>
            ) : (
              <Select {...p} value={pickedRoleId} onChange={(e) => setRoleId(e.target.value)}>
                <option value="">Select a role…</option>
                {(rolesQuery.data ?? []).map((r) => {
                  // The bot marks roles this user may not make self-assignable
                  // (staff/granted, moderator perms, too high, managed); it
                  // re-checks on add, this is only a hint.
                  const blocked = r.managed || !!r.blocked_reason
                  return (
                    <option key={r.id} value={r.id} disabled={blocked} title={r.blocked_reason ?? undefined}>
                      {r.name}
                      {r.managed ? " (managed)" : r.blocked_reason ? " (not allowed)" : ""}
                    </option>
                  )
                })}
              </Select>
            )
          }
        </Field>
        <Field label="Button label" hint="Optional.">
          {(p) => <Input {...p} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Button text" />}
        </Field>
      </div>
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={!canSubmit} loading={addEntry.isPending}>
          {addEntry.isPending ? "Adding" : "Add"}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
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
