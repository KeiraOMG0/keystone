import { useQuery } from "@tanstack/react-query"
import { apiFetch, ApiError } from "@/api/client"
import { backoffDelay, classifyAuthFailure, type AuthFailure } from "@/api/classify"
import type { MeResponse } from "@/types/api"

async function fetchMe(): Promise<MeResponse> {
  return apiFetch<MeResponse>("/api/me")
}

/** Classify a GET /api/me failure (see api/classify.ts for THE RULE). */
export function classifyMeError(error: unknown): AuthFailure {
  if (error instanceof ApiError) return classifyAuthFailure(error.status, error.body, error.transient)
  return "transient" // not an ApiError: never a reason to log the user out
}

/**
 * Wraps GET /api/me.
 *
 * STAY LOGGED IN: only a real 401 {"error":"unauthenticated"} (or 403
 * not_a_member) is a verdict. EVERY other failure (network error, 502/503/504,
 * Cloudflare 52x/530 HTML pages, non-JSON bodies, 502 internal_api_unavailable
 * while the bot restarts) retries FOREVER with capped exponential backoff plus
 * jitter, so the session heals by itself when the site/bot/tunnel comes back.
 * It also refetches immediately when the browser comes back online, when the
 * tab becomes visible again, and (LiveProvider) on every websocket reconnect.
 */
export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: fetchMe,
    retry: (_failureCount, error) => classifyMeError(error) === "transient",
    retryDelay: (attempt) => backoffDelay(attempt),
    staleTime: 60_000,
    // Try even when navigator.onLine is false (it lies on some networks) and
    // let the retry loop keep going; refetch on regained focus/connectivity.
    networkMode: "always",
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
  })
}
