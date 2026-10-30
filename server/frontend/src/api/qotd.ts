import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError, apiFetch } from "@/api/client"
import type {
  GenerateQotdResponse,
  GuildChannel,
  GuildRole,
  PostQotdInput,
  PostQotdResponse,
  QotdConfigResponse,
  QotdFallbackQuestionsResponse,
  QotdHistoryResponse,
  QotdModelsResponse,
  UpsertQotdConfigInput,
} from "@/types/api"

const configKey = ["qotd", "config"] as const
const historyKey = (limit: number) => ["qotd", "history", limit] as const

async function fetchConfig(): Promise<QotdConfigResponse> {
  return apiFetch<QotdConfigResponse>("/api/qotd/config")
}

/** Wraps GET /api/qotd/config (requires "manage_qotd" server-side). Includes
 *  `last_posted_at` and whether today's staff-lounge approval is still
 *  pending (cogs/qotd.py's automatic scheduler). */
export function useQotdConfig() {
  return useQuery({
    queryKey: configKey,
    queryFn: fetchConfig,
  })
}

async function upsertConfigRequest(input: UpsertQotdConfigInput): Promise<QotdConfigResponse["config"]> {
  const result = await apiFetch<{ config: QotdConfigResponse["config"] }>("/api/qotd/config", {
    method: "PUT",
    body: JSON.stringify(input),
  })
  return result.config
}

/**
 * Wraps PUT /api/qotd/config — edits the SAME qotd_config row
 * cogs/qotd.py's `/qotd setchannel` / `/qotd setapprovalchannel` /
 * `/qotd setmodel` slash commands write to, including the daily post_hour /
 * post_minute the automatic scheduler checks every 60 seconds. Invalidates
 * the config query on success.
 */
export function useUpdateQotdConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: upsertConfigRequest,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: configKey }),
  })
}

async function fetchHistory(limit: number): Promise<QotdHistoryResponse> {
  return apiFetch<QotdHistoryResponse>(`/api/qotd/history?limit=${encodeURIComponent(String(limit))}`)
}

/** Wraps GET /api/qotd/history?limit=N (requires "manage_qotd" server-side). Newest first. */
export function useQotdHistory(limit = 50) {
  return useQuery({
    queryKey: historyKey(limit),
    queryFn: () => fetchHistory(limit),
  })
}

async function fetchModels(): Promise<QotdModelsResponse> {
  return apiFetch<QotdModelsResponse>("/api/qotd/models")
}

/** Wraps GET /api/qotd/models — the same AVAILABLE_MODELS catalogue
 *  `/qotd setmodel`'s select menu uses. Static list, safe to cache
 *  indefinitely for the session. */
export function useQotdModels() {
  return useQuery({
    queryKey: ["qotd", "models"],
    queryFn: fetchModels,
    staleTime: Infinity,
  })
}

async function fetchFallbackQuestions(): Promise<QotdFallbackQuestionsResponse> {
  return apiFetch<QotdFallbackQuestionsResponse>("/api/qotd/fallback-questions")
}

/** Wraps GET /api/qotd/fallback-questions — the hardcoded FALLBACK_QUESTIONS
 *  pool (services/qotd_service.py), read-only display only. Static for the
 *  process lifetime of the bot (a Python constant, not a DB table). */
export function useQotdFallbackQuestions() {
  return useQuery({
    queryKey: ["qotd", "fallback-questions"],
    queryFn: fetchFallbackQuestions,
    staleTime: Infinity,
  })
}

async function fetchChannels(): Promise<GuildChannel[]> {
  return apiFetch<GuildChannel[]>("/api/qotd/channels")
}

/** Wraps GET /api/qotd/channels — text-channel picker for the public/
 *  approval channel fields, gated on "manage_qotd" (its own endpoint, not
 *  the "manage_reaction_roles"-gated /api/channels). */
export function useQotdChannels() {
  return useQuery({
    queryKey: ["qotd", "channels"],
    queryFn: fetchChannels,
  })
}

async function fetchRoles(): Promise<GuildRole[]> {
  return apiFetch<GuildRole[]>("/api/qotd/roles")
}

