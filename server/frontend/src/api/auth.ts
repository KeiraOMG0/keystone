import { useMutation, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"

async function logoutRequest(): Promise<{ ok: boolean }> {
  return apiFetch<{ ok: boolean }>("/api/auth/logout", { method: "POST" })
}

/**
 * Wraps POST /api/auth/logout. On success, invalidates the ["me"] query so
 * AuthContext re-derives to "unauthenticated" on its next render — no manual
 * redirect needed, RequireAuth's existing guard handles bouncing to "/".
 */
export function useLogout() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: logoutRequest,
    onSettled: () => {
      // Settled, not just onSuccess: logout is idempotent server-side (per
      // server/auth/oauth.py), so even a network hiccup on the request
      // itself shouldn't leave the frontend showing a stale "authenticated"
      // state — invalidate regardless and let /api/me be the source of truth.
      queryClient.invalidateQueries({ queryKey: ["me"] })
    },
  })
}
