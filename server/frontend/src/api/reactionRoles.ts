import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { ROLES_FALLBACK_POLL_MS } from "@/api/rolesLive"
import { useLiveConnected } from "@/live/useLive"
import type {
  AddReactionRoleEntryResponse,
  CreateReactionRolePanelResponse,
  GuildChannel,
  GuildRole,
  PostReactionRolePanelResponse,
  ReactionRolePanel,
} from "@/types/api"

const PANELS_KEY = ["reaction-role-panels"] as const

function panelQueryKey(panelId: number) {
  return ["reaction-role-panels", panelId] as const
}

async function fetchPanels(): Promise<ReactionRolePanel[]> {
  return apiFetch<ReactionRolePanel[]>("/api/reaction-roles/panels")
}

/** Wraps GET /api/reaction-roles/panels (requires "manage_reaction_roles" server-side). */
export function useReactionRolePanels() {
  return useQuery({
    queryKey: PANELS_KEY,
    queryFn: fetchPanels,
  })
}

async function fetchPanel(panelId: number): Promise<ReactionRolePanel> {
  return apiFetch<ReactionRolePanel>(`/api/reaction-roles/panels/${panelId}`)
}

/** Wraps GET /api/reaction-roles/panels/{panelId} (requires "manage_reaction_roles" server-side). */
export function useReactionRolePanel(panelId: number | null) {
  return useQuery({
    queryKey: panelQueryKey(panelId ?? -1),
    queryFn: () => fetchPanel(panelId as number),
    enabled: panelId !== null,
  })
}

async function fetchChannels(): Promise<GuildChannel[]> {
  return apiFetch<GuildChannel[]>("/api/channels")
}

/** Wraps GET /api/channels — text-channel picker for panel creation. */
export function useGuildChannels() {
  return useQuery({
    queryKey: ["guild-channels"],
    queryFn: fetchChannels,
  })
}

/**
 * A role in the Self-roles picker. `blocked_reason` is set (by the bot,
 * services/self_role_policy.py) when the signed-in user may not make this
 * role self-assignable: staff/granted roles, moderator permissions, at or
 * above the bot's or their own top role, managed or @everyone. Only a UI
 * hint; the bot re-checks on add and answers 409 role_not_allowed.
 */
export interface SelfRoleCandidate extends GuildRole {
  blocked_reason?: string | null
}

async function fetchReactionRoleEditorRoles(): Promise<SelfRoleCandidate[]> {
  return apiFetch<SelfRoleCandidate[]>("/api/reaction-roles/roles")
}

/**
 * Wraps GET /api/reaction-roles/roles — role picker for the entry editor.
 * Named distinctly from api/permissions.ts's useGuildRoles (which hits
 * /api/permissions/roles, gated on full staff) since this page only
 * requires "manage_reaction_roles" and must not accidentally depend on a
 * stricter permission than the rest of this page needs.
 */
export function useReactionRoleEditorRoles() {
  // Kept fresh by the live `roles` topic (useRolesLive in api/rolesLive.ts,
  // mounted by SelfRolesPage); poll only while the socket is down.
  const connected = useLiveConnected()
  return useQuery({
    queryKey: ["reaction-role-roles"],
    queryFn: fetchReactionRoleEditorRoles,
    refetchInterval: connected ? false : ROLES_FALLBACK_POLL_MS,
  })
}

function invalidatePanels(queryClient: ReturnType<typeof useQueryClient>, panelId?: number) {
  queryClient.invalidateQueries({ queryKey: PANELS_KEY })
  if (panelId !== undefined) {
    queryClient.invalidateQueries({ queryKey: panelQueryKey(panelId) })
  }
}

export interface CreatePanelInput {
  /** Discord snowflake — string end-to-end, never Number(). */
  channel_id: string
  title: string
  exclusive: boolean
}

