import { useEffect, useRef, useState, type ReactNode } from "react"
import { Link } from "react-router-dom"
import {
  ChevronRight,
  MessageSquareWarning,
  Sparkles,
  Ticket,
  FilePlus2,
  FileText,
  KeyRound,
  type LucideIcon,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Field } from "@/components/ui/field"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog"
import { Card, CardHeader, CardTitle } from "@/components/ui/card"
import { useToast } from "@/components/ui/toast"
import { useAddWarning } from "@/api/moderation"
import { generateQotdQuestion, usePostQotd, useQotdConfig } from "@/api/qotd"
import { isAbortError } from "@/api/client"
import { describeApiError } from "@/lib/apiError"
import { useAuth } from "@/hooks/useAuth"
import { useBotOffline } from "@/hooks/useBotOffline"
import { discordIdError } from "@/lib/format"
import { STAFF_ONLY_PERMISSION } from "@/lib/owner"

interface LinkAction {
  key: string
  label: string
  icon: LucideIcon
  to: string
  requiredPermission?: string
}

const LINK_ACTIONS: LinkAction[] = [
  { key: "open-tickets", label: "Open tickets", icon: Ticket, to: "/dashboard/tickets", requiredPermission: "manage_tickets" },
  { key: "new-panel", label: "New ticket panel", icon: FilePlus2, to: "/dashboard/tickets/panels/new", requiredPermission: "manage_tickets" },
  { key: "new-template", label: "New template", icon: FileText, to: "/dashboard/templates", requiredPermission: "manage_templates" },
  { key: "grant-permission", label: "Grant permission", icon: KeyRound, to: "/dashboard/permissions", requiredPermission: STAFF_ONLY_PERMISSION },
]

const railRowClass =
  "flex min-h-10 w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-base font-medium text-foreground transition-colors duration-[120ms] hover:bg-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"

function RailRow({ icon: Icon, children }: { icon: LucideIcon; children: ReactNode }) {
  return (
    <>
      <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      <span className="flex-1">{children}</span>
      <ChevronRight className="size-4 text-subtle-foreground" aria-hidden />
    </>
  )
}

/**
 * Overview's Quick actions rail card. Warn a user and Generate + post QOTD
 * perform their action in place (dialogs below); the rest jump to a specific,
 * actionable starting point. Every row is permission-filtered.
 */
export function QuickActionsCard() {
  const { permissions } = useAuth()
  const visibleLinks = LINK_ACTIONS.filter((a) => !a.requiredPermission || permissions[a.requiredPermission])
  return (
    <Card className="gap-2">
      <CardHeader>
        <CardTitle>Quick actions</CardTitle>
      </CardHeader>
      <nav aria-label="Quick actions" className="flex flex-col px-3 pb-1">
        {permissions.moderate_members ? (
          <WarnUserDialog
            trigger={
              <button type="button" className={railRowClass}>
                <RailRow icon={MessageSquareWarning}>Warn a user</RailRow>
              </button>
            }
          />
        ) : null}
        {permissions.manage_qotd ? (
          <GenerateQotdDialog
            trigger={
              <button type="button" className={railRowClass}>
                <RailRow icon={Sparkles}>Generate and post QOTD</RailRow>
              </button>
            }
          />
        ) : null}
        {visibleLinks.map((a) => (
          <Link key={a.key} to={a.to} className={railRowClass}>
            <RailRow icon={a.icon}>{a.label}</RailRow>
          </Link>
        ))}
      </nav>
    </Card>
  )
}

