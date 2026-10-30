import { useEffect, useMemo, useState } from "react"
import { Eraser, Mail, ScrollText } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { Alert } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { StaffOnlyBadge } from "@/components/patterns/StaffOnlyBadge"
import { ErrorState } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import {
  checkPurgeAmount,
  useMessageLogExport,
  useMessageLogStatus,
  usePurgeChannels,
  usePurgeJob,
  useStartPurge,
} from "@/api/members"
import { useAuth } from "@/hooks/useAuth"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"

/**
 * Message tools (`/dashboard/message-tools`):
 *  - Purge (`!purge`, manage_messages_bulk): channel, a count 1-1000 or a window
 *    (30s / 15m / 2h / 1d, up to 14 days), optional user ID filter. Over 100
 *    messages or any window needs the confirm dialog (the server refuses
 *    without it). Runs as a bot-side job that this page polls.
 *  - Message log export (`!msglog`, staff only): the bot DMs the .txt to you;
 *    nothing is downloaded in the browser (it holds other members' messages).
 */
export default function ModToolsPage() {
  const { permissions, isStaff } = useAuth()
  const canPurge = Boolean(permissions.manage_messages_bulk)
  if (!canPurge && !isStaff) return <Forbidden403 reason="forbidden" requiredPermission="manage_messages_bulk" />
  return (
    <PageContainer>
      <PageHeader title="Message tools" description="Bulk-delete messages and export the moderation message log." />
      <div className="grid gap-4 lg:grid-cols-2">
        {canPurge ? <PurgeCard /> : null}
        {isStaff ? <MessageLogCard /> : null}
      </div>
    </PageContainer>
  )
}

