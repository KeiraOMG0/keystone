import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import type { CachedUserResponse } from "@/types/api"

/**
 * The bot's user cache (user_cache.db): last known profile + membership
 * status of members, including those who left / were kicked / banned.
 * Normal rendering goes through POST /api/resolve (`<ResolvedUser>`), which
 * already falls back to the cache; these are for a member profile page.
 *
 * GET    /api/user-cache/{id}  any bot permission; 404 not_cached, 503 user_cache_unavailable
 * DELETE /api/user-cache/{id}  staff only (require_staff); audited as user_cache_forget
 */
export function fetchCachedUser(userId: string, signal?: AbortSignal): Promise<CachedUserResponse> {
  return apiFetch<CachedUserResponse>(`/api/user-cache/${encodeURIComponent(userId)}`, { signal })
}

export function forgetCachedUser(userId: string): Promise<{ ok: true; removed: boolean; in_guild: boolean }> {
  return apiFetch(`/api/user-cache/${encodeURIComponent(userId)}`, { method: "DELETE" })
}

export function useCachedUser(userId: string | null | undefined) {
  return useQuery({
    queryKey: ["user-cache", userId] as const,
    queryFn: ({ signal }) => fetchCachedUser(userId as string, signal),
    enabled: Boolean(userId),
    retry: false,
  })
}

/** Forget one user's cached profile; drops the cached resolve entry so it re-resolves. */
export function useForgetCachedUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (userId: string) => forgetCachedUser(userId),
    onSuccess: (_data, userId) => {
      queryClient.invalidateQueries({ queryKey: ["user-cache", userId] })
      queryClient.invalidateQueries({ queryKey: ["resolve-single", "users", userId] })
    },
  })
}
