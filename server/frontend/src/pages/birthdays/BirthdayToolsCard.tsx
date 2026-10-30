import { useRef, useState } from "react"
import { Download, RotateCcw, Send, Upload } from "lucide-react"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { ResolvedUser } from "@/components/ResolvedUser"
import {
  useBirthdayExport,
  useBirthdayImport,
  useBirthdayResetTimers,
  useBirthdayTest,
  type BirthdayOverview,
  type ExportFormat,
  type ImportPreview,
} from "@/api/birthdays"
import { useAuth } from "@/hooks/useAuth"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import type { MemberSearchResult } from "@/types/api"
import { MemberChip, MemberPicker } from "./MemberPicker"

const EXPORTS: { format: ExportFormat; label: string; mime: string }[] = [
  { format: "plaintext", label: "Text (.txt)", mime: "text/plain" },
  { format: "csv", label: "CSV (.csv)", mime: "text/csv" },
  { format: "ics", label: "Calendar (.ics)", mime: "text/calendar" },
]

function download(filename: string, content: string, mime: string) {
  const url = URL.createObjectURL(new Blob([content], { type: `${mime};charset=utf-8` }))
  const a = document.createElement("a")
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1_000)
}

/**
 * /config announce test, /config announce timers-reset, /birthdays export and
 * /birthdays import, as buttons. Everything hits the bot (disabled while it
 * is offline) and writes an audit row.
 */
