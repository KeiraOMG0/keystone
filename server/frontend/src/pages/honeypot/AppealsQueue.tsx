import { useState } from "react"
import { Inbox } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
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
import { useToast } from "@/components/ui/toast"
import { ResolvedUser } from "@/components/ResolvedUser"
import { EmptyState, ErrorState, ListSkeleton } from "@/components/patterns/states"
import { useApproveAppeal, useDenyAppeal, useHoneypotAppeals } from "@/api/honeypot"
import { useBotOffline } from "@/hooks/useBotOffline"
import type { AppealStatus, HoneypotAppeal, HoneypotCatch } from "@/types/api"
import { apiErrorMessage, formatDateTime } from "./format"

const STATUS_BADGE: Record<AppealStatus, "info" | "warning" | "success" | "destructive"> = {
  invited: "info",
  pending: "warning",
  approved: "success",
  denied: "destructive",
}

/**
 * Note field + Approve / Deny for one pending appeal. Neither action offers
 * Undo (approve messages the member; a denied appeal can never be reviewed
 * again), so BOTH confirm first, naming the member.
 * Shared by the queue cards and the rail.
 */
export function AppealReview({ appeal, canManage, compact = false }: { appeal: HoneypotAppeal; canManage: boolean; compact?: boolean }) {
  const approve = useApproveAppeal()
  const deny = useDenyAppeal()
  const { toast } = useToast()
  const bot = useBotOffline()
  const [note, setNote] = useState("")
  const busy = approve.isPending || deny.isPending
  const noteValue = note.trim() === "" ? null : note.trim()
  const blocked = !canManage || busy || bot.offline

  function onApprove() {
    approve.mutate(
      { appeal_id: appeal.id, note: noteValue },
      {
        onSuccess: (r) =>
          toast({
            title: r.unbanned ? "Appeal approved and user unbanned" : "Appeal approved",
            description: r.unbanned ? undefined : "They were not banned, so nothing needed undoing.",
            variant: "success",
          }),
        onError: (err) => toast({ title: "Could not approve", description: apiErrorMessage(err), variant: "destructive" }),
      }
    )
  }

  function onDeny() {
    deny.mutate(
      { appeal_id: appeal.id, note: noteValue },
      {
        onSuccess: () => toast({ title: "Appeal denied", variant: "success" }),
        onError: (err) => toast({ title: "Could not deny", description: apiErrorMessage(err), variant: "destructive" }),
      }
    )
  }

  return (
    <div className={compact ? "flex flex-col gap-2" : "flex flex-col gap-2 sm:flex-row sm:items-center"}>
      <Input
        aria-label="Optional note"
        placeholder="Optional note"
        maxLength={500}
        value={note}
        disabled={!canManage || busy}
        onChange={(e) => setNote(e.target.value)}
        className={compact ? "" : "sm:max-w-sm"}
      />
      <div className="flex gap-2">
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button size={compact ? "sm" : "default"} disabled={blocked} title={bot.reason} loading={approve.isPending}>
              {approve.isPending ? "Approving" : "Approve and unban"}
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Approve this appeal?</AlertDialogTitle>
              <AlertDialogDescription>
                This unbans {appeal.username ?? appeal.user_id} and tells them they can rejoin. It only lifts a
                honeypot ban; a ban for any other reason is left in place. The member is messaged, so this cannot be
                undone.
                {appeal.verified === false
                  ? " This appeal is UNVERIFIED (no DM link): anyone who knows the ID could have filed it, so confirm it is really them first."
                  : ""}
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel autoFocus>Cancel</AlertDialogCancel>
              <Button onClick={onApprove}>Approve and unban</Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button
              variant="destructive-outline"
              size={compact ? "sm" : "default"}
              disabled={blocked}
              title={bot.reason}
              loading={deny.isPending}
            >
              {deny.isPending ? "Denying" : "Deny"}
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Deny this appeal?</AlertDialogTitle>
              <AlertDialogDescription>
                {appeal.username ?? appeal.user_id} stays banned and the appeal is closed as denied
                {noteValue ? " (your note is saved with it)" : ""}. Only pending appeals can be reviewed, so this
                cannot be undone or approved later.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel autoFocus>Cancel</AlertDialogCancel>
              <Button variant="destructive" onClick={onDeny}>
                Deny appeal
              </Button>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </div>
      {!canManage ? <p className="text-xs text-warning">Only members with Ban Members can review appeals.</p> : null}
    </div>
  )
}

