import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError, apiFetch } from "@/api/client"
import { useLiveConnected, useLiveTopic } from "@/live/useLive"
import { invalidateSoon } from "@/live/invalidate"

/* ---- wire types (GET /api/music/*; snowflakes are strings) ---- */

export interface MusicTrack {
  position: number
  title: string
  url: string
  autoplay: boolean
}

export interface MusicState {
  connected: boolean
  voice_channel: { channel_id: string; name: string; listeners: number } | null
  playing: boolean
  paused: boolean
  current: {
    title: string
    url: string
    duration: number | null
    elapsed: number
    requester_id: string | null
  } | null
  queue: MusicTrack[]
  queue_total: number
  volume: number
  loop: boolean
  autoplay: { enabled: boolean; consecutive: number; max: number }
  previous: { title: string; url: string } | null
  history: { title: string; url: string }[]
  announce_channel: { channel_id: string; name: string } | null
  inactivity_timeout: number
}

export interface MusicVoiceChannel {
  channel_id: string
  name: string
  listeners: number
  bot_can_join: boolean
  connected: boolean
}

export interface MusicPlaylist {
  name: string
  tracks: number | null
  created_by: string | null
}

export interface MusicStatus {
  available: boolean
  ping_ms?: number | null
  bot_uptime_seconds?: number | null
  now_playing?: string | null
  queue_count?: number
  loop?: boolean
  autoplay?: { enabled: boolean; consecutive: number; max: number }
  pot_server?: { state: string; responding: boolean; enabled: boolean; uptime_seconds: number | null; restart_count: number }
  cookies?: { present: boolean; age_hours: number | null }
  versions?: { node: string; ytdlp: string; bgutil_plugin: string }
  cookie_refresh?: { age_seconds: number | null; poll_age_seconds: number | null } | null
  staff?: {
    url: string
    pid: number | null
    adopted_external: boolean
    watchdog: boolean
    watchdog_active: boolean
    last_error: string | null
    cookie_listener: { updates_total: number; accepted_count: number; rejected_count: number; last_error: string | null } | null
    log_name: string
    log_tail: string[]
  } | null
}

export interface MusicActionResult {
  message: string
  state?: MusicState
}

export type MusicControlAction =
  | "pause"
  | "resume"
  | "skip"
  | "stop"
  | "previous"
  | "shuffle"
  | "clear"
  | "loop"
  | "autoplay"
  | "volume"
  | "remove"
  | "move"
  | "join"
  | "leave"

export interface MusicControlInput {
  action: MusicControlAction
  value?: number | boolean
  index?: number
  to_index?: number
  channel_id?: string
}

export interface MusicSearchResult {
  index: number
  title: string
  url: string
}

interface MusicJob<T> {
  job_id: string
  kind: string
  status: "running" | "done" | "failed"
  result: T | null
  error: string | null
  message: string | null
}

/* ---- queries ---- */

const stateKey = ["music", "state"] as const
const playlistsKey = ["music", "playlists"] as const
/** Poll only while the live socket is down (track ends etc. arrive on `music`). */
const FALLBACK_POLL_MS = 5_000

/** Live `music` topic: every player change invalidates the music queries. */
export function useMusicLive(enabled: boolean) {
  const qc = useQueryClient()
  useLiveTopic(
    "music",
    (msg) => {
      const event = (msg.data as { event?: string } | undefined)?.event
      if (event === "playlists") invalidateSoon(qc, playlistsKey)
      else invalidateSoon(qc, stateKey)
    },
    { enabled }
  )
}

export function useMusicState() {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: stateKey,
    queryFn: ({ signal }) => apiFetch<MusicState>("/api/music/state", { signal }),
    refetchInterval: connected ? false : FALLBACK_POLL_MS,
  })
}

export function useMusicVoiceChannels() {
  return useQuery({
    queryKey: ["music", "voice-channels"],
    queryFn: ({ signal }) => apiFetch<{ channels: MusicVoiceChannel[] }>("/api/music/voice-channels", { signal }),
    staleTime: 30_000,
    refetchInterval: 60_000,
  })
}

export function useMusicPlaylists() {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: playlistsKey,
    queryFn: ({ signal }) => apiFetch<{ playlists: MusicPlaylist[] }>("/api/music/playlists", { signal }),
    refetchInterval: connected ? false : 60_000,
  })
}

/** Public !musicstatus view (control_music). */
export function useMusicStatus() {
  return useQuery({
    queryKey: ["music", "status"],
    queryFn: ({ signal }) => apiFetch<MusicStatus>("/api/music/status", { signal }),
    refetchInterval: 60_000,
  })
}

