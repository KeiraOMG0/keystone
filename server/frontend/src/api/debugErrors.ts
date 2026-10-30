import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"

/*
 * Owner-only Debug > Errors (/api/debug/errors, server/routes/debug.py). Every
 * route answers 404 to anyone but OWNER_ID. "browser" and "server" reports are
 * stored by the dashboard backend (30 days); "bot" errors come from the bot's
 * in-memory ring since its last start. Types live here (only consumer).
 */

export type ErrorSource = "browser" | "server" | "bot"

export interface ErrorGroup {
  id: string
  source: ErrorSource
  kind: string
  name: string
  message: string
  count: number
  first_seen: string
  last_seen: string
  paths: string[]
  user_agents: string[]
  users: { id: string; username: string | null }[]
  sample: {
    stack: string | null
    component_stack: string | null
    path: string | null
    user_agent: string | null
    user_id: string | null
    username: string | null
    logger: string | null
  }
}

export interface ErrorsResponse {
  groups: ErrorGroup[]
  bot: { available: boolean; started_at: string | null }
  retention_days: number
}

const noRetryOn404 = (count: number, error: unknown) =>
  !(error && typeof error === "object" && "status" in error && (error as { status: number }).status === 404) && count < 2

const ERRORS_KEY = ["debug", "errors"] as const

export function useDebugErrors(enabled: boolean) {
  return useQuery({
    queryKey: ERRORS_KEY,
    queryFn: ({ signal }) => apiFetch<ErrorsResponse>("/api/debug/errors", { signal }),
    enabled,
    retry: noRetryOn404,
    refetchInterval: 30_000,
  })
}

export function useClearDebugErrors() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { source: "all" | ErrorSource; id?: string }) =>
      apiFetch<{ ok: boolean; removed: number; bot_error: string | null }>("/api/debug/errors/clear", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    onSettled: () => qc.invalidateQueries({ queryKey: ERRORS_KEY }),
  })
}
