import { useEffect, useRef, useState } from "react"
import { Pause, Play, Radio, Trash2 } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { LiveCaption } from "@/components/patterns/LivePill"
import { useLive } from "@/live/useLive"
import { LIVE_TOPICS, type Envelope, type LiveTopic } from "@/live/protocol"

const MAX_ROWS = 300

interface Row {
  id: number
  at: number
  msg: Envelope
}

/** Subscribes the inspector itself to one topic while its box is ticked (ref-counted with the pages). */
function TopicSubscription({ topic }: { topic: LiveTopic }) {
  const { subscribe } = useLive()
  useEffect(() => subscribe(topic, () => {}), [subscribe, topic])
  return null
}

/**
 * Lab: live-event inspector. Shows every raw WebSocket envelope this tab
 * receives (LiveConnection.tap: hello, pong, event, snapshot, delta...), with
 * pause/clear. Ticking a topic subscribes this tab to it so its events show up
 * even when no page is watching it (diagnostics starts the bot's collector
 * while ticked). Browser-only: nothing is sent to the bot.
 */
export function LiveInspectorCard() {
  const { tap, state } = useLive()
  const [rows, setRows] = useState<Row[]>([])
  const [paused, setPaused] = useState(false)
  const [hidePongs, setHidePongs] = useState(true)
  const [topics, setTopics] = useState<LiveTopic[]>([])
  const [open, setOpen] = useState<number | null>(null)
  const pausedRef = useRef(paused)
  pausedRef.current = paused
  const nextId = useRef(1)

  useEffect(
    () =>
      tap((msg) => {
        if (pausedRef.current) return
        const row = { id: nextId.current++, at: Date.now(), msg }
        setRows((prev) => [row, ...prev].slice(0, MAX_ROWS))
      }),
    [tap]
  )

  const visible = hidePongs ? rows.filter((r) => r.msg.type !== "pong" && r.msg.type !== "ack") : rows

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Radio className="size-4" aria-hidden /> Live-event inspector
        </CardTitle>
        <CardDescription>
          Raw WebSocket envelopes this browser tab receives.
        </CardDescription>
        <LiveCaption liveText={`Socket ${state}`} fallbackText={`Socket ${state}: nothing arrives until it reconnects.`} />
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="secondary" onClick={() => setPaused((p) => !p)}>
            {paused ? <Play /> : <Pause />} {paused ? "Resume" : "Pause"}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setRows([])} disabled={rows.length === 0}>
            <Trash2 /> Clear
          </Button>
          <div className="flex items-center gap-2">
            <Checkbox id="lab-hide-pongs" checked={hidePongs} onCheckedChange={(v) => setHidePongs(v === true)} />
            <Label htmlFor="lab-hide-pongs" className="font-normal">
              Hide pong/ack
            </Label>
          </div>
          <span className="ml-auto text-xs text-muted-foreground">
            {visible.length} shown (last {MAX_ROWS} kept)
          </span>
        </div>
        <fieldset className="flex flex-wrap gap-x-4 gap-y-1">
          <legend className="mb-1 text-xs text-muted-foreground">Also subscribe this tab to:</legend>
          {LIVE_TOPICS.map((t) => (
            <div key={t} className="flex items-center gap-1.5">
              <Checkbox
                id={`lab-topic-${t}`}
                checked={topics.includes(t)}
                onCheckedChange={(v) => setTopics((ts) => (v ? [...ts, t] : ts.filter((x) => x !== t)))}
              />
              <Label htmlFor={`lab-topic-${t}`} className="font-mono text-xs font-normal">
                {t}
              </Label>
            </div>
          ))}
        </fieldset>
        {topics.map((t) => (
          <TopicSubscription key={t} topic={t} />
        ))}
        <div className="max-h-[420px] overflow-auto rounded-lg border border-border">
          {visible.length === 0 ? (
            <p className="p-3 text-xs text-muted-foreground">
              {paused ? "Paused." : "Waiting for frames… (a pong arrives every 25 s; tick a topic to see its events)"}
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {visible.map((r) => (
                <li key={r.id} className="text-xs">
                  <button
                    type="button"
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                    onClick={() => setOpen((o) => (o === r.id ? null : r.id))}
                    aria-expanded={open === r.id}
                  >
                    <span className="font-mono text-muted-foreground">{new Date(r.at).toLocaleTimeString()}</span>
                    <Badge variant={r.msg.type === "error" ? "destructive" : r.msg.type === "event" ? "info" : "outline"}>{r.msg.type}</Badge>
                    {r.msg.topic ? <span className="font-mono text-foreground">{r.msg.topic}</span> : null}
                    {typeof r.msg.seq === "number" ? <span className="text-muted-foreground">seq {r.msg.seq}</span> : null}
                    <span className="ml-auto truncate font-mono text-muted-foreground">{preview(r.msg.data)}</span>
                  </button>
                  {open === r.id ? (
                    <pre className="max-h-72 overflow-auto bg-muted px-3 py-2 font-mono text-[11px] leading-4 text-foreground">
                      {JSON.stringify(r.msg, null, 2)}
                    </pre>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

function preview(data: unknown): string {
  if (data === undefined || data === null) return ""
  try {
    const s = JSON.stringify(data)
    return s.length > 120 ? s.slice(0, 117) + "..." : s
  } catch {
    return ""
  }
}
