import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import type {
  FortnitePostNowResponse,
  FortniteStatusResponse,
  GuildChannel,
  GuildRole,
  UpdateFortniteConfigInput,
} from "@/types/api"

const statusKey = ["fortnite", "status"] as const

/** GET /api/fortnite/status (perm manage_schedules). No live topic: config only
 *  changes from this page or /fortnite commands, so a slow poll is enough. */
export function useFortniteStatus() {
  return useQuery({
    queryKey: statusKey,
    queryFn: () => apiFetch<FortniteStatusResponse>("/api/fortnite/status"),
    refetchInterval: 60_000,
  })
}

/** PUT /api/fortnite/config. Snowflakes stay strings. */
export function useUpdateFortniteConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateFortniteConfigInput) =>
      apiFetch<FortniteStatusResponse>("/api/fortnite/config", { method: "PUT", body: JSON.stringify(input) }),
    onSuccess: (data) => queryClient.setQueryData(statusKey, data),
  })
}

/** POST /api/fortnite/post-now: posts today's shop to the configured channel (dedupe untouched). */
export function useFortnitePostNow() {
  return useMutation({
    mutationFn: () => apiFetch<FortnitePostNowResponse>("/api/fortnite/post-now", { method: "POST" }),
  })
}

export function useFortniteChannels() {
  return useQuery({
    queryKey: ["fortnite", "channels"],
    queryFn: () => apiFetch<GuildChannel[]>("/api/fortnite/channels"),
    staleTime: 60_000,
  })
}

export function useFortniteRoles() {
  return useQuery({
    queryKey: ["fortnite", "roles"],
    queryFn: () => apiFetch<GuildRole[]>("/api/fortnite/roles"),
    staleTime: 60_000,
  })
}
