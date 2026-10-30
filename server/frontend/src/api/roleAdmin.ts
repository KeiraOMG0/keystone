import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError, apiFetch } from "@/api/client"
import { useLiveConnected } from "@/live/useLive"
import { useRolesLive } from "@/api/rolesLive"
import type {
  MemberSearchResponse,
  RoleAdminErrorBody,
  RoleAdminOverview,
  RoleMemberChangeResponse,
  RoleMembersResponse,
  RoleMutationResponse,
} from "@/types/api"

/**
 * Staff-only Roles page (/dashboard/roles). Wraps server/routes/role_admin.py
 * (/api/roles/*, require_staff). All role/user ids are snowflake strings.
 *
 * LIVE: the `roles` topic (published by the bot on guild role create/update/
 * delete; topic and publisher are owned by the live-roles work) re-fetches
 * every role-admin query through the SHARED path in api/rolesLive.ts
 * (invalidateRoleQueries covers ["role-admin", ...] plus every other role
 * cache). "role-admin" is also a reconnect-resync root in LiveProvider. While
 * the socket is down the overview polls instead.
 */

const FALLBACK_POLL_MS = 30_000

export const roleAdminKeys = {
  all: ["role-admin"] as const,
  overview: ["role-admin", "overview"] as const,
  members: (roleId: string, offset: number, q: string) => ["role-admin", "members", roleId, offset, q] as const,
  search: (q: string) => ["role-admin", "member-search", q] as const,
}

/**
 * Mount once on the Roles page: any `roles` event re-fetches the role-admin
 * queries and every other role cache (idempotent). Thin wrapper over the shared
 * useRolesLive so all role displays use one invalidation path.
 */
export function useRoleAdminLive(opts?: { enabled?: boolean }) {
  useRolesLive(opts)
}

export function useRoleAdminOverview() {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: roleAdminKeys.overview,
    queryFn: ({ signal }) => apiFetch<RoleAdminOverview>("/api/roles", { signal }),
    refetchInterval: connected ? false : FALLBACK_POLL_MS,
  })
}

export function useRoleMembers(roleId: string | null, offset: number, q: string) {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: roleAdminKeys.members(roleId ?? "", offset, q),
    queryFn: ({ signal }) => {
      const params = new URLSearchParams({ offset: String(offset), limit: "25" })
      if (q) params.set("q", q)
      return apiFetch<RoleMembersResponse>(`/api/roles/${encodeURIComponent(roleId ?? "")}/members?${params}`, { signal })
    },
    enabled: roleId !== null,
    placeholderData: keepPreviousData,
    refetchInterval: connected ? false : FALLBACK_POLL_MS,
  })
}

/** User picker for "add member": only fires at 2+ characters. */
export function useRoleMemberSearch(q: string) {
  const trimmed = q.trim()
  return useQuery({
    queryKey: roleAdminKeys.search(trimmed),
    queryFn: ({ signal }) => apiFetch<MemberSearchResponse>(`/api/roles/member-search?q=${encodeURIComponent(trimmed)}`, { signal }),
    enabled: trimmed.length >= 2,
    staleTime: 15_000,
  })
}

export interface RoleFieldsInput {
  name?: string
  color?: string | null
  hoist?: boolean
  mentionable?: boolean
  permissions?: string[]
  unicode_emoji?: string | null
  icon_base64?: string | null
  clear_icon?: boolean
  internal_permissions?: string[]
  confirm_administrator?: boolean
}

export interface CreateRoleInput extends RoleFieldsInput {
  name: string
  position?: number | null
}

function useRoleMutation<TVars, TResult>(fn: (vars: TVars) => Promise<TResult>) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: roleAdminKeys.all })
      // Internal grants are shown on the Permissions page too.
      void queryClient.invalidateQueries({ queryKey: ["permissions"] })
    },
  })
}

export function useCreateRole() {
  return useRoleMutation((body: CreateRoleInput) =>
    apiFetch<RoleMutationResponse>("/api/roles", { method: "POST", body: JSON.stringify(body) })
  )
}

export function useUpdateRole() {
  return useRoleMutation(({ roleId, body }: { roleId: string; body: RoleFieldsInput }) =>
    apiFetch<RoleMutationResponse>(`/api/roles/${encodeURIComponent(roleId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    })
  )
}

export function useMoveRole() {
  return useRoleMutation(({ roleId, position }: { roleId: string; position: number }) =>
    apiFetch<RoleMutationResponse>(`/api/roles/${encodeURIComponent(roleId)}/move`, {
      method: "POST",
      body: JSON.stringify({ position }),
    })
  )
}

export function useDuplicateRole() {
  return useRoleMutation(
    ({ roleId, name, confirmAdministrator }: { roleId: string; name?: string; confirmAdministrator?: boolean }) =>
      apiFetch<RoleMutationResponse>(`/api/roles/${encodeURIComponent(roleId)}/duplicate`, {
        method: "POST",
        // confirm_administrator: required (typed confirm in the UI) when the copy
        // would carry Administrator; the server returns 400
        // administrator_needs_confirm otherwise.
        body: JSON.stringify({ ...(name ? { name } : {}), ...(confirmAdministrator ? { confirm_administrator: true } : {}) }),
      })
  )
}

export function useDeleteRole() {
  return useRoleMutation(({ roleId, confirmName }: { roleId: string; confirmName: string }) =>
    apiFetch<{ deleted: boolean }>(`/api/roles/${encodeURIComponent(roleId)}`, {
      method: "DELETE",
      body: JSON.stringify({ confirm_name: confirmName }),
    })
  )
}

export function useAddRoleMember() {
  return useRoleMutation(({ roleId, userId }: { roleId: string; userId: string }) =>
    apiFetch<RoleMemberChangeResponse>(
      `/api/roles/${encodeURIComponent(roleId)}/members/${encodeURIComponent(userId)}`,
      { method: "POST" }
    )
  )
}

export function useRemoveRoleMember() {
  return useRoleMutation(({ roleId, userId }: { roleId: string; userId: string }) =>
    apiFetch<RoleMemberChangeResponse>(
      `/api/roles/${encodeURIComponent(roleId)}/members/${encodeURIComponent(userId)}`,
      { method: "DELETE" }
    )
  )
}

/** Human message for a failed role-admin call (server messages are written for staff). */
export function roleErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body as RoleAdminErrorBody | null
    if (body && typeof body === "object" && typeof body.message === "string") return body.message
    if (body && typeof body === "object" && body.error === "internal_api_unavailable") {
      return "The bot is unreachable right now. Try again in a moment."
    }
    if (error.status === 0) return "Network error. Check your connection."
  }
  return error instanceof Error ? error.message : "Something went wrong."
}
