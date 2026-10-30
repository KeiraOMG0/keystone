import { useState } from "react"
import { Play, ShieldAlert } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Field } from "@/components/ui/field"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import { useLabAntispam, type LabOptions, type LabSpamMessage } from "@/api/debugLab"

const PRESETS: Record<string, string> = {
  "Normal chat": ["0 | A | general | hey all", "4 | B | general | hi!", "9 | A | general | how was your day", "15 | A | memes | lol"].join("\n"),
  Flood: Array.from({ length: 8 }, (_, i) => `${(i * 0.5).toFixed(1)} | A | general | message ${i + 1}`).join("\n"),
  "Cross-channel duplicate": ["general", "memes", "art", "music", "gaming", "intro"]
    .map((ch, i) => `${i * 3} | A | ${ch} | FREE NITRO at example.com`)
    .join("\n"),
  "Mention spam": "0 | A | general | <@111111111111111111> <@222222222222222222> <@333333333333333333> <@444444444444444444> <@555555555555555555> <@666666666666666666> hi",
  "Cooldown follow-ups": [
    ...Array.from({ length: 7 }, (_, i) => `${(i * 0.4).toFixed(1)} | A | general | spam ${i}`),
    "10 | A | general | still here",
    "40 | A | general | after the cooldown",
  ].join("\n"),
}

/** "seconds | user | channel | text" per line; a line without "|" is text one second after the previous one. */
function parseLines(text: string): { messages: LabSpamMessage[]; error: string | null } {
  const messages: LabSpamMessage[] = []
  let last = 0
  const lines = text.split("\n").filter((l) => l.trim())
  for (const [i, line] of lines.entries()) {
    const parts = line.split("|").map((p) => p.trim())
    if (parts.length === 1) {
      last = messages.length ? last + 1 : 0
      messages.push({ at: last, content: parts[0] })
      continue
    }
    const at = Number(parts[0])
    if (!Number.isFinite(at) || at < 0) return { messages, error: `Line ${i + 1}: the first part must be seconds (0 or more).` }
    last = at
    messages.push({ at, user: parts[1] || "A", channel: parts[2] || "general", content: parts.slice(3).join(" | ") || null })
  }
  if (messages.length > 200) return { messages, error: "At most 200 messages." }
  return { messages, error: null }
}

/**
 * Lab: anti-spam tester. Runs pasted sample messages through the real
 * services/spam_detector.py with a fake clock and the saved thresholds (or
 * per-test overrides). Pure: nothing is deleted, nobody is timed out, nothing
 * is saved.
 */