async function createPanelRequest(input: CreatePanelInput): Promise<CreateReactionRolePanelResponse> {
  return apiFetch<CreateReactionRolePanelResponse>("/api/reaction-roles/panels", {
    method: "POST",
    body: JSON.stringify(input),
  })
}

/** Wraps POST /api/reaction-roles/panels. Invalidates the panel list on success. */
export function useCreateReactionRolePanel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: createPanelRequest,
    onSuccess: () => invalidatePanels(queryClient),
  })
}

export interface UpdatePanelInput {
  panelId: number
  title?: string
  exclusive?: boolean
}

async function updatePanelRequest({ panelId, ...body }: UpdatePanelInput): Promise<{ success: boolean }> {
  return apiFetch<{ success: boolean }>(`/api/reaction-roles/panels/${panelId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  })
}

/**
 * Wraps PATCH /api/reaction-roles/panels/{panelId}. The backend
 * (services.reaction_role_service.ReactionRoleService.update_panel)
 * automatically edits the live Discord message in place if one exists —
 * no separate "post" action is needed after renaming a panel or toggling
 * "unique role" on an already-posted panel.
 */
export function useUpdateReactionRolePanel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: updatePanelRequest,
    onSuccess: (_data, variables) => invalidatePanels(queryClient, variables.panelId),
  })
}

async function deletePanelRequest(panelId: number): Promise<{ removed: boolean }> {
  return apiFetch<{ removed: boolean }>(`/api/reaction-roles/panels/${panelId}`, {
    method: "DELETE",
  })
}

/** Wraps DELETE /api/reaction-roles/panels/{panelId}. Also deletes the live Discord message, if any. */
export function useDeleteReactionRolePanel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: deletePanelRequest,
    onSuccess: () => invalidatePanels(queryClient),
  })
}

async function postPanelRequest(panelId: number): Promise<PostReactionRolePanelResponse> {
  return apiFetch<PostReactionRolePanelResponse>(`/api/reaction-roles/panels/${panelId}/post`, {
    method: "POST",
  })
}

/**
 * Wraps POST /api/reaction-roles/panels/{panelId}/post — sends the panel to
 * Discord for the first time, or explicitly re-sends it (deleting the old
 * message and posting a new one, which changes the message link). Ordinary
 * edits after the first post do NOT need this; see useUpdateReactionRolePanel.
 */
export function usePostReactionRolePanel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: postPanelRequest,
    onSuccess: (_data, panelId) => invalidatePanels(queryClient, panelId),
  })
}

export interface AddEntryInput {
  panelId: number
  emoji: string
  /** Discord snowflake — string end-to-end, never Number(). */
  role_id: string
  label?: string | null
}

async function addEntryRequest({ panelId, ...body }: AddEntryInput): Promise<AddReactionRoleEntryResponse> {
  return apiFetch<AddReactionRoleEntryResponse>(`/api/reaction-roles/panels/${panelId}/entries`, {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/**
 * Wraps POST /api/reaction-roles/panels/{panelId}/entries. The backend
 * automatically syncs the live Discord message in place if the panel has
 * already been posted.
 */
export function useAddReactionRoleEntry() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: addEntryRequest,
    onSuccess: (_data, variables) => invalidatePanels(queryClient, variables.panelId),
  })
}

export interface RemoveEntryInput {
  panelId: number
  entryId: number
}

async function removeEntryRequest({ panelId, entryId }: RemoveEntryInput): Promise<{ removed: boolean }> {
  return apiFetch<{ removed: boolean }>(`/api/reaction-roles/panels/${panelId}/entries/${entryId}`, {
    method: "DELETE",
  })
}

/**
 * Wraps DELETE /api/reaction-roles/panels/{panelId}/entries/{entryId}. The
 * backend automatically syncs the live Discord message in place if the
 * panel has already been posted.
 */
export function useRemoveReactionRoleEntry() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: removeEntryRequest,
    onSuccess: (_data, variables) => invalidatePanels(queryClient, variables.panelId),
  })
}
