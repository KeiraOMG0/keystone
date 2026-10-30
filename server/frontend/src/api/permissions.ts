import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { ROLES_FALLBACK_POLL_MS } from "@/api/rolesLive"
import { useLiveConnected } from "@/live/useLive"
import type { AddGrantResponse, GrantsResponse, RevokeGrantResponse, RolesResponse } from "@/types/api"

const rolesQueryKey = ["permissions", "roles"] as const
const grantsQueryKey = ["permissions", "grants"] as const

async function fetchRoles(signal?: AbortSignal): Promise<RolesResponse> {
  return apiFetch<RolesResponse>("/api/permissions/roles", { signal })
}

/** Wraps GET /api/permissions/roles (requires require_staff server-side —
 *  see server/permissions/guards.py). Live Discord roles for this guild,
 *  highest position first, @everyone excluded. Kept fresh by the live
 *  `roles` topic (useRolesLive, mounted by PermissionsPage); polls only
 *  while the socket is down. */
export function useGuildRoles() {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: rolesQueryKey,
    queryFn: ({ signal }) => fetchRoles(signal),
    refetchInterval: connected ? false : ROLES_FALLBACK_POLL_MS,
  })
}

async function fetchGrants(signal?: AbortSignal): Promise<GrantsResponse> {
  return apiFetch<GrantsResponse>("/api/permissions/grants", { signal })
}

/** Wraps GET /api/permissions/grants (requires require_staff server-side).
 *  Returns every current role->permission grant plus the full
 *  VALID_PERMISSIONS list (the matrix's column set). */
export function usePermissionGrants() {
  return useQuery({
    queryKey: grantsQueryKey,
    queryFn: ({ signal }) => fetchGrants(signal),
  })
}

interface AddGrantInput {
  /** Discord snowflake — string end-to-end, never Number(). */
  role_id: string
  permission: string
}

async function addGrantRequest(body: AddGrantInput): Promise<AddGrantResponse> {
  return apiFetch<AddGrantResponse>("/api/permissions/grants", {
    method: "POST",
    body: JSON.stringify(body),
  })
}

/**
 * Wraps POST /api/permissions/grants. On success, invalidates the grants
 * query so the matrix re-fetches with the new grant included — mirrors
 * useAddWarning's mutation/invalidate pattern (server/frontend/src/api/
 * moderation.ts).
 */
export function useAddGrant() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: addGrantRequest,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: grantsQueryKey })
    },
  })
}

interface RevokeGrantInput {
  /** Discord snowflake — string end-to-end, never Number(). */
  role_id: string
  permission: string
}

async function revokeGrantRequest(body: RevokeGrantInput): Promise<RevokeGrantResponse> {
  return apiFetch<RevokeGrantResponse>("/api/permissions/grants", {
    method: "DELETE",
    body: JSON.stringify(body),
  })
}

/** Wraps DELETE /api/permissions/grants. On success, invalidates the grants
 *  query — mirrors useRemoveWarning's pattern. */
export function useRevokeGrant() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: revokeGrantRequest,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: grantsQueryKey })
    },
  })
}