function PurgeCard() {
  const { toast } = useToast()
  const bot = useBotOffline()
  const channels = usePurgeChannels(true)
  const start = useStartPurge()
  const [channelId, setChannelId] = useState("")
  const [amount, setAmount] = useState("50")
  const [userId, setUserId] = useState("")
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [jobId, setJobId] = useState<string | null>(null)
  const job = usePurgeJob(jobId)

  useEffect(() => {
    const first = channels.data?.channels.find((c) => c.bot_can_purge)?.id
    if (first && !channelId) setChannelId(first)
  }, [channels.data, channelId])

  const check = checkPurgeAmount(amount)
  const userIdValid = userId.trim() === "" || /^\d{15,21}$/.test(userId.trim())
  const channel = channels.data?.channels.find((c) => c.id === channelId)
  const running = job.data?.status === "running" || start.isPending
  useUnsavedChangesGuard(running)

  const scope = useMemo(() => {
    const who = userId.trim() ? ` from user ${userId.trim()}` : ""
    return /^\d+$/.test(amount.trim()) ? `the last ${amount.trim()} messages${who}` : `up to 1000 messages${who} from the last ${amount.trim()}`
  }, [amount, userId])

  function run(confirm: boolean) {
    start.mutate(
      { channel_id: channelId, amount: amount.trim(), user_id: userId.trim() || null, confirm },
      {
        onSuccess: (j) => setJobId(j.job_id),
        onError: (e) => toast({ title: "Could not start the purge", description: describeApiError(e), variant: "destructive" }),
      }
    )
  }

  useEffect(() => {
    if (job.data?.status === "done") toast({ title: "Purge finished", description: `Deleted ${job.data.deleted} message(s).`, variant: "success" })
    if (job.data?.status === "failed") toast({ title: "Purge failed", description: job.data.error ?? undefined, variant: "destructive" })
    // only when the status changes
  }, [job.data?.status])

  return (
    <Card>
      <CardHeader>
        <CardTitle>Purge messages</CardTitle>
        <CardDescription>Same as !purge. Deletions are logged to the moderation log and the audit log.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {channels.isPending ? (
          <Skeleton className="h-9 w-full" />
        ) : channels.isError ? (
          <ErrorState title="Could not load channels" message={describeApiError(channels.error)} onRetry={() => void channels.refetch()} retrying={channels.isRefetching} />
        ) : (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="purge-channel">Channel</Label>
            <Select id="purge-channel" value={channelId} onChange={(e) => setChannelId(e.target.value)}>
              {channels.data.channels.map((c) => (
                <option key={c.id} value={c.id} disabled={!c.bot_can_purge}>
                  #{c.name}
                  {c.category ? ` (${c.category})` : ""}
                  {c.bot_can_purge ? "" : " - bot lacks Manage Messages"}
                </option>
              ))}
            </Select>
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="purge-amount">Count or window</Label>
            <Input id="purge-amount" value={amount} onChange={(e) => setAmount(e.target.value)} aria-invalid={!!check.error} placeholder="50, 15m, 2h, 1d" />
            <span className={check.error ? "text-xs text-danger" : "text-xs text-subtle-foreground"}>
              {check.error ?? "1 to 1000 messages, or a window up to 14 days (deletes up to 1000)."}
            </span>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="purge-user">Only from user ID (optional)</Label>
            <Input id="purge-user" value={userId} onChange={(e) => setUserId(e.target.value)} aria-invalid={!userIdValid} placeholder="Works for users who left" inputMode="numeric" />
            {!userIdValid ? <span className="text-xs text-danger">That is not a Discord user ID.</span> : null}
          </div>
        </div>
        {job.data ? (
          <Alert variant={job.data.status === "failed" ? "danger" : job.data.status === "done" ? "success" : "info"} title={job.data.status === "running" ? "Purging..." : job.data.status === "done" ? "Done" : "Failed"}>
            {job.data.status === "running"
              ? "Keep this page open to see the result; the purge continues on the bot either way."
              : job.data.status === "done"
                ? `Deleted ${job.data.deleted} message(s).`
                : job.data.error}
          </Alert>
        ) : job.isError ? (
          <Alert variant="warning" title="Lost track of the purge">{describeApiError(job.error)}</Alert>
        ) : null}
        {bot.offline ? <p className="text-sm text-danger">{bot.reason}</p> : null}
        <div>
          <Button
            variant="destructive"
            disabled={!!check.error || !userIdValid || !channel?.bot_can_purge || running || bot.offline}
            title={bot.reason}
            loading={running}
            onClick={() => (check.confirm ? setConfirmOpen(true) : run(false))}
          >
            <Eraser /> Purge
          </Button>
        </div>
      </CardContent>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Purge #${channel?.name ?? "channel"}?`}
        description={`Delete ${scope} in #${channel?.name ?? ""}? This can't be undone.`}
        confirmLabel="Delete messages"
        confirmIcon={<Eraser />}
        onConfirm={() => run(true)}
      />
    </Card>
  )
}

function MessageLogCard() {
  const { toast } = useToast()
  const bot = useBotOffline()
  const status = useMessageLogStatus(true)
  const exportLog = useMessageLogExport()
  const channels = usePurgeChannels(true)
  const [kind, setKind] = useState<"user" | "channel" | "deleted">("user")
  const [userId, setUserId] = useState("")
  const [channelId, setChannelId] = useState("")
  const [windowValue, setWindowValue] = useState("24h")

  const userOk = kind !== "user" || /^\d{15,21}$/.test(userId.trim())
  const channelOk = kind !== "channel" || channelId !== ""

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Message log export <StaffOnlyBadge />
        </CardTitle>
        <CardDescription>Same as !msglog. The bot sends the file to your Discord DMs; it is never downloaded here.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {status.isPending ? (
          <Skeleton className="h-5 w-2/3" />
        ) : status.isError ? (
          <p className="text-sm text-danger">{describeApiError(status.error)}</p>
        ) : status.data.enabled ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <ScrollText className="size-4" aria-hidden /> {status.data.rows} message(s) stored ({status.data.deleted} deleted), kept {status.data.retention_days} day(s), {status.data.size_kb} KB.
          </p>
        ) : (
          <Alert variant="warning" title="Message log is off">The bot runs with MESSAGE_LOG_ENABLED=false.</Alert>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="msglog-kind">What</Label>
            <Select id="msglog-kind" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
              <option value="user">One user's messages</option>
              <option value="channel">One channel</option>
              <option value="deleted">Deleted messages only</option>
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="msglog-window">Window</Label>
            <Select id="msglog-window" value={windowValue} onChange={(e) => setWindowValue(e.target.value)}>
              <option value="30m">Last 30 minutes</option>
              <option value="6h">Last 6 hours</option>
              <option value="24h">Last 24 hours</option>
              <option value="3d">Last 3 days</option>
              <option value="7d">Last 7 days</option>
              <option value="30d">Everything kept</option>
            </Select>
          </div>
        </div>
        {kind === "user" ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="msglog-user">User ID</Label>
            <Input id="msglog-user" value={userId} onChange={(e) => setUserId(e.target.value)} inputMode="numeric" aria-invalid={userId !== "" && !userOk} />
          </div>
        ) : kind === "channel" ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="msglog-channel">Channel</Label>
            <Select id="msglog-channel" value={channelId} onChange={(e) => setChannelId(e.target.value)}>
              <option value="">Pick a channel</option>
              {channels.data?.channels.map((c) => (
                <option key={c.id} value={c.id}>
                  #{c.name}
                </option>
              ))}
            </Select>
          </div>
        ) : null}
        <div>
          <Button
            variant="secondary"
            disabled={!userOk || !channelOk || bot.offline || exportLog.isPending || (status.data ? !status.data.enabled : false)}
            title={bot.reason}
            loading={exportLog.isPending}
            onClick={() =>
              exportLog.mutate(
                { kind, user_id: kind === "user" ? userId.trim() : null, channel_id: kind === "channel" ? channelId : null, window: windowValue },
                {
                  onSuccess: (r) => toast({ title: "Sent to your DMs", description: `${r.count} message(s).`, variant: "success" }),
                  onError: (e) => toast({ title: "Could not send the export", description: describeApiError(e), variant: "destructive" }),
                }
              )
            }
          >
            <Mail /> Send to my DMs
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