/** Warn a user by Discord ID. `trigger` is the element that opens the dialog. */
export function WarnUserDialog({ trigger }: { trigger: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [userId, setUserId] = useState("")
  const [touched, setTouched] = useState(false)
  const [reason, setReason] = useState("")
  const addWarning = useAddWarning()
  const { toast } = useToast()
  const bot = useBotOffline()

  const idError = touched ? discordIdError(userId) : null
  const valid = userId.trim().length > 0 && discordIdError(userId) === null
  const canSubmit = valid && !addWarning.isPending && !bot.offline

  function handleSubmit() {
    setTouched(true)
    if (!canSubmit) return
    addWarning.mutate(
      { user_id: userId.trim(), reason: reason.trim() || null },
      {
        onSuccess: () => {
          toast({ title: "Warning issued", variant: "success" })
          setOpen(false)
          setUserId("")
          setReason("")
          setTouched(false)
        },
        onError: (err) => {
          toast({
            title: "Failed to issue warning",
            description: err instanceof Error ? err.message : undefined,
            variant: "destructive",
          })
        },
      }
    )
  }

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Warn a user</AlertDialogTitle>
          <AlertDialogDescription>
            Enter the Discord user ID to warn. This adds to their warning history. For full context (past
            warnings, kick/ban) use the Moderation page instead.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <form
          className="flex flex-col gap-3 py-2"
          onSubmit={(e) => {
            e.preventDefault()
            handleSubmit()
          }}
        >
          <Field label="User ID" error={idError} hint="Right-click a member in Discord with Developer Mode on, then Copy User ID.">
            {(p) => (
              <Input
                {...p}
                className="font-mono"
                inputMode="numeric"
                autoComplete="off"
                value={userId}
                onBlur={() => setTouched(true)}
                onChange={(e) => setUserId(e.target.value)}
                placeholder="123456789012345678"
              />
            )}
          </Field>
          <Field label="Reason (optional)">
            {(p) => <Textarea {...p} value={reason} onChange={(e) => setReason(e.target.value)} rows={2} />}
          </Field>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button type="submit" disabled={!canSubmit} loading={addWarning.isPending} title={bot.reason}>
              {addWarning.isPending ? "Warning" : "Issue warning"}
            </Button>
          </AlertDialogFooter>
        </form>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** Generate a question and post it to the configured QOTD channel. */
export function GenerateQotdDialog({ trigger }: { trigger: ReactNode }) {
  const [open, setOpen] = useState(false)
  // One explicit state machine: the question shown is the only one that can
  // be posted, and nothing can be posted while a (re)generation is running.
  const [state, setState] = useState<QotdDialogState>({ kind: "idle" })
  const abortRef = useRef<AbortController | null>(null)
  const configQuery = useQotdConfig()
  const post = usePostQotd()
  const { toast } = useToast()
  const bot = useBotOffline()

  const channelId = configQuery.data?.config?.channel_id ?? null

  // Abort any in-flight polling when the dialog unmounts.
  useEffect(() => () => abortRef.current?.abort(), [])

  function startGenerate() {
    abortRef.current?.abort()
    const controller = new AbortController()
    abortRef.current = controller
    setState({ kind: "generating" })
    generateQotdQuestion(null, controller.signal)
      .then((data) => {
        // A superseded or cancelled run must never replace what is shown.
        if (abortRef.current !== controller) return
        setState({ kind: "ready", text: data.question, source: data.source })
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || abortRef.current !== controller) return
        setState({ kind: "error", message: describeApiError(err, "Could not generate a question.") })
      })
  }

  function handleOpenChange(next: boolean) {
    // Closing while posting would hide the outcome; the post itself is quick.
    if (!next && post.isPending) return
    setOpen(next)
    if (next) {
      startGenerate()
    } else {
      abortRef.current?.abort()
      abortRef.current = null
      setState({ kind: "idle" })
    }
  }

  function handlePost() {
    if (state.kind !== "ready" || !channelId) return
    // Post exactly the question on screen (snapshot at click time).
    const snapshot = { question: state.text, source: state.source, channel_id: channelId }
    post.mutate(snapshot, {
      onSuccess: (data) => {
        toast({
          title: "Question of the Day posted",
          description: data.warning,
          variant: data.warning ? "default" : "success",
        })
        setOpen(false)
        setState({ kind: "idle" })
      },
      onError: (err) => {
        toast({
          title: "Failed to post QOTD",
          description: describeApiError(err, "The question was not posted."),
          variant: "destructive",
        })
      },
    })
  }

  const generating = state.kind === "generating"
  return (
    <AlertDialog open={open} onOpenChange={handleOpenChange}>
      <AlertDialogTrigger asChild>{trigger}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Question of the Day</AlertDialogTitle>
          <AlertDialogDescription>
            {channelId
              ? "Posts to the configured QOTD channel. Edit the schedule and channel on the QOTD page."
              : "No QOTD channel is configured yet. Set one on the QOTD page before posting."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <div className="py-2" aria-live="polite">
          {state.kind === "ready" ? (
            <blockquote className="rounded-lg border-l-4 border-primary bg-muted px-3 py-2 text-base text-foreground">
              {state.text}
            </blockquote>
          ) : state.kind === "error" ? (
            <p className="text-sm text-danger">{state.message}</p>
          ) : (
            <p className="text-sm text-muted-foreground">Generating… large models can take a minute or two.</p>
          )}
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={post.isPending}>Cancel</AlertDialogCancel>
          <Button type="button" variant="secondary" disabled={generating || post.isPending} onClick={startGenerate}>
            {state.kind === "error" ? "Try again" : "Regenerate"}
          </Button>
          <Button
            type="button"
            disabled={state.kind !== "ready" || !channelId || bot.offline}
            loading={post.isPending}
            title={bot.reason}
            onClick={handlePost}
          >
            {post.isPending ? "Posting" : "Post now"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

type QotdDialogState =
  | { kind: "idle" }
  | { kind: "generating" }
  | { kind: "ready"; text: string; source: string }
  | { kind: "error"; message: string }
