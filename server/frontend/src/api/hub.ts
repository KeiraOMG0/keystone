import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch, ApiError } from "@/api/client"

/** GET /api/hub (server/routes/hub.py -> services/member_hub_service.py). All Discord ids are strings. */
export type HubStatus = "member" | "trap" | "banned" | "not_member"

export interface HubRole {
  id: string
  name: string
  color: string | null
}

export interface HubSelfRole extends HubRole {
  emoji: string | null
  label: string | null
  has: boolean
}

export interface HubPanel {
  panel_id: number
  title: string
  exclusive: boolean
  channel_id: string | null
  jump_url: string | null
  roles: HubSelfRole[]
}

export interface HubResponse {
  status: HubStatus
  user: { id: string | null; username: string | null; display_name: string | null; avatar_url: string | null }
  server: {
    id: string
    name: string
    icon_url: string | null
    member_count: number
    online_count: number | null
    boost_count: number
    boost_tier: number
  }
  /** Sections whose feature isn't loaded or failed (left out, never fatal). */
  unavailable: string[]
  // status === "member"
  profile?: {
    display_name: string
    joined_at: string | null
    created_at: string | null
    roles: HubRole[]
    top_role_color: string | null
    booster_since: string | null
    timed_out_until: string | null
    /** null = this server has no verification roles configured. */
    verified: boolean | null
  }
  standing?: {
    level: "good" | "warned" | "timed_out"
    warning_count: number
    warnings: { id: number; created_at: string | null }[]
    timed_out_until: string | null
  } | null
  self_roles?: HubPanel[]
  birthday?: {
    mine: { month: number; day: number; label: string; timezone: string | null } | null
    nearest: {
      offset: number
      month: number
      day: number
      label: string
      people: { id: string; display_name: string; avatar_url: string | null }[]
    }[]
  } | null
  tickets?: { id: number; number: number | null; channel_name: string | null; opened_at: string | null; jump_url: string }[]
  events?: {
    id: string
    name: string
    description: string | null
    start_time: string | null
    end_time: string | null
    active: boolean
    location: string | null
    interested: number | null
    url: string
    image_url: string | null
  }[]
  qotd?: { question: string; posted_at: string | null; jump_url: string | null } | null
  music?: { title: string; paused: boolean; voice_channel: string | null; listeners: number | null; queue_total: number } | null
  // status !== "member"
  trap?: {
    eligible: boolean
    appeal: { status: string; submitted_at: string | null; reviewed_at: string | null } | null
  } | null
  banned?: boolean | null
}

export const HUB_QUERY_KEY = ["hub"] as const

/** True for the "no session" answer: the page shows the sign-in gate. */
export function isSignedOut(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}

export function useHub() {
  return useQuery({
    queryKey: HUB_QUERY_KEY,
    queryFn: ({ signal }) => apiFetch<HubResponse>("/api/hub", { signal }),
    // A 401 is a verdict (show the sign-in gate); anything else is retried a few times.
    retry: (count, error) => !isSignedOut(error) && count < 3,
    refetchOnWindowFocus: true,
    staleTime: 30_000,
  })
}

export interface ToggleVars {
  panel: HubPanel
  role: HubSelfRole
  assign: boolean
}

/**
 * POST /api/hub/self-roles. Optimistic: the chip flips at once (and, on an
 * exclusive panel, the panel's other roles turn off); a refusal rolls back.
 */
export function useToggleSelfRole() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ panel, role, assign }: ToggleVars) =>
      apiFetch<{ result: string; role_id: string; removed_role_ids: string[] }>("/api/hub/self-roles", {
        method: "POST",
        body: JSON.stringify({ panel_id: panel.panel_id, role_id: role.id, assign }),
      }),
    onMutate: async ({ panel, role, assign }) => {
      await queryClient.cancelQueries({ queryKey: HUB_QUERY_KEY })
      const previous = queryClient.getQueryData<HubResponse>(HUB_QUERY_KEY)
      if (previous?.self_roles) {
        queryClient.setQueryData<HubResponse>(HUB_QUERY_KEY, {
          ...previous,
          self_roles: previous.self_roles.map((p) =>
            p.panel_id !== panel.panel_id
              ? p
              : {
                  ...p,
                  roles: p.roles.map((r) =>
                    r.id === role.id ? { ...r, has: assign } : assign && p.exclusive ? { ...r, has: false } : r,
                  ),
                },
          ),
        })
      }
      return { previous }
    },
    onError: (_error, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(HUB_QUERY_KEY, context.previous)
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: HUB_QUERY_KEY })
    },
  })
}