function AppealCard({
  appeal,
  catchRow,
  canManage,
}: {
  appeal: HoneypotAppeal
  catchRow: HoneypotCatch | undefined
  canManage: boolean
}) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <ResolvedUser userId={appeal.user_id} />
            <Badge variant={STATUS_BADGE[appeal.status]}>{appeal.status}</Badge>
            {appeal.is_test ? <Badge variant="accent">Test (Debug page)</Badge> : null}
            {appeal.verified === false ? (
              <Badge variant="destructive">
                Unverified - submitted without the DM link, confirm identity before approving
              </Badge>
            ) : appeal.verified_via === "discord_login" ? (
              <Badge variant="outline">Verified by Discord login</Badge>
            ) : null}
            {appeal.status !== "pending" && appeal.outcome_dm_sent !== null ? (
              <Badge variant="outline">{appeal.outcome_dm_sent ? "Outcome DM delivered" : "Outcome DM not delivered"}</Badge>
            ) : null}
            {appeal.rejoined_at ? <Badge variant="success">Rejoined</Badge> : null}
          </div>
          <span className="text-xs text-muted-foreground">
            Submitted {formatDateTime(appeal.submitted_at ?? appeal.created_at)}
          </span>
        </div>

        <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[8rem_1fr]">
          <dt className="text-muted-foreground">Name given</dt>
          <dd>{appeal.name || "not provided"}</dd>
          <dt className="text-muted-foreground">User ID</dt>
          <dd>
            <code className="font-mono text-xs">{appeal.user_id}</code>
          </dd>
          <dt className="text-muted-foreground">What happened</dt>
          <dd className="break-words whitespace-pre-wrap">{appeal.message || "No message provided."}</dd>
          <dt className="text-muted-foreground">Original message</dt>
          <dd>
            {catchRow ? (
              <blockquote className="border-l-2 border-border pl-3 break-words whitespace-pre-wrap text-muted-foreground">
                {catchRow.content || "(no text content)"}
              </blockquote>
            ) : (
              <span className="text-muted-foreground">Not among the most recent catches.</span>
            )}
          </dd>
          {appeal.review_note ? (
            <>
              <dt className="text-muted-foreground">Review note</dt>
              <dd className="break-words whitespace-pre-wrap">{appeal.review_note}</dd>
            </>
          ) : null}
        </dl>

        {appeal.status === "pending" ? <AppealReview appeal={appeal} canManage={canManage} /> : null}
      </CardContent>
    </Card>
  )
}

/** Appeals tab: filterable queue, pending by default. */
export function AppealsQueue({ catches, canManage }: { catches: HoneypotCatch[]; canManage: boolean }) {
  const [filter, setFilter] = useState<AppealStatus | "all">("pending")
  const query = useHoneypotAppeals(filter)
  const appeals = query.data?.appeals ?? []
  const byId = new Map(catches.map((c) => [c.id, c]))

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <label htmlFor="appeal-filter" className="text-sm text-muted-foreground">
          Show
        </label>
        <Select id="appeal-filter" value={filter} onChange={(e) => setFilter(e.target.value as AppealStatus | "all")} className="w-40">
          <option value="pending">Pending</option>
          <option value="approved">Approved</option>
          <option value="denied">Denied</option>
          <option value="invited">Invited</option>
          <option value="all">All</option>
        </Select>
      </div>

      {query.isPending ? (
        <Card className="gap-0 py-0">
          <ListSkeleton rows={3} />
        </Card>
      ) : query.isError ? (
        <Card>
          <ErrorState title="Could not load appeals" message={apiErrorMessage(query.error)} onRetry={() => void query.refetch()} retrying={query.isRefetching} showStatusLink />
        </Card>
      ) : appeals.length === 0 ? (
        <Card>
          <EmptyState
            icon={Inbox}
            title={filter === "pending" ? "No appeals waiting" : "No appeals match"}
            description={filter === "pending" ? "Appeals from banned members show up here for review." : "Try another status filter."}
          />
        </Card>
      ) : (
        appeals.map((a) => <AppealCard key={a.id} appeal={a} catchRow={a.catch_id ? byId.get(a.catch_id) : undefined} canManage={canManage} />)
      )}
    </div>
  )
}
