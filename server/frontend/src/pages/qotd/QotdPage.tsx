import { useEffect, useRef, useState } from "react"
import { History as HistoryIcon, ListChecks, Search, Sparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Progress } from "@/components/ui/progress"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { Field, CharCounter } from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useToast } from "@/components/ui/toast"
import { ResolvedChannel } from "@/components/ResolvedChannel"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { SaveBar } from "@/components/patterns/SaveBar"
import { EmptyState, ErrorState, TableSkeleton } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { ApiError } from "@/api/client"
import { describeApiError } from "@/lib/apiError"
import { useBotOffline } from "@/hooks/useBotOffline"
import { usePersistentString } from "@/lib/storage"
import { formatDateTime } from "@/lib/format"
import {
  useGenerateQotd,
  useQotdChannels,
  useQotdConfig,
  useQotdFallbackQuestions,
  useQotdHistory,
  useQotdModels,
  useQotdRoles,
  useUpdateQotdConfig,
  usePostQotd,
} from "@/api/qotd"
import type { ForbiddenError, QotdSource } from "@/types/api"

const TABS = ["generate", "schedule", "history", "fallback"] as const
type Tab = (typeof TABS)[number]

/**
 * PROTECTED. QOTD (Question of the Day) management: manual generate -> preview
 * -> post, the daily-schedule config that cogs/qotd.py's `_scheduler` already
 * reads every 60 seconds (this page edits that config, it adds no second
 * scheduler), a history table, and a read-only view of the hardcoded fallback
 * pool. Backed by /api/qotd/* (needs "manage_qotd"); a 403 renders
 * <Forbidden403> in place. The header's Generate question button jumps to the
 * Generate tab and runs it.
 */
export default function QotdPage() {
  const configQuery = useQotdConfig()
  const [tab, setTab] = usePersistentString<Tab>("kb-tab-qotd", "generate", TABS)
  const [autoGenerate, setAutoGenerate] = useState(0)

  const forbidden =
    configQuery.isError &&
    configQuery.error instanceof ApiError &&
    configQuery.error.status === 403 &&
    isForbiddenError(configQuery.error.body)
      ? (configQuery.error.body as ForbiddenError)
      : null

  if (forbidden) {
    return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />
  }

  return (
    <PageContainer width="form">
      <PageHeader
        title="Question of the Day"
        description="Generate, schedule and review daily questions."
        actions={
          <Button
            onClick={() => {
              setTab("generate")
              setAutoGenerate((n) => n + 1)
            }}
          >
            <Sparkles /> Generate question
          </Button>
        }
      />

      <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
        <TabsList>
          <TabsTrigger value="generate">Generate &amp; Post</TabsTrigger>
          <TabsTrigger value="schedule">Schedule</TabsTrigger>
          <TabsTrigger value="history">History</TabsTrigger>
          <TabsTrigger value="fallback">Fallback pool</TabsTrigger>
        </TabsList>

        <TabsContent value="generate">
          <GenerateAndPostCard autoGenerate={autoGenerate} />
        </TabsContent>
        <TabsContent value="schedule">
          <ScheduleConfigCard />
        </TabsContent>
        <TabsContent value="history">
          <HistoryCard />
        </TabsContent>
        <TabsContent value="fallback">
          <FallbackPoolCard />
        </TabsContent>
      </Tabs>
    </PageContainer>
  )
}

// ---------------------------------------------------------------------------
// Generate + preview + post
// ---------------------------------------------------------------------------

/**
 * Elapsed-time indicator while a question generates. The real duration is
 * unknown (up to 3 model attempts, cold model loads), so the bar eases toward
 * ~95% and only the server response ends it.
 */
function GenerateProgress() {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    const start = Date.now()
    const id = window.setInterval(() => setElapsed((Date.now() - start) / 1000), 250)
    return () => window.clearInterval(id)
  }, [])
  const pct = 95 * (1 - Math.exp(-elapsed / 30))
  return (
    <div className="flex max-w-sm flex-col gap-1.5" role="status" aria-live="polite">
      <Progress value={pct} />
      <p className="text-xs text-muted-foreground">
        Generating… {Math.floor(elapsed)}s elapsed
        {elapsed > 10 ? " · the model may retry up to 3 times; after about 2½ minutes a fallback question is used" : ""}
      </p>
    </div>
  )
}

