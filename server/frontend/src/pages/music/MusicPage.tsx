import { useEffect, useMemo, useState, type FormEvent } from "react"
import {
  ArrowDown,
  ArrowUp,
  Bug,
  ExternalLink,
  ListMusic,
  LogOut,
  Music,
  Pause,
  Play,
  Plus,
  Repeat,
  Search,
  Shuffle,
  SkipBack,
  SkipForward,
  Square,
  Trash2,
  Volume2,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { useToast } from "@/components/ui/toast"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { EmptyState, ErrorState, ListSkeleton } from "@/components/patterns/states"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { LiveCaption } from "@/components/patterns/LivePill"
import { ResolvedUser } from "@/components/ResolvedUser"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { ApiError } from "@/api/client"
import { useAuth } from "@/hooks/useAuth"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import {
  useDeletePlaylist,
  useLoadPlaylist,
  useMusicControl,
  useMusicDebugUrl,
  useMusicLive,
  useMusicPick,
  useMusicPlaylists,
  useMusicPlayUrl,
  useMusicSearch,
  useMusicStaffStatus,
  useMusicState,
  useMusicStatus,
  useMusicVoiceChannels,
  useSavePlaylist,
  type MusicControlInput,
  type MusicSearchResult,
  type MusicState,
  type MusicStatus,
} from "@/api/music"

function forbiddenPermission(error: unknown): string | null {
  if (error instanceof ApiError && error.status === 403) {
    const body = error.body as { error?: string; required_permission?: string; detail?: { error?: string; required_permission?: string } } | null
    const b = body?.detail ?? body
    if (b?.error === "forbidden") return b.required_permission ?? "control_music"
  }
  return null
}

function fmt(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "?:??"
  const s = Math.max(0, Math.floor(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = String(s % 60).padStart(2, "0")
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`
}

function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "never"
  if (seconds < 90) return `${Math.round(seconds)} s ago`
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} d ago`
}

const isUrl = (text: string) => /^https?:\/\//i.test(text.trim())

/** Local progress: the bot sends `elapsed` at fetch time; tick it while playing. */
function useElapsed(state: MusicState | undefined, fetchedAt: number): number {
  const [now, setNow] = useState(() => Date.now())
  const ticking = !!state?.current && state.playing
  useEffect(() => {
    if (!ticking) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [ticking])
  if (!state?.current) return 0
  const extra = ticking ? Math.max(0, (now - fetchedAt) / 1000) : 0
  const value = state.current.elapsed + extra
  return state.current.duration ? Math.min(value, state.current.duration) : value
}

/**
 * Music player (/dashboard/music, perm control_music; staff bypass). Drives
 * the bot's live player (the same state the ! music commands use): every
 * action is announced in the music text channel ("via dashboard by ...") and
 * audited. Updates live on the `music` topic; polls only while the socket is down.
 */
export default function MusicPage() {
  const stateQuery = useMusicState()
  const { isStaff } = useAuth()
  const { offline, reason } = useBotOffline()
  useMusicLive(stateQuery.data !== undefined)

  const forbidden = forbiddenPermission(stateQuery.error)
  if (forbidden) return <Forbidden403 reason="forbidden" requiredPermission={forbidden} />

  const st = stateQuery.data

  return (
    <PageContainer>
      <PageHeader
        title="Music"
        description="Control the bot's music player. Everything you do here is announced in the music channel and logged."
        badge={<LiveCaption fallbackText="Live updates paused. Refreshing every 5 s." />}
      />
      {offline ? <Alert variant="warning">{reason}</Alert> : null}

      {stateQuery.isPending ? (
        <div className="grid gap-6 lg:grid-cols-[3fr_2fr]">
          <Card>
            <CardContent className="flex flex-col gap-3">
              <Skeleton className="h-6 w-2/3" />
              <Skeleton className="h-2 w-full" />
              <Skeleton className="h-9 w-1/2" />
            </CardContent>
          </Card>
          <Card>
            <ListSkeleton rows={3} avatar={false} />
          </Card>
        </div>
      ) : stateQuery.isError || !st ? (
        <Card>
          <ErrorState
            title="Could not load the music player"
            message={describeApiError(stateQuery.error)}
            onRetry={() => stateQuery.refetch()}
            retrying={stateQuery.isFetching}
            showStatusLink
          />
        </Card>
      ) : (
        <MusicBody state={st} fetchedAt={stateQuery.dataUpdatedAt} disabled={offline} disabledReason={reason} isStaff={isStaff} />
      )}
    </PageContainer>
  )
}

function MusicBody({
  state,
  fetchedAt,
  disabled,
  disabledReason,
  isStaff,
}: {
  state: MusicState
  fetchedAt: number
  disabled: boolean
  disabledReason: string | undefined
  isStaff: boolean
}) {
  const control = useMusicControl()
  const { toast } = useToast()
  const channelsQuery = useMusicVoiceChannels()
  const [channelId, setChannelId] = useState("")
  const channels = useMemo(() => channelsQuery.data?.channels ?? [], [channelsQuery.data])

  // Default the picker to the bot's current channel (or the first joinable one).
  useEffect(() => {
    if (channelId) return
    const current = channels.find((c) => c.connected) ?? channels.find((c) => c.bot_can_join)
    if (current) setChannelId(current.channel_id)
  }, [channels, channelId])

  /** Channel to send with play/pick/load when the bot is not connected yet. */
  const joinTarget = state.connected ? undefined : channelId || undefined

  const run = (input: MusicControlInput, success?: string) =>
    control.mutate(input, {
      onSuccess: (r) => (success ?? r.message) && toast({ title: success ?? r.message, variant: "success" }),
      onError: (err) => toast({ title: "Could not do that", description: describeApiError(err), variant: "destructive" }),
    })
  const busy = disabled || control.isPending

  return (
    <div className="grid gap-6 lg:grid-cols-[3fr_2fr]">
      <div className="flex min-w-0 flex-col gap-6">
        <NowPlaying state={state} fetchedAt={fetchedAt} busy={busy} disabledReason={disabledReason} run={run} joinTarget={joinTarget} />
        <AddTrack state={state} disabled={disabled} disabledReason={disabledReason} joinTarget={joinTarget} />
        <QueueCard state={state} busy={busy} run={run} />
      </div>
      <div className="flex min-w-0 flex-col gap-6">
        <Card>
          <CardHeader>
            <CardTitle>Voice channel</CardTitle>
            <CardDescription>
              {state.connected && state.voice_channel
                ? `In ${state.voice_channel.name} with ${state.voice_channel.listeners} listener${state.voice_channel.listeners === 1 ? "" : "s"}.`
                : "Not connected. Pick a channel; playing something joins it."}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {channelsQuery.isError ? (
              <p className="text-sm text-danger">{describeApiError(channelsQuery.error)}</p>
            ) : (
              <Select
                aria-label="Voice channel"
                value={channelId}
                disabled={busy || channelsQuery.isPending}
                onChange={(e) => setChannelId(e.target.value)}
              >
                <option value="">Choose a voice channel</option>
                {channels.map((c) => (
                  <option key={c.channel_id} value={c.channel_id} disabled={!c.bot_can_join}>
                    {c.name}
                    {c.listeners ? ` (${c.listeners})` : ""}
                    {c.connected ? " - connected" : ""}
                    {!c.bot_can_join ? " - bot can't join" : ""}
                  </option>
                ))}
              </Select>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                variant="secondary"
                size="sm"
                disabled={busy || !channelId || (state.connected && state.voice_channel?.channel_id === channelId)}
                title={disabledReason}
                onClick={() => run({ action: "join", channel_id: channelId })}
              >
                {state.connected ? "Move here" : "Join"}
              </Button>
              <Button
                variant="destructive-outline"
                size="sm"
                disabled={busy || !state.connected}
                title={disabledReason}
                onClick={() => run({ action: "leave" })}
              >
                <LogOut /> Leave
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {state.announce_channel
                ? `Dashboard actions are announced in #${state.announce_channel.name}.`
                : "No music text channel yet: use a music command in Discord once, or join a voice channel (its chat is used)."}
            </p>
          </CardContent>
        </Card>
        <PlaylistsCard state={state} disabled={disabled} disabledReason={disabledReason} joinTarget={joinTarget} />
        <HistoryCard state={state} />
        <StatusCard isStaff={isStaff} disabled={disabled} />
      </div>
    </div>
  )
}

function NowPlaying({
  state,
  fetchedAt,
  busy,
  disabledReason,
  run,
  joinTarget,
}: {
  state: MusicState
  fetchedAt: number
  busy: boolean
  disabledReason: string | undefined
  run: (input: MusicControlInput, success?: string) => void
  joinTarget: string | undefined
}) {
  const elapsed = useElapsed(state, fetchedAt)
  const cur = state.current
  const [volume, setVolume] = useState(state.volume)
  const [dragging, setDragging] = useState(false)
  useEffect(() => {
    if (!dragging) setVolume(state.volume)
  }, [state.volume, dragging])
  const commitVolume = () => {
    setDragging(false)
    if (volume !== state.volume) run({ action: "volume", value: volume }, `Volume set to ${volume}%`)
  }
  const pct = cur?.duration ? (elapsed / cur.duration) * 100 : 0

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Music className="size-4" aria-hidden /> Now playing
          {state.paused ? <Badge variant="warning">Paused</Badge> : state.playing ? <Badge variant="success" dot>Playing</Badge> : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {cur ? (
          <div className="flex flex-col gap-2">
            <div className="flex items-start justify-between gap-3">
              <p className="min-w-0 break-words text-md font-semibold text-foreground">{cur.title}</p>
              {cur.url ? (
                <a href={cur.url} target="_blank" rel="noreferrer noopener" className="shrink-0 text-muted-foreground hover:text-foreground" aria-label="Open track">
                  <ExternalLink className="size-4" />
                </a>
              ) : null}
            </div>
            <Progress value={pct} aria-label="Track progress" />
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{fmt(elapsed)} / {fmt(cur.duration)}</span>
              {cur.requester_id ? (
                <span className="flex items-center gap-1">
                  Started by <ResolvedUser userId={cur.requester_id} />
                </span>
              ) : null}
            </div>
          </div>
        ) : (
          <EmptyState icon={Music} title="Nothing is playing" description="Add a track below, or load a playlist." className="py-4" />
        )}

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            size="icon"
            aria-label="Replay previous"
            title={disabledReason ?? (state.previous ? `Replay ${state.previous.title}` : "No previous song")}
            disabled={busy || !state.previous}
            onClick={() => run({ action: "previous", channel_id: joinTarget })}
          >
            <SkipBack />
          </Button>
          {state.paused ? (
            <Button size="icon" aria-label="Resume" title={disabledReason} disabled={busy} onClick={() => run({ action: "resume" })}>
              <Play />
            </Button>
          ) : (
            <Button size="icon" aria-label="Pause" title={disabledReason} disabled={busy || !state.playing} onClick={() => run({ action: "pause" })}>
              <Pause />
            </Button>
          )}
          <Button
            variant="secondary"
            size="icon"
            aria-label="Skip"
            title={disabledReason ?? "Skip now (no vote from the dashboard)"}
            disabled={busy || !(state.playing || state.paused)}
            onClick={() => run({ action: "skip" })}
          >
            <SkipForward />
          </Button>
          <Button
            variant="secondary"
            size="icon"
            aria-label="Stop and clear the queue"
            title={disabledReason ?? "Stop and clear the queue"}
            disabled={busy || (!state.playing && !state.paused && state.queue_total === 0)}
            onClick={() => run({ action: "stop" })}
          >
            <Square />
          </Button>
          <div className="ml-auto flex items-center gap-2">
            <Volume2 className="size-4 text-muted-foreground" aria-hidden />
            <input
              type="range"
              min={0}
              max={200}
              step={5}
              value={volume}
              disabled={busy}
              aria-label="Volume"
              className="w-32 accent-primary"
              onChange={(e) => {
                setDragging(true)
                setVolume(Number(e.target.value))
              }}
              onPointerUp={commitVolume}
              onKeyUp={commitVolume}
              onBlur={() => dragging && commitVolume()}
            />
            <span className="w-10 text-right text-sm tabular-nums text-muted-foreground">{volume}%</span>
          </div>
        </div>

        <div className="flex flex-wrap gap-6">
          <div className="flex items-center gap-2">
            <Switch id="music-loop" checked={state.loop} disabled={busy} onCheckedChange={(v) => run({ action: "loop", value: v })} />
            <Label htmlFor="music-loop" className="flex items-center gap-1">
              <Repeat className="size-3.5" aria-hidden /> Loop
            </Label>
          </div>
          <div className="flex items-center gap-2">
            <Switch
              id="music-autoplay"
              checked={state.autoplay.enabled}
              disabled={busy}
              onCheckedChange={(v) => run({ action: "autoplay", value: v })}
            />
            <Label htmlFor="music-autoplay">
              Autoplay
              {state.autoplay.enabled ? (
                <span className="ml-1 text-xs text-muted-foreground">
                  ({state.autoplay.consecutive}/{state.autoplay.max} in a row)
                </span>
              ) : null}
            </Label>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function AddTrack({
  state,
  disabled,
  disabledReason,
  joinTarget,
}: {
  state: MusicState
  disabled: boolean
  disabledReason: string | undefined
  joinTarget: string | undefined
}) {
  const [query, setQuery] = useState("")
  const [results, setResults] = useState<{ jobId: string; items: MusicSearchResult[] } | null>(null)
  const play = useMusicPlayUrl()
  const search = useMusicSearch()
  const pick = useMusicPick()
  const { toast } = useToast()
  const pending = play.isPending || search.isPending
  const needsChannel = !state.connected && !joinTarget

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const q = query.trim()
    if (!q) return
    if (isUrl(q)) {
      play.mutate(
        { query: q, channel_id: joinTarget },
        {
          onSuccess: (r) => {
            setQuery("")
            toast({ title: r.playlist ? `Queued playlist ${r.title} (${r.count} tracks)` : `Queued ${r.title}`, variant: "success" })
          },
          onError: (err) => toast({ title: "Could not add that", description: describeApiError(err), variant: "destructive" }),
        }
      )
    } else {
      search.mutate(q, {
        onSuccess: (r) => setResults({ jobId: r.jobId, items: r.results }),
        onError: (err) => toast({ title: "Search failed", description: describeApiError(err), variant: "destructive" }),
      })
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Add a track</CardTitle>
        <CardDescription>Paste a YouTube link (track or playlist) or type a search. Lookups can take up to a minute.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <form onSubmit={submit} className="flex gap-2">
          <Input
            aria-label="URL or search"
            placeholder="https://youtu.be/... or song name"
            value={query}
            maxLength={500}
            onChange={(e) => setQuery(e.target.value)}
            disabled={disabled}
          />
          <Button
            type="submit"
            loading={pending}
            disabled={disabled || pending || !query.trim() || (isUrl(query) && needsChannel)}
            title={disabledReason ?? (isUrl(query) && needsChannel ? "Pick a voice channel first" : undefined)}
          >
            {isUrl(query) ? <Plus /> : <Search />} {isUrl(query) ? "Add" : "Search"}
          </Button>
        </form>
        {results ? (
          results.items.length === 0 ? (
            <p className="text-sm text-muted-foreground">No results found.</p>
          ) : (
            <ul className="flex flex-col divide-y divide-border rounded-lg border">
              {results.items.map((r) => (
                <li key={r.index} className="flex items-center gap-3 px-3 py-2">
                  <span className="min-w-0 flex-1 truncate text-sm" title={r.title}>
                    {r.title}
                  </span>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={disabled || pick.isPending || needsChannel}
                    title={disabledReason ?? (needsChannel ? "Pick a voice channel first" : undefined)}
                    onClick={() =>
                      pick.mutate(
                        { jobId: results.jobId, index: r.index, channel_id: joinTarget },
                        {
                          onSuccess: () => toast({ title: `Queued ${r.title}`, variant: "success" }),
                          onError: (err) => toast({ title: "Could not queue it", description: describeApiError(err), variant: "destructive" }),
                        }
                      )
                    }
                  >
                    <Plus /> Queue
                  </Button>
                </li>
              ))}
            </ul>
          )
        ) : null}
      </CardContent>
    </Card>
  )
}

function QueueCard({ state, busy, run }: { state: MusicState; busy: boolean; run: (input: MusicControlInput, success?: string) => void }) {
  const [confirmClear, setConfirmClear] = useState(false)
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ListMusic className="size-4" aria-hidden /> Queue
          <Badge variant="outline">{state.queue_total}</Badge>
        </CardTitle>
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" disabled={busy || state.queue_total < 2} onClick={() => run({ action: "shuffle" })}>
            <Shuffle /> Shuffle
          </Button>
          <Button variant="destructive-outline" size="sm" disabled={busy || state.queue_total === 0} onClick={() => setConfirmClear(true)}>
            <Trash2 /> Clear
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {state.queue.length === 0 ? (
          <EmptyState icon={ListMusic} title="The queue is empty" description="Queued tracks show up here." className="py-4" />
        ) : (
          <ol className="flex flex-col divide-y divide-border">
            {state.queue.map((t) => (
              <li key={`${t.position}-${t.url}`} className="flex items-center gap-2 py-2">
                <span className="w-7 shrink-0 text-right text-xs tabular-nums text-muted-foreground">{t.position}.</span>
                <span className="min-w-0 flex-1 truncate text-sm" title={t.title}>
                  {t.title}
                </span>
                {t.autoplay ? <Badge variant="info">Autoplay</Badge> : null}
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Move ${t.title} up`}
                  disabled={busy || t.position === 1}
                  onClick={() => run({ action: "move", index: t.position, to_index: t.position - 1 })}
                >
                  <ArrowUp />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Move ${t.title} down`}
                  disabled={busy || t.position === state.queue_total}
                  onClick={() => run({ action: "move", index: t.position, to_index: t.position + 1 })}
                >
                  <ArrowDown />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Remove ${t.title}`}
                  disabled={busy}
                  onClick={() => run({ action: "remove", index: t.position })}
                >
                  <Trash2 />
                </Button>
              </li>
            ))}
          </ol>
        )}
        {state.queue_total > state.queue.length ? (
          <p className="pt-2 text-xs text-muted-foreground">And {state.queue_total - state.queue.length} more.</p>
        ) : null}
      </CardContent>
      <ConfirmDialog
        open={confirmClear}
        onOpenChange={setConfirmClear}
        title="Clear the queue?"
        description={`Removes all ${state.queue_total} queued tracks. The current track keeps playing.`}
        confirmLabel="Clear queue"
        onConfirm={() => {
          setConfirmClear(false)
          run({ action: "clear" })
        }}
      />
    </Card>
  )
}

function PlaylistsCard({
  state,
  disabled,
  disabledReason,
  joinTarget,
}: {
  state: MusicState
  disabled: boolean
  disabledReason: string | undefined
  joinTarget: string | undefined
}) {
  const { user, isStaff } = useAuth()
  const playlistsQuery = useMusicPlaylists()
  const save = useSavePlaylist()
  const load = useLoadPlaylist()
  const del = useDeletePlaylist()
  const { toast } = useToast()
  const [name, setName] = useState("")
  const [toDelete, setToDelete] = useState<string | null>(null)
  const nothingToSave = !state.current && state.queue_total === 0
  const needsChannel = !state.connected && !joinTarget
  const playlists = playlistsQuery.data?.playlists ?? []
  const myId = user?.id

  const onError = (title: string) => (err: unknown) => toast({ title, description: describeApiError(err), variant: "destructive" })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Playlists</CardTitle>
        <CardDescription>Only whoever saved a playlist (or staff) can overwrite or delete it.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (!name.trim()) return
            save.mutate(name.trim(), {
              onSuccess: (r) => {
                setName("")
                toast({ title: r.message, variant: "success" })
              },
              onError: onError("Could not save"),
            })
          }}
        >
          <Input
            aria-label="Playlist name"
            placeholder="Name (letters, numbers, _ -)"
            value={name}
            maxLength={64}
            onChange={(e) => setName(e.target.value)}
            disabled={disabled}
          />
          <Button
            type="submit"
            variant="secondary"
            loading={save.isPending}
            disabled={disabled || save.isPending || !name.trim() || nothingToSave}
            title={disabledReason ?? (nothingToSave ? "Nothing to save" : "Save the current track and queue")}
          >
            Save
          </Button>
        </form>
        {playlistsQuery.isPending ? (
          <ListSkeleton rows={2} avatar={false} />
        ) : playlistsQuery.isError ? (
          <p className="text-sm text-danger">{describeApiError(playlistsQuery.error)}</p>
        ) : playlists.length === 0 ? (
          <p className="text-sm text-muted-foreground">No playlists saved yet.</p>
        ) : (
          <ul className="flex flex-col divide-y divide-border">
            {playlists.map((p) => {
              const mine = !!myId && p.created_by === myId
              const canChange = mine || isStaff
              return (
                <li key={p.name} className="flex items-center gap-2 py-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{p.name}</p>
                    <p className="flex items-center gap-1 text-xs text-muted-foreground">
                      {p.tracks ?? "?"} tracks
                      {p.created_by ? (
                        <>
                          {" "}by <ResolvedUser userId={p.created_by} />
                        </>
                      ) : (
                        " (no owner, staff only)"
                      )}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={disabled || load.isPending || needsChannel}
                    title={disabledReason ?? (needsChannel ? "Pick a voice channel first" : undefined)}
                    onClick={() =>
                      load.mutate(
                        { name: p.name, channel_id: joinTarget },
                        { onSuccess: (r) => toast({ title: r.message, variant: "success" }), onError: onError("Could not load") }
                      )
                    }
                  >
                    Load
                  </Button>
                  <Button
                    size="icon-sm"
                    variant="ghost"
                    aria-label={`Delete ${p.name}`}
                    disabled={disabled || del.isPending}
                    title={canChange ? undefined : "Staff may delete it; the bot checks again"}
                    onClick={() => setToDelete(p.name)}
                  >
                    <Trash2 />
                  </Button>
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
      <ConfirmDialog
        open={toDelete !== null}
        onOpenChange={(o) => !o && setToDelete(null)}
        title={`Delete playlist ${toDelete ?? ""}?`}
        description="The saved playlist file is removed for everyone. This cannot be undone."
        confirmLabel="Delete playlist"
        loading={del.isPending}
        onConfirm={() => {
          if (!toDelete) return
          del.mutate(toDelete, {
            onSuccess: (r) => {
              setToDelete(null)
              toast({ title: r.message, variant: "success" })
            },
            onError: (err) => {
              setToDelete(null)
              onError("Could not delete")(err)
            },
          })
        }}
      />
    </Card>
  )
}

function HistoryCard({ state }: { state: MusicState }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Recently played</CardTitle>
      </CardHeader>
      <CardContent>
        {state.history.length === 0 ? (
          <p className="text-sm text-muted-foreground">No songs have been played yet.</p>
        ) : (
          <ol className="flex flex-col gap-1">
            {state.history.map((h, i) => (
              <li key={`${i}-${h.url}`} className="truncate text-sm text-muted-foreground" title={h.title}>
                {i + 1}. {h.title}
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  )
}

function StatusRows({ s }: { s: MusicStatus }) {
  const rows: [string, string][] = useMemo(() => {
    if (!s.available) return []
    return [
      ["Ping", s.ping_ms != null ? `${s.ping_ms} ms` : "n/a"],
      ["PO token server", `${s.pot_server?.state ?? "?"} (${s.pot_server?.responding ? "responding" : "not responding"})`],
      ["Server restarts", String(s.pot_server?.restart_count ?? 0)],
      ["Cookies", s.cookies?.present ? `present, ${s.cookies.age_hours ?? "?"} h old` : "MISSING"],
      ["Last cookie refresh", s.cookie_refresh ? fmtAge(s.cookie_refresh.age_seconds) : "listener off"],
      ["Versions", `node ${s.versions?.node}, yt-dlp ${s.versions?.ytdlp}, bgutil ${s.versions?.bgutil_plugin}`],
    ]
  }, [s])
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function StatusCard({ isStaff, disabled }: { isStaff: boolean; disabled: boolean }) {
  const pub = useMusicStatus()
  const staff = useMusicStaffStatus(isStaff)
  const s = (isStaff ? staff.data : undefined) ?? pub.data
  const query = isStaff ? staff : pub
  return (
    <Card>
      <CardHeader>
        <CardTitle>Status</CardTitle>
        <CardDescription>Same as !musicstatus{isStaff ? " (staff view)" : ""}.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {query.isPending && !s ? (
          <Skeleton className="h-24 w-full" />
        ) : !s ? (
          <p className="text-sm text-danger">{describeApiError(query.error)}</p>
        ) : !s.available ? (
          <p className="text-sm text-muted-foreground">The PO token server service is not available.</p>
        ) : (
          <>
            <StatusRows s={s} />
            {s.staff ? (
              <div className="flex flex-col gap-2 border-t pt-3 text-sm">
                <p className="font-medium">Staff details</p>
                <p className="text-muted-foreground">
                  {s.staff.url} · pid {s.staff.pid ?? "?"}
                  {s.staff.adopted_external ? " (adopted)" : ""} · watchdog {s.staff.watchdog ? "on" : "off"}
                </p>
                <p className="text-muted-foreground">Last error: {s.staff.last_error ?? "none"}</p>
                {s.staff.log_tail.length ? (
                  <pre className="max-h-40 overflow-auto rounded-md bg-muted p-2 text-xs">{s.staff.log_tail.join("\n")}</pre>
                ) : null}
              </div>
            ) : null}
          </>
        )}
        {isStaff ? <DebugUrl disabled={disabled} /> : null}
      </CardContent>
    </Card>
  )
}

function DebugUrl({ disabled }: { disabled: boolean }) {
  const [q, setQ] = useState("")
  const debug = useMusicDebugUrl()
  return (
    <div className="flex flex-col gap-2 border-t pt-3">
      <p className="flex items-center gap-1 text-sm font-medium">
        <Bug className="size-3.5" aria-hidden /> Debug a URL <Badge variant="outline">Staff</Badge>
      </p>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          debug.mutate(q.trim())
        }}
      >
        <Input aria-label="URL or query to debug" placeholder="Blank = current track" value={q} maxLength={500} onChange={(e) => setQ(e.target.value)} />
        <Button type="submit" variant="secondary" loading={debug.isPending} disabled={disabled || debug.isPending}>
          Run
        </Button>
      </form>
      {debug.isError ? <p className="text-sm text-danger">{describeApiError(debug.error)}</p> : null}
      {debug.data ? (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 text-xs">{debug.data.report}</pre>
      ) : null}
    </div>
  )
}