/** Wraps GET /api/qotd/roles — role picker for the public/staff ping-role
 *  fields, gated on "manage_qotd". */
export function useQotdRoles() {
  return useQuery({
    queryKey: ["qotd", "roles"],
    queryFn: fetchRoles,
  })
}

/** A bot-side generation job (POST /api/qotd/generate, GET /api/qotd/generate/{job_id}). */
interface QotdGenerationJob {
  job_id: string
  status: "running" | "done" | "failed"
  question: string | null
  source: string | null
  errors: string[]
  error: string | null
  reused?: boolean
}

const POLL_INTERVAL_MS = 2000
/** The bot bounds a generation at ~150 s and then falls back; this is only a safety net. */
const MAX_WAIT_MS = 4 * 60 * 1000
/** Poll failures tolerated in a row (a brief dashboard/bot blip) before giving up. */
const MAX_POLL_FAILURES = 3

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException("Aborted", "AbortError"))
      return
    }
    const id = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort)
      resolve()
    }, ms)
    function onAbort() {
      window.clearTimeout(id)
      reject(new DOMException("Aborted", "AbortError"))
    }
    signal?.addEventListener("abort", onAbort, { once: true })
  })
}

/**
 * Generates a question WITHOUT posting it (Ollama + validation, falling back
 * to the hardcoded pool). Starts a job on the bot (fast 202) and polls it
 * every 2 s, so no single request comes near Cloudflare's 100 s limit however
 * slow the model is. Abort `signal` to stop polling (dialog closed, page
 * left); the rejection is then an AbortError (see isAbortError).
 */
export async function generateQotdQuestion(model: string | null, signal?: AbortSignal): Promise<GenerateQotdResponse> {
  let job = await apiFetch<QotdGenerationJob>("/api/qotd/generate", {
    method: "POST",
    body: JSON.stringify({ model }),
    signal,
  })
  const started = Date.now()
  let failures = 0
  while (job.status === "running") {
    if (Date.now() - started > MAX_WAIT_MS) {
      throw new Error("Generating took too long. Try again, or choose a smaller model.")
    }
    await sleep(POLL_INTERVAL_MS, signal)
    try {
      job = await apiFetch<QotdGenerationJob>(`/api/qotd/generate/${encodeURIComponent(job.job_id)}`, { signal })
      failures = 0
    } catch (err) {
      const transient = err instanceof ApiError && err.transient
      failures += 1
      if (!transient || failures >= MAX_POLL_FAILURES) throw err
    }
  }
  if (job.status !== "done" || !job.question) {
    throw new Error(job.error ? `Generation failed: ${job.error}` : "Generation failed. Try again.")
  }
  return { question: job.question, source: job.source ?? "fallback", errors: job.errors ?? [] }
}

/**
 * Mutation wrapper around generateQotdQuestion (no abort; the page keeps it
 * mounted). Mirrors `/qotd preview`. Call again (with the same or a different
 * model) to regenerate before deciding to post.
 */
export function useGenerateQotd() {
  return useMutation({
    mutationFn: (model: string | null) => generateQotdQuestion(model),
  })
}

/** POST /api/qotd/post result. `warning` is set when the message went out but
 *  the history row could not be saved (do not retry: it would post twice). */
export type PostQotdResult = PostQotdResponse & { warning?: string }

async function postRequest(input: PostQotdInput): Promise<PostQotdResult> {
  return apiFetch<PostQotdResult>("/api/qotd/post", {
    method: "POST",
    body: JSON.stringify(input),
  })
}

/**
 * Wraps POST /api/qotd/post — actually sends the QOTD embed to the chosen
 * channel and records a qotd_history row (QotdService.post_qotd, the same
 * method `/qotd now` and the staff-lounge Approve button call). Distinct
 * from useGenerateQotd on purpose: staff must explicitly choose to post
 * after reviewing the generated/edited question, never automatically.
 * Invalidates the history and config (last_posted_at) queries on success.
 */
export function usePostQotd() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: postRequest,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["qotd", "history"] })
      queryClient.invalidateQueries({ queryKey: configKey })
    },
  })
}