export function BirthdayToolsCard({ overview }: { overview: BirthdayOverview }) {
  const { toast } = useToast()
  const { user } = useAuth()
  const { offline, reason } = useBotOffline()
  const test = useBirthdayTest()
  const reset = useBirthdayResetTimers()
  const exporter = useBirthdayExport()
  const importer = useBirthdayImport()
  const fileRef = useRef<HTMLInputElement>(null)

  const [testMembers, setTestMembers] = useState<MemberSearchResult[]>([])
  const [confirmReset, setConfirmReset] = useState(false)
  const [pending, setPending] = useState<{ filename: string; content: string } | null>(null)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [importError, setImportError] = useState<string | null>(null)

  const maxMembers = overview.limits.test_members_max
  const maxBytes = overview.limits.import_max_bytes
  const channel = overview.status.channel
  const channelProblem = !channel.id
    ? "Set an announcement channel first (Settings tab)."
    : !channel.exists
      ? "The configured channel no longer exists."
      : !channel.can_send
        ? "The bot can't send messages in the announcement channel."
        : undefined
  const everyone = (overview.settings.announce_message ?? "").includes("%e") || (overview.settings.announce_message_pl ?? "").includes("%e")
  const testIds = testMembers.length ? testMembers.map((m) => m.id) : user ? [user.id] : []

  function runTest() {
    test.mutate(testIds, {
      onSuccess: (r) =>
        toast({ title: "Test announcement sent", description: `#${channel.name ?? "channel"}, ${r.member_count} member(s)`, variant: "success" }),
      onError: (e) => toast({ title: "Could not send the test", description: describeApiError(e), variant: "destructive" }),
    })
  }

  function runReset() {
    reset.mutate(undefined, {
      onSuccess: (r) =>
        toast({
          title: "Today's timers reset",
          description: r.reset
            ? `${r.reset} birthday(s) today will be announced again in a moment.`
            : "Nobody has a birthday today (server timezone), so nothing will be announced.",
          variant: "success",
        }),
      onError: (e) => toast({ title: "Could not reset the timers", description: describeApiError(e), variant: "destructive" }),
    })
  }

  function runExport(format: ExportFormat, mime: string) {
    exporter.mutate(format, {
      onSuccess: (r) => {
        download(r.filename, r.content, mime)
        toast({ title: `Exported ${r.count} birthday(s)`, variant: "success" })
      },
      onError: (e) => toast({ title: "Export failed", description: describeApiError(e), variant: "destructive" }),
    })
  }

  async function pickFile(file: File | undefined) {
    setPreview(null)
    setImportError(null)
    setPending(null)
    if (!file) return
    if (!file.name.toLowerCase().endsWith(".txt")) return setImportError("Please choose a .txt file (a BirthdayBot or plain-text export).")
    if (file.size > maxBytes) return setImportError(`File too large (max ${Math.round(maxBytes / 1_000_000)} MB).`)
    const content = await file.text()
    const p = { filename: file.name, content }
    setPending(p)
    importer.mutate({ ...p, dry_run: true }, {
      onSuccess: setPreview,
      onError: (e) => setImportError(describeApiError(e)),
    })
  }

  function runImport() {
    if (!pending) return
    importer.mutate({ ...pending, dry_run: false }, {
      onSuccess: (r) => {
        setPreview(r)
        setPending(null)
        toast({ title: `Imported ${r.imported ?? 0} birthday(s)`, description: r.skipped ? `${r.skipped} line(s) skipped` : undefined, variant: "success" })
      },
      onError: (e) => setImportError(describeApiError(e)),
    })
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Test announcement</CardTitle>
          <CardDescription>
            Sends the configured announcement to {channel.name ? `#${channel.name}` : "the announcement channel"} for
            up to {maxMembers} members (default: you). Same as /config announce test: no role is given and nothing is
            marked as celebrated. The members are really mentioned{everyone ? ", and the message contains %e, so it pings @everyone" : ""}.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {testMembers.length ? (
            <div className="flex flex-wrap gap-2">
              {testMembers.map((m) => (
                <MemberChip key={m.id} member={m} onRemove={() => setTestMembers((xs) => xs.filter((x) => x.id !== m.id))} />
              ))}
            </div>
          ) : null}
          {testMembers.length < maxMembers ? (
            <MemberPicker
              onPick={(m) => setTestMembers((xs) => (xs.some((x) => x.id === m.id) ? xs : [...xs, m]))}
              excludeIds={testMembers.map((m) => m.id)}
              placeholder="Add a member to the test (optional)"
            />
          ) : null}
          {channelProblem ? <Alert variant="warning">{channelProblem}</Alert> : null}
          {everyone ? <Alert variant="warning" title="This test pings @everyone">The saved message contains %e.</Alert> : null}
          <Button
            className="w-fit"
            onClick={runTest}
            loading={test.isPending}
            disabled={offline || test.isPending || Boolean(channelProblem) || testIds.length === 0}
            title={reason ?? channelProblem}
          >
            <Send /> Send test announcement
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Reset today's timers</CardTitle>
          <CardDescription>
            Same as /config announce timers-reset. Forgets that today's birthdays (in the server's timezone) were
            already announced, so the bot announces them again and gives the birthday role again within a moment. Use
            it after fixing the channel or message. Other days are not touched.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button variant="destructive-outline" onClick={() => setConfirmReset(true)} disabled={offline || reset.isPending} loading={reset.isPending} title={reason}>
            <RotateCcw /> Reset today's timers
          </Button>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Export</CardTitle>
          <CardDescription>
            Every registered birthday with member names. The text file can be imported back. Exports are audited:
            this is members' personal data, keep the file private.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {EXPORTS.map((x) => (
            <Button key={x.format} variant="secondary" onClick={() => runExport(x.format, x.mime)} disabled={offline || exporter.isPending} title={reason}>
              <Download /> {x.label}
            </Button>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Import</CardTitle>
          <CardDescription>
            A BirthdayBot export or this bot's text export (.txt, max {Math.round(maxBytes / 1_000_000)} MB), lines like
            "● Mar-14: 123456789012345678 name | Time zone: Europe/London". You see a preview first; existing birthdays
            for the same members are replaced.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <input
            ref={fileRef}
            type="file"
            accept=".txt,text/plain"
            className="hidden"
            onChange={(e) => {
              void pickFile(e.target.files?.[0])
              e.target.value = ""
            }}
          />
          <Button variant="secondary" className="w-fit" onClick={() => fileRef.current?.click()} disabled={offline || importer.isPending} title={reason}>
            <Upload /> Choose file
          </Button>
          {importError ? <Alert variant="danger">{importError}</Alert> : null}
          {preview ? <ImportSummary preview={preview} /> : null}
          {pending && preview?.dry_run ? (
            <div className="flex gap-2">
              <Button onClick={runImport} loading={importer.isPending} disabled={offline || importer.isPending || preview.valid === 0} title={reason}>
                Import {preview.users} birthday(s)
              </Button>
              <Button variant="ghost" onClick={() => { setPending(null); setPreview(null) }}>
                Cancel
              </Button>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <ConfirmDialog
        open={confirmReset}
        onOpenChange={setConfirmReset}
        title="Reset today's birthday timers?"
        description="Everyone whose birthday is today (server timezone) will be announced again, and get the birthday role again, within a moment."
        confirmLabel="Reset timers"
        onConfirm={runReset}
      />
    </div>
  )
}

function ImportSummary({ preview }: { preview: ImportPreview }) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border p-3 text-sm">
      <p className="font-medium">
        {preview.dry_run ? "Preview" : "Imported"}: {preview.users} member(s) from {preview.valid} valid line(s)
      </p>
      <p className="text-muted-foreground">
        {preview.new} new, {preview.updated} changed, {preview.unchanged} unchanged, {preview.invalid.length} skipped
        {!preview.dry_run && preview.scheduled !== undefined ? `. ${preview.scheduled} birthday(s) queued in the next 14 days.` : ""}
      </p>
      {preview.entries.some((e) => !e.in_guild) ? (
        <p className="text-xs text-muted-foreground">
          {preview.entries.filter((e) => !e.in_guild).length} of them are not in the server right now (kept, but never announced while away).
        </p>
      ) : null}
      {preview.invalid.length ? (
        <details>
          <summary className="cursor-pointer text-warning">Skipped lines</summary>
          <ul className="mt-1 max-h-40 overflow-y-auto text-xs">
            {preview.invalid.map((l) => (
              <li key={l.line}>
                Line {l.line}: {l.reason} <code className="text-muted-foreground">{l.text}</code>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {preview.dry_run && preview.entries.length ? (
        <details>
          <summary className="cursor-pointer">Entries</summary>
          <ul className="mt-1 flex max-h-48 flex-col gap-1 overflow-y-auto">
            {preview.entries.slice(0, 100).map((e) => (
              <li key={e.user_id} className="flex items-center gap-2 text-xs">
                <span className="w-14 tabular-nums">{String(e.birth_month).padStart(2, "0")}-{String(e.birth_day).padStart(2, "0")}</span>
                <ResolvedUser userId={e.user_id} />
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  )
}
