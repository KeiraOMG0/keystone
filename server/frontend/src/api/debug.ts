import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import type {
  AppealTestCase,
  AppealTestState,
  DebugActionsResponse,
  DebugHealthResponse,
  DebugRunInput,
  DebugRunResult,
  FortniteShopPreview,
  GuildChannel,
} from "@/types/api"

/*
 * Hidden owner-only Debug page (/api/debug/*). Every route answers 404 to
 * anyone but OWNER_ID, so none of these retry on 404.
 */

const noRetryOn404 = (count: number, error: unknown) =>
  !(error && typeof error === "object" && "status" in error && (error as { status: number }).status === 404) && count < 2

export function useDebugActions(enabled: boolean) {
  return useQuery({
    queryKey: ["debug", "actions"],
    queryFn: () => apiFetch<DebugActionsResponse>("/api/debug/actions"),
    enabled,
    retry: noRetryOn404,
  })
}

export function useDebugChannels(enabled: boolean) {
  return useQuery({
    queryKey: ["debug", "channels"],
    queryFn: () => apiFetch<GuildChannel[]>("/api/debug/channels"),
    enabled,
    retry: noRetryOn404,
    staleTime: 60_000,
  })
}

/** Health snapshot; refreshes every 15s while the page is open. */
export function useDebugHealth(enabled: boolean) {
  return useQuery({
    queryKey: ["debug", "health"],
    queryFn: () => apiFetch<DebugHealthResponse>("/api/debug/health"),
    enabled,
    retry: noRetryOn404,
    refetchInterval: 15_000,
  })
}

export function useRunDebugAction() {
  return useMutation({
    mutationFn: (input: DebugRunInput) =>
      apiFetch<DebugRunResult>("/api/debug/run", { method: "POST", body: JSON.stringify(input) }),
  })
}

const appealTestKey = ["debug", "appeal-test"] as const

/** Honeypot appeal-flow test cases (TEST rows, no real ban). */
export function useAppealTestCases(enabled: boolean) {
  return useQuery({
    queryKey: appealTestKey,
    queryFn: ({ signal }) => apiFetch<{ cases: AppealTestCase[] }>("/api/debug/appeal-test", { signal }),
    enabled,
    retry: noRetryOn404,
  })
}

export function useCreateAppealTestCase() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { user_id: string; state: AppealTestState }) =>
      apiFetch<{ catch_id: number; appeal_id: number | null; state: AppealTestState; username: string | null }>(
        "/api/debug/appeal-test",
        { method: "POST", body: JSON.stringify(body) }
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: appealTestKey })
      qc.invalidateQueries({ queryKey: ["honeypot", "appeals"] })
    },
  })
}

export function useClearAppealTestCases() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () =>
      apiFetch<{ catches: number; appeals: number }>("/api/debug/appeal-test/clear", { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: appealTestKey })
      qc.invalidateQueries({ queryKey: ["honeypot", "appeals"] })
    },
  })
}

/** Parsed shop data for the JSON viewer (manual fetch). */
export function useFortnitePreview() {
  return useMutation({
    mutationFn: (refresh: boolean) =>
      apiFetch<FortniteShopPreview>(`/api/debug/fortnite/preview?refresh=${refresh ? "true" : "false"}`),
  })
}