export function AntispamTesterCard({ options }: { options: LabOptions | undefined }) {
  const run = useLabAntispam()
  const { offline, reason } = useBotOffline()
  const [text, setText] = useState(PRESETS.Flood)
  const [overrides, setOverrides] = useState<Record<string, string>>({})
  const parsed = parseLines(text)

  const submit = () => {
    const ov: Record<string, number | boolean | null> = {}
    for (const [k, v] of Object.entries(overrides)) {
      if (v.trim() === "") continue
      ov[k] = k === "antispam_enabled" ? v === "true" : Number(v)
    }
    run.mutate({ overrides: ov, messages: parsed.messages })
  }

  const saved = options?.antispam ?? {}
  const intFields = (options?.antispam_fields ?? []).filter((f) => f.type === "int")
  const r = run.data

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldAlert className="size-4" aria-hidden /> Anti-spam tester
        </CardTitle>
        <CardDescription>
          Replays sample messages through the real spam detector with the saved thresholds. Nothing is deleted or timed out.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap gap-1.5">
          {Object.keys(PRESETS).map((name) => (
            <Button key={name} size="xs" variant="secondary" onClick={() => setText(PRESETS[name])}>
              {name}
            </Button>
          ))}
        </div>
        <Field
          label="Messages"
          hint='One per line: "seconds | user | channel | text". Mentions are counted from <@id> / <@&id> in the text.'
          error={parsed.error ?? undefined}
        >
          {(p) => <Textarea {...p} rows={7} className="font-mono text-xs" value={text} onChange={(e) => setText(e.target.value)} />}
        </Field>
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground">Override thresholds for this test (blank = saved value)</summary>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {intFields.map((f) => (
              <Field key={f.key} label={f.label}>
                {(p) => (
                  <Input
                    {...p}
                    type="number"
                    min={f.min ?? undefined}
                    max={f.max ?? undefined}
                    placeholder={String((saved as Record<string, unknown>)[f.key.replace("antispam_", "")] ?? "")}
                    value={overrides[f.key] ?? ""}
                    onChange={(e) => setOverrides((o) => ({ ...o, [f.key]: e.target.value }))}
                  />
                )}
              </Field>
            ))}
          </div>
        </details>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            onClick={submit}
            loading={run.isPending}
            disabled={run.isPending || offline || Boolean(parsed.error) || parsed.messages.length === 0}
            title={offline ? reason : undefined}
          >
            <Play /> Run {parsed.messages.length} message{parsed.messages.length === 1 ? "" : "s"}
          </Button>
        </div>
        {run.isError ? <Alert variant="danger" title="Test failed">{describeApiError(run.error)}</Alert> : null}
        {r && !run.isPending ? (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap gap-1.5 text-xs">
              <Badge variant={r.rules.enabled ? "success" : "warning"}>{r.rules.enabled ? "Anti-spam on" : "Anti-spam OFF"}</Badge>
              <Badge variant="outline">
                Flood {r.rules.flood_messages || "off"}
                {r.rules.flood_messages ? ` in ${r.rules.flood_seconds}s` : ""}
              </Badge>
              <Badge variant="outline">
                Dupes {r.rules.duplicate_messages || "off"}
                {r.rules.duplicate_messages ? ` in ${r.rules.duplicate_seconds}s` : ""}
              </Badge>
              <Badge variant="outline">Mentions {r.rules.mention_limit || "off"}</Badge>
              <Badge variant="outline">Timeout {r.rules.timeout_minutes} min</Badge>
            </div>
            <p className="text-sm text-foreground">
              {r.timeouts.length
                ? `Would time out: ${r.timeouts.map((t) => `${t.user} at ${t.at}s (${t.rule}, ${t.minutes} min)`).join("; ")}.`
                : Object.keys(r.counts).length
                  ? "Would delete messages but not time anyone out."
                  : "Nothing would be flagged."}{" "}
              {r.deleted.length ? `${r.deleted.length} message${r.deleted.length === 1 ? "" : "s"} deleted.` : ""}
            </p>
            <div className="max-h-80 overflow-auto rounded-lg border border-border">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-card text-left text-muted-foreground">
                  <tr>
                    <th className="px-2 py-1 font-medium">#</th>
                    <th className="px-2 py-1 font-medium">Time</th>
                    <th className="px-2 py-1 font-medium">User</th>
                    <th className="px-2 py-1 font-medium">Channel</th>
                    <th className="px-2 py-1 font-medium">Text</th>
                    <th className="px-2 py-1 font-medium">Result</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {r.results.map((m) => (
                    <tr key={m.index} className={r.deleted.includes(m.index) ? "bg-danger-soft" : undefined}>
                      <td className="px-2 py-1 font-mono">{m.index + 1}</td>
                      <td className="px-2 py-1 font-mono">{m.at}s</td>
                      <td className="px-2 py-1">{m.user}</td>
                      <td className="px-2 py-1">#{m.channel}</td>
                      <td className="max-w-[16rem] truncate px-2 py-1" title={m.content ?? ""}>
                        {m.content ?? <span className="text-muted-foreground">(no text)</span>}
                      </td>
                      <td className="px-2 py-1">
                        {m.verdict ? (
                          <span className="flex flex-col">
                            <span className="font-medium text-danger">
                              {m.verdict.rule}
                              {m.verdict.first ? "" : " (cooldown, delete only)"}
                            </span>
                            <span className="text-muted-foreground">{m.verdict.detail}</span>
                          </span>
                        ) : r.deleted.includes(m.index) ? (
                          <span className="text-danger">deleted with a later hit</span>
                        ) : (
                          <span className="text-muted-foreground">ok</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {r.notes.map((n) => (
              <p key={n} className="text-xs text-muted-foreground">
                {n}
              </p>
            ))}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}
