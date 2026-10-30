import { useMutation, useQuery } from "@tanstack/react-query"
import { ApiError, apiFetch } from "@/api/client"
import type {
  SupervisorLogsResponse,
  SupervisorRestartInput,
  SupervisorRestartResponse,
  SupervisorServiceName,
  SupervisorStatusResponse,
} from "@/types/api"

export const supervisorStatusKey = ["supervisor", "status"] as const

/** 503 {"error":"supervisor_unavailable"}: the stack is not running under supervisor.py. */
export function isSupervisorUnavailable(err: unknown): boolean {
  return err instanceof ApiError && err.status === 503 && (err.body as { error?: string } | null)?.error === "supervisor_unavailable"
}

export function fetchSupervisorStatus() {
  return apiFetch<SupervisorStatusResponse>("/api/supervisor/status")
}

/**
 * GET /api/supervisor/status (staff; works while the bot is down). No live
 * topic: polls every 5s while `enabled` (the Diagnostics card is expanded and
 * the tab is visible), every 15s while the supervisor is unreachable.
 */
export function useSupervisorStatus(enabled: boolean) {
  return useQuery({
    queryKey: supervisorStatusKey,
    queryFn: fetchSupervisorStatus,
    enabled,
    refetchInterval: (query) => (!enabled ? false : isSupervisorUnavailable(query.state.error) ? 15_000 : 5_000),
    refetchIntervalInBackground: false,
    staleTime: 0,
    retry: (count, err) => !(err instanceof ApiError && (err.status === 403 || err.status === 503)) && count < 1,
    placeholderData: (prev) => prev,
  })
}

/** POST /api/supervisor/restart. 202 means queued: the supervisor stops the target ~1.5s later. */
export function useSupervisorRestart() {
  return useMutation({
    mutationFn: (input: SupervisorRestartInput) =>
      apiFetch<SupervisorRestartResponse>("/api/supervisor/restart", { method: "POST", body: JSON.stringify(input) }),
  })
}

/** GET /api/supervisor/logs: the supervisor's in-memory output tail (secrets scrubbed server-side). */
export function useSupervisorLogs(service: SupervisorServiceName | null, enabled: boolean) {
  return useQuery({
    queryKey: ["supervisor", "logs", service],
    queryFn: () => apiFetch<SupervisorLogsResponse>(`/api/supervisor/logs?service=${service}&lines=120`),
    enabled: enabled && !!service,
    refetchInterval: enabled && service ? 5_000 : false,
    refetchIntervalInBackground: false,
    retry: false,
  })
}

// ---------------------------------------------------------------------------
// Weekly log rotation (supervisor.py >= v2.1). Typed here rather than in
// types/api.ts: older supervisors omit `log_rotation` from /status.
// ---------------------------------------------------------------------------

export type LogRotationFileStatus = "rotated" | "empty" | "missing" | "deferred" | "failed" | "compress_failed"

export interface LogRotationFileResult {
  key: string
  file: string
  status: LogRotationFileStatus | string
  bytes: number
  /** Archive file name in `archive_dir` (".log.gz"; ".log" when gzip failed). */
  archive: string | null
  gz_bytes?: number
  error: string | null
}

export interface LogRotationRun {
  trigger: "schedule" | "manual" | string
  requested_by: string | null
  source: string | null
  reason: string | null
  /** Local time with UTC offset. */
  at: string
  finished_at: string | null
  ok: boolean
  files: LogRotationFileResult[]
  deleted: string[]
  error?: string
}

export interface LogRotationStatus {
  schedule: string
  retention_days: number
  archive_dir: string
  running: boolean
  /** Queued by POST /rotate-logs, not yet started by the supervisor's main loop. */
  pending: boolean
  last_run: LogRotationRun | null
  next_run_at: string | null
  overdue: boolean
  /** Files left in place because another process held them open (bot_startup.log: rotated at the next bot start). */
  deferred: string[]
}

/** /status as sent by supervisor.py v2.1+, which adds `log_rotation`. */
export type SupervisorStatusWithRotation = SupervisorStatusResponse & { log_rotation?: LogRotationStatus | null }

/**
 * POST /api/supervisor/rotate-logs (OWNER ONLY; 404 for everyone else). 202
 * means queued: the supervisor starts the rotation within a second and the
 * result appears in /status `log_rotation.last_run`.
 */
export function useSupervisorRotateLogs() {
  return useMutation({
    mutationFn: (input: { reason?: string | null }) =>
      apiFetch<{ queued: boolean; message: string }>("/api/supervisor/rotate-logs", {
        method: "POST",
        body: JSON.stringify(input),
      }),
  })
}