function GenerateAndPostCard({ autoGenerate }: { autoGenerate: number }) {
  const channelsQuery = useQotdChannels()
  const rolesQuery = useQotdRoles()
  const modelsQuery = useQotdModels()
  const configQuery = useQotdConfig()
  const generate = useGenerateQotd()
  const post = usePostQotd()
  const { toast } = useToast()
  const bot = useBotOffline()

  const [model, setModel] = useState("")
  const [question, setQuestion] = useState("")
  const [source, setSource] = useState<QotdSource | string>("")
  const [channelId, setChannelId] = useState("")
  const [pingRoleId, setPingRoleId] = useState("")
  const [useEmojis, setUseEmojis] = useState(false)

  // Pre-fill the channel from the daily config once it loads.
  const cfg = configQuery.data?.config
  useEffect(() => {
    if (cfg && channelId === "" && cfg.channel_id) {
      setChannelId(String(cfg.channel_id))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg])

  function handleGenerate() {
    generate.mutate(model || null, {
      onSuccess: (data) => {
        setQuestion(data.question)
        setSource(data.source)
      },
      onError: (error) => {
        toast({
          title: "Failed to generate a question",
          description: describeApiError(error),
          variant: "destructive",
        })
      },
    })
  }

  // The header's "Generate question" bumps this counter to run a generation here.
  const lastAuto = useRef(0)
  useEffect(() => {
    if (autoGenerate > lastAuto.current) {
      lastAuto.current = autoGenerate
      handleGenerate()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoGenerate])

  function handlePost() {
    if (!question.trim() || !channelId) return
    post.mutate(
      {
        question: question.trim(),
        source: source || "custom",
        // channelId/pingRoleId are Discord snowflakes: pass the string through
        // unchanged. Number() silently corrupts ids past MAX_SAFE_INTEGER (the
        // confirmed production bug that 404'd a mangled channel id).
        channel_id: channelId,
        use_emojis: useEmojis,
        ping_role_id: pingRoleId || null,
      },
      {
        onSuccess: (data) => {
          toast({
            title: "Question of the Day posted",
            description: data.warning ?? `Message ID ${data.message_id}`,
            variant: data.warning ? "default" : "success",
          })
          setQuestion("")
          setSource("")
        },
        onError: (error) => {
          toast({
            title: "Failed to post",
            description: describeApiError(error),
            variant: "destructive",
          })
        },
      }
    )
  }

  const sourceLabel = source === "ai" ? "AI-generated" : source === "fallback" ? "Fallback list" : source ? "Custom" : null
  const aiFailed = generate.data?.errors && generate.data.errors.length > 0

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Generate a question</CardTitle>
          <CardDescription>Generate and preview a question first. Nothing posts to Discord until you click Post to channel.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Field label="Model" hint="Optional. Defaults to the configured model.">
            {(p) => (
              <Select {...p} value={model} onChange={(e) => setModel(e.target.value)} className="max-w-sm">
                <option value="">Use configured default</option>
                {(modelsQuery.data?.models ?? []).map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <div>
            <Button type="button" onClick={handleGenerate} loading={generate.isPending}>
              <Sparkles /> {generate.isPending ? "Generating" : "Generate question"}
            </Button>
          </div>
          {generate.isPending ? <GenerateProgress /> : null}
          {aiFailed ? (
            <Alert variant="warning" title="The AI model was not available">
              The question below came from the fallback pool instead.
              <details className="mt-1 text-xs">
                <summary className="cursor-pointer">Attempt failures</summary>
                <ul className="mt-1 list-disc pl-5">
                  {generate.data?.errors.map((err, i) => (
                    <li key={i}>{err}</li>
                  ))}
                </ul>
              </details>
            </Alert>
          ) : null}
        </CardContent>
      </Card>

      {question ? (
        <Card>
          <CardHeader>
            <CardTitle>Preview{sourceLabel ? <Badge variant="outline" className="ml-2 align-middle">{sourceLabel}</Badge> : null}</CardTitle>
            <CardDescription>Editable before posting.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="rounded-lg border-l-4 border-primary bg-muted p-3">
              <Textarea
                aria-label="Question"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                rows={3}
                maxLength={500}
                className="w-full resize-none border-transparent bg-transparent text-md"
              />
              <div className="mt-1 text-right">
                <CharCounter value={question.length} max={500} />
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Post to channel" required>
                {(p) => (
                  <Select {...p} value={channelId} onChange={(e) => setChannelId(e.target.value)}>
                    <option value="">Select a channel…</option>
                    {(channelsQuery.data ?? []).map((c) => (
                      <option key={c.id} value={String(c.id)}>
                        #{c.name}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
              <Field label="Ping role" hint="Optional.">
                {(p) => (
                  <Select {...p} value={pingRoleId} onChange={(e) => setPingRoleId(e.target.value)}>
                    <option value="">No ping</option>
                    {(rolesQuery.data ?? []).map((r) => (
                      <option key={r.id} value={String(r.id)}>
                        {r.name}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            </div>

            <div className="flex items-center gap-2">
              <Checkbox id="qotd-emojis" checked={useEmojis} onCheckedChange={(v) => setUseEmojis(v === true)} />
              <Label htmlFor="qotd-emojis">Use emoji styling in the embed</Label>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" onClick={handleGenerate} variant="secondary" disabled={generate.isPending || post.isPending}>
                Regenerate
              </Button>
              <Button
                type="button"
                onClick={handlePost}
                disabled={!channelId || bot.offline || generate.isPending}
                loading={post.isPending}
                title={generate.isPending ? "Wait for the new question first." : bot.reason}
              >
                {post.isPending ? "Posting" : "Post to channel"}
              </Button>
              <span className="text-xs text-muted-foreground">Posting is final: it messages the channel.</span>
            </div>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Daily schedule config: edits the SAME qotd_config row cogs/qotd.py's
// `_scheduler` tasks.loop already checks every 60 seconds.
// ---------------------------------------------------------------------------

function ScheduleConfigCard() {
  const configQuery = useQotdConfig()
  const channelsQuery = useQotdChannels()
  const rolesQuery = useQotdRoles()
  const modelsQuery = useQotdModels()
  const update = useUpdateQotdConfig()
  const { toast } = useToast()

  const cfg = configQuery.data?.config

  // null = untouched (falls back to the server value).
  const [channelId, setChannelId] = useState<string | null>(null)
  const [approvalChannelId, setApprovalChannelId] = useState<string | null>(null)
  const [postHour, setPostHour] = useState<string | null>(null)
  const [postMinute, setPostMinute] = useState<string | null>(null)
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [model, setModel] = useState<string | null>(null)
  const [pingRoleId, setPingRoleId] = useState<string | null>(null)
  const [staffPingRoleId, setStaffPingRoleId] = useState<string | null>(null)

  if (configQuery.isPending) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Daily schedule</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4" aria-busy="true">
          <Skeleton className="h-9 w-full max-w-sm" />
          <Skeleton className="h-9 w-full max-w-sm" />
          <Skeleton className="h-9 w-full max-w-sm" />
        </CardContent>
      </Card>
    )
  }

  if (configQuery.isError) {
    return (
      <Card>
        <ErrorState title="Could not load the schedule" onRetry={() => void configQuery.refetch()} retrying={configQuery.isRefetching} showStatusLink />
      </Card>
    )
  }

  const srv = {
    channel: cfg?.channel_id ? String(cfg.channel_id) : "",
    approval: cfg?.approval_channel_id ? String(cfg.approval_channel_id) : "",
    hour: String(cfg?.post_hour ?? 9),
    minute: String(cfg?.post_minute ?? 0),
    enabled: Boolean(cfg?.enabled),
    model: cfg?.model ?? "",
    ping: cfg?.ping_role_id ? String(cfg.ping_role_id) : "",
    staff: cfg?.staff_ping_role_id ? String(cfg.staff_ping_role_id) : "",
  }
  const effectiveChannelId = channelId ?? srv.channel
  const effectiveApprovalChannelId = approvalChannelId ?? srv.approval
  const effectivePostHour = postHour ?? srv.hour
  const effectivePostMinute = postMinute ?? srv.minute
  const effectiveEnabled = enabled ?? srv.enabled
  const effectiveModel = model ?? srv.model
  const effectivePingRoleId = pingRoleId ?? srv.ping
  const effectiveStaffPingRoleId = staffPingRoleId ?? srv.staff

  const changes = [
    effectiveChannelId !== srv.channel,
    effectiveApprovalChannelId !== srv.approval,
    effectivePostHour !== srv.hour,
    effectivePostMinute !== srv.minute,
    effectiveEnabled !== srv.enabled,
    effectiveModel !== srv.model,
    effectivePingRoleId !== srv.ping,
    effectiveStaffPingRoleId !== srv.staff,
  ].filter(Boolean).length

  const hourNum = Number(effectivePostHour)
  const minuteNum = Number(effectivePostMinute)
  const hourError = effectivePostHour.trim() === "" || !Number.isInteger(hourNum) || hourNum < 0 || hourNum > 23 ? "Enter a whole hour from 0 to 23 (UTC)." : null
  const minuteError = effectivePostMinute.trim() === "" || !Number.isInteger(minuteNum) || minuteNum < 0 || minuteNum > 59 ? "Enter a whole minute from 0 to 59." : null

  function discard() {
    setChannelId(null)
    setApprovalChannelId(null)
    setPostHour(null)
    setPostMinute(null)
    setEnabled(null)
    setModel(null)
    setPingRoleId(null)
    setStaffPingRoleId(null)
  }

  function handleSave() {
    if (hourError || minuteError) return
    update.mutate(
      {
        // *_id fields are Discord snowflakes: strings, never Number().
        // post_hour/post_minute are small plain integers, safe to convert.
        channel_id: effectiveChannelId || null,
        post_hour: hourNum,
        post_minute: minuteNum,
        enabled: effectiveEnabled,
        model: effectiveModel || null,
        ping_role_id: effectivePingRoleId || null,
        clear_ping_role: !effectivePingRoleId,
        staff_ping_role_id: effectiveStaffPingRoleId || null,
        clear_staff_ping_role: !effectiveStaffPingRoleId,
        approval_channel_id: effectiveApprovalChannelId || null,
        clear_approval_channel: !effectiveApprovalChannelId,
      },
      {
        onSuccess: () => {
          discard()
          toast({ title: "Schedule saved", variant: "success" })
        },
        onError: (error) => {
          toast({
            title: "Failed to save configuration",
            description: describeApiError(error),
            variant: "destructive",
          })
        },
      }
    )
  }

  const roleOptions = (rolesQuery.data ?? []).map((r) => (
    <option key={r.id} value={String(r.id)}>
      {r.name}
    </option>
  ))
  const channelOptions = (channelsQuery.data ?? []).map((c) => (
    <option key={c.id} value={String(c.id)}>
      #{c.name}
    </option>
  ))

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Daily schedule</CardTitle>
          <CardDescription>
            QOTD already posts automatically once a day: the bot generates a question at the configured time and sends it to the staff
            lounge for approval (approve / deny / regenerate / custom question) before it goes live in the public channel. This does not
            bypass that approval step; it only edits the schedule itself.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          {configQuery.data?.pending_approval ? <Alert variant="info">An approval is currently pending in the staff lounge.</Alert> : null}

          <div className="flex items-start justify-between gap-4">
            <div>
              <Label htmlFor="qotd-enabled">Automatic daily posting</Label>
              <p className="text-xs text-muted-foreground">When on, a question is generated and sent for approval every day.</p>
            </div>
            <Switch id="qotd-enabled" checked={effectiveEnabled} onCheckedChange={setEnabled} />
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Public channel">
              {(p) => (
                <Select {...p} value={effectiveChannelId} onChange={(e) => setChannelId(e.target.value)}>
                  <option value="">Not set</option>
                  {channelOptions}
                </Select>
              )}
            </Field>
            <Field label="Staff approval channel" hint="Required for the daily question: without it no approval request is sent.">
              {(p) => (
                <Select {...p} value={effectiveApprovalChannelId} onChange={(e) => setApprovalChannelId(e.target.value)}>
                  <option value="">Not set</option>
                  {channelOptions}
                </Select>
              )}
            </Field>
            <Field label="Post hour (UTC, 0 to 23)" error={hourError}>
              {(p) => <Input {...p} type="number" value={effectivePostHour} onChange={(e) => setPostHour(e.target.value)} />}
            </Field>
            <Field label="Post minute (0 to 59)" error={minuteError}>
              {(p) => <Input {...p} type="number" value={effectivePostMinute} onChange={(e) => setPostMinute(e.target.value)} />}
            </Field>
            <Field label="Ollama model">
              {(p) => (
                <Select {...p} value={effectiveModel} onChange={(e) => setModel(e.target.value)}>
                  <option value="">Default</option>
                  {(modelsQuery.data?.models ?? []).map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <div />
            <Field label="Ping role in public channel" hint="Optional.">
              {(p) => (
                <Select {...p} value={effectivePingRoleId} onChange={(e) => setPingRoleId(e.target.value)}>
                  <option value="">No ping</option>
                  {roleOptions}
                </Select>
              )}
            </Field>
            <Field label="Ping role in staff lounge" hint="Optional.">
              {(p) => (
                <Select {...p} value={effectiveStaffPingRoleId} onChange={(e) => setStaffPingRoleId(e.target.value)}>
                  <option value="">No ping</option>
                  {roleOptions}
                </Select>
              )}
            </Field>
          </div>

          <p className="text-xs text-muted-foreground">
            {configQuery.data?.last_posted_at ? `Last posted: ${formatDate(configQuery.data.last_posted_at)}` : "Never posted yet."}
          </p>
        </CardContent>
      </Card>

      <SaveBar
        changes={changes}
        onDiscard={discard}
        onSave={handleSave}
        saving={update.isPending}
        saveLabel="Save schedule"
        blockedReason={hourError || minuteError ? "Fix the highlighted time first." : undefined}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

const SOURCE_VARIANT: Record<string, "accent" | "default" | "warning"> = {
  ai: "accent",
  fallback: "default",
  custom: "warning",
}

function HistoryCard() {
  const historyQuery = useQotdHistory(50)

  return (
    <Card className="gap-0 py-0">
      <CardHeader className="border-b py-4">
        <CardTitle>History</CardTitle>
        <CardDescription>The last 50 posted questions, newest first.</CardDescription>
      </CardHeader>
      {historyQuery.isPending ? <TableSkeleton rows={4} cols={4} /> : null}
      {historyQuery.isError ? (
        <ErrorState
          title="Could not load history"
          message={describeApiError(historyQuery.error, "Could not load history.")}
          onRetry={() => void historyQuery.refetch()}
          retrying={historyQuery.isRefetching}
          showStatusLink
        />
      ) : null}
      {historyQuery.data ? (
        historyQuery.data.history.length === 0 ? (
          <EmptyState icon={HistoryIcon} title="No QOTD history yet" description="Posted questions will be listed here." />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Date</TableHead>
                <TableHead>Question</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>Channel</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {historyQuery.data.history.map((entry) => (
                <TableRow key={entry.id}>
                  <TableCell className="text-muted-foreground">{formatDate(entry.posted_at)}</TableCell>
                  <TableCell className="max-w-[420px] whitespace-normal">{entry.question}</TableCell>
                  <TableCell>
                    <Badge variant={SOURCE_VARIANT[entry.source] ?? "default"}>{entry.source}</Badge>
                  </TableCell>
                  <TableCell>{entry.channel_id ? <ResolvedChannel channelId={String(entry.channel_id)} /> : <span className="text-subtle-foreground">Unknown</span>}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )
      ) : null}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Fallback question pool: read-only (still a hardcoded Python constant in
// services/qotd_service.py's FALLBACK_QUESTIONS, not a DB table).
// ---------------------------------------------------------------------------

function FallbackPoolCard() {
  const fallbackQuery = useQotdFallbackQuestions()
  const [expanded, setExpanded] = useState(false)
  const [search, setSearch] = useState("")

  const questions = fallbackQuery.data?.questions ?? []
  const q = search.trim().toLowerCase()
  const matching = q ? questions.filter((x) => x.toLowerCase().includes(q)) : questions
  const visible = expanded || q ? matching : matching.slice(0, 5)

  return (
    <Card>
      <CardHeader>
        <CardTitle>Fallback question pool</CardTitle>
        <CardDescription>
          Read-only. Used automatically when AI generation fails 3 times in a row. This is currently a fixed list in the bot's code (
          {questions.length} questions), not editable from the dashboard.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {fallbackQuery.isPending ? <Skeleton className="h-24 w-full" /> : null}
        {fallbackQuery.isError ? <ErrorState title="Could not load the fallback pool" onRetry={() => void fallbackQuery.refetch()} retrying={fallbackQuery.isRefetching} className="py-6" /> : null}
        {questions.length > 0 ? (
          <>
            <div className="relative w-full sm:w-72">
              <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-subtle-foreground" />
              <Input type="text" data-page-search aria-label="Search fallback questions" placeholder="Search questions" value={search} onChange={(e) => setSearch(e.target.value)} className="w-full pl-9" />
            </div>
            {matching.length === 0 ? (
              <EmptyState icon={ListChecks} title="No matches" description={`No questions match "${search.trim()}".`} className="py-6" />
            ) : (
              <ul className="flex flex-col gap-2 text-base text-foreground">
                {visible.map((text, i) => (
                  <li key={i} className="rounded-lg border border-border bg-muted/40 px-3 py-2">
                    {text}
                  </li>
                ))}
              </ul>
            )}
            {!q && questions.length > 5 ? (
              <Button type="button" variant="secondary" size="sm" className="w-fit" onClick={() => setExpanded((v) => !v)}>
                {expanded ? "Show fewer" : `Show all ${questions.length}`}
              </Button>
            ) : null}
          </>
        ) : null}
      </CardContent>
    </Card>
  )
}

function isForbiddenError(body: unknown): body is ForbiddenError {
  return (
    typeof body === "object" &&
    body !== null &&
    "error" in body &&
    (body as { error: unknown }).error === "forbidden"
  )
}

function formatDate(value: string): string {
  const withZone = value.endsWith("Z") ? value : `${value}Z`
  return Number.isNaN(new Date(withZone).getTime()) ? value : formatDateTime(withZone)
}