/** Staff !musicstatus view (require_staff); only enabled for staff. */
export function useMusicStaffStatus(enabled: boolean) {
  return useQuery({
    queryKey: ["music", "status", "staff"],
    queryFn: ({ signal }) => apiFetch<MusicStatus>("/api/music/status/staff", { signal }),
    refetchInterval: 60_000,
    enabled,
  })
}

/* ---- mutations ---- */

function useApplyState() {
  const qc = useQueryClient()
  return (res: MusicActionResult) => {
    if (res.state) qc.setQueryData(stateKey, res.state)
    invalidateSoon(qc, stateKey)
  }
}

export function useMusicControl() {
  const apply = useApplyState()
  return useMutation({
    mutationFn: (input: MusicControlInput) =>
      apiFetch<MusicActionResult>("/api/music/control", { method: "POST", body: JSON.stringify(input) }),
    onSuccess: apply,
  })
}

const POLL_MS = 1500
/** The bot bounds one extraction at MUSIC_EXTRACT_TIMEOUT (90 s, plus one cookie retry). */
const MAX_WAIT_MS = 5 * 60 * 1000
const MAX_POLL_FAILURES = 3

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

/** Poll a bot-side job until it finishes (each request is short: no Cloudflare 100 s cut). */
async function waitForJob<T>(first: MusicJob<T>, pollPath: (id: string) => string): Promise<MusicJob<T>> {
  let job = first
  const started = Date.now()
  let failures = 0
  while (job.status === "running") {
    if (Date.now() - started > MAX_WAIT_MS) throw new Error("That took too long. Try again.")
    await sleep(POLL_MS)
    try {
      job = await apiFetch<MusicJob<T>>(pollPath(job.job_id))
      failures = 0
    } catch (err) {
      failures += 1
      if (!(err instanceof ApiError && err.transient) || failures >= MAX_POLL_FAILURES) throw err
    }
  }
  if (job.status !== "done" || !job.result) throw new Error(job.message ?? "That did not work. Try again.")
  return job
}

const jobPath = (id: string) => `/api/music/jobs/${encodeURIComponent(id)}`

/** POST /api/music/play (URL) -> 202 job -> poll. */
export function useMusicPlayUrl() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (input: { query: string; channel_id?: string }) => {
      const first = await apiFetch<MusicJob<{ title: string; count: number; playlist: boolean }>>("/api/music/play", {
        method: "POST",
        body: JSON.stringify(input),
      })
      invalidateSoon(qc, stateKey) // the join (if any) already happened
      return (await waitForJob(first, jobPath)).result!
    },
    onSettled: () => invalidateSoon(qc, stateKey),
  })
}

/** POST /api/music/search -> 202 job -> poll; returns the job id (needed to pick) and results. */
export function useMusicSearch() {
  return useMutation({
    mutationFn: async (query: string) => {
      const first = await apiFetch<MusicJob<{ results: MusicSearchResult[] }>>("/api/music/search", {
        method: "POST",
        body: JSON.stringify({ query }),
      })
      const job = await waitForJob(first, jobPath)
      return { jobId: job.job_id, results: job.result!.results }
    },
  })
}

export function useMusicPick() {
  const apply = useApplyState()
  return useMutation({
    mutationFn: (input: { jobId: string; index: number; channel_id?: string }) =>
      apiFetch<MusicActionResult>(`/api/music/search/${encodeURIComponent(input.jobId)}/pick`, {
        method: "POST",
        body: JSON.stringify({ index: input.index, channel_id: input.channel_id }),
      }),
    onSuccess: apply,
  })
}

/** Staff-only !debugurl (masked report). */
export function useMusicDebugUrl() {
  return useMutation({
    mutationFn: async (query: string) => {
      const first = await apiFetch<MusicJob<{ ok: boolean; error_class: string | null; report: string }>>(
        "/api/music/debugurl",
        { method: "POST", body: JSON.stringify({ query: query || null }) }
      )
      return (await waitForJob(first, (id) => `/api/music/debugurl/${encodeURIComponent(id)}`)).result!
    },
  })
}

export function useSavePlaylist() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch<{ message: string; name: string }>("/api/music/playlists", { method: "POST", body: JSON.stringify({ name }) }),
    onSuccess: () => invalidateSoon(qc, playlistsKey),
  })
}

export function useLoadPlaylist() {
  const apply = useApplyState()
  return useMutation({
    mutationFn: (input: { name: string; channel_id?: string }) =>
      apiFetch<MusicActionResult>(`/api/music/playlists/${encodeURIComponent(input.name)}/load`, {
        method: "POST",
        body: JSON.stringify({ channel_id: input.channel_id }),
      }),
    onSuccess: apply,
  })
}

export function useDeletePlaylist() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch<{ message: string }>(`/api/music/playlists/${encodeURIComponent(name)}`, { method: "DELETE" }),
    onSuccess: () => invalidateSoon(qc, playlistsKey),
  })
}
