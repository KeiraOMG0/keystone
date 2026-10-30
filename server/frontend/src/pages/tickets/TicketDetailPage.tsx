import { useMemo, useState } from "react"
import { useParams, useNavigate, Link } from "react-router-dom"
import { ArrowLeft, Ellipsis, Lock, MessageSquare, Search, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { CopyButton } from "@/components/ui/copy-button"
import { ResolvedUser } from "@/components/ResolvedUser"
import { ResolvedChannel } from "@/components/ResolvedChannel"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState, ErrorState, ListSkeleton } from "@/components/patterns/states"
import { useBreadcrumb } from "@/components/layout/breadcrumbs"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { useToast } from "@/components/ui/toast"
import {
  useClaimTicket,
  useCloseTicket,
  useDeleteTicket,
  useTicket,
  useTicketMessages,
  useTicketsLive,
} from "@/api/tickets"
import { ApiError } from "@/api/client"
import { describeApiError } from "@/lib/apiError"
import { useBotOffline } from "@/hooks/useBotOffline"
import { useResolvedUsersMap, userMatches } from "@/hooks/useResolve"
import { formatDateTime, formatRelativeTime, padTicket } from "@/lib/format"
import type { ForbiddenError } from "@/types/api"

/**
 * PROTECTED. Full detail for a single ticket plus Claim / Close / Delete.
 * Backed by GET /api/tickets/{ticketId} and the POST .../close|claim routes,
 * all needing "manage_tickets" server-side.
 *
 * Close locks the member out and deletes the Discord channel 30 seconds later
 * (the ticket then shows as archived; record + chat log stay), so it has a
 * confirm. Delete is more destructive still (erases the record) and asks you
 * to type the ticket number. Claim has no reverse endpoint, so it is
 * immediate with a plain toast (never a fake Undo). There is no Archive
 * action: archiving is what Close turns into after the delay.
 */
export default function TicketDetailPage() {
  const { ticketId } = useParams<{ ticketId: string }>()
  const navigate = useNavigate()
  const id = ticketId ?? ""
  const { toast } = useToast()
  const bot = useBotOffline()
  const [confirm, setConfirm] = useState<null | "close" | "delete">(null)
  const [logSearch, setLogSearch] = useState("")

  useTicketsLive()
  const ticketQuery = useTicket(id)
  const closeTicket = useCloseTicket()
  const claimTicket = useClaimTicket()
  const deleteTicket = useDeleteTicket()

  const ticket = ticketQuery.data
  const isArchived = ticket?.status === "archived"
  // Open tickets: the chat log polls (TICKET_MESSAGES_POLL_MS); the bot publishes
  // no live event per chat message, only status changes (which also refresh it).
  const messagesQuery = useTicketMessages(id, ticket !== undefined, ticket?.status === "open")
  useBreadcrumb(ticket ? padTicket(ticket.ticket_number) : null)

  const authorIds = useMemo(() => (messagesQuery.data?.messages ?? []).map((m) => m.author_id), [messagesQuery.data])
  const authors = useResolvedUsersMap(authorIds)
  const filteredMessages = useMemo(() => {
    const q = logSearch.trim().toLowerCase()
    const msgs = messagesQuery.data?.messages ?? []
    return q
      ? msgs.filter((m) => m.content.toLowerCase().includes(q) || m.author_id.includes(q) || userMatches(authors.get(m.author_id), q))
      : msgs
  }, [messagesQuery.data, logSearch, authors])

  const forbidden =
    ticketQuery.isError &&
    ticketQuery.error instanceof ApiError &&
    ticketQuery.error.status === 403 &&
    isForbiddenError(ticketQuery.error.body)
      ? (ticketQuery.error.body as ForbiddenError)
      : null

  if (forbidden) {
    return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />
  }

  const notFound = ticketQuery.isError && ticketQuery.error instanceof ApiError && ticketQuery.error.status === 404

  const failToast = (title: string) => (error: unknown) =>
    toast({ title, description: describeApiError(error), variant: "destructive" })

  function handleClaim() {
    claimTicket.mutate(id, { onSuccess: () => toast({ title: "Ticket claimed", variant: "success" }), onError: failToast("Failed to claim ticket") })
  }
  function handleClose() {
    closeTicket.mutate(id, {
      onSuccess: () =>
        toast({ title: "Ticket closed", description: "Its Discord channel will be deleted in 30 seconds; the record stays viewable here.", variant: "success" }),
      onError: failToast("Failed to close ticket"),
    })
  }
  function handleDelete() {
    deleteTicket.mutate(id, {
      onSuccess: () => {
        toast({ title: "Ticket deleted", variant: "success" })
        navigate("/dashboard/tickets")
      },
      onError: failToast("Failed to delete ticket"),
    })
  }

  const back = (
    <Button asChild variant="ghost" size="sm" className="-ml-2 w-fit">
      <Link to="/dashboard/tickets">
        <ArrowLeft /> Back to tickets
      </Link>
    </Button>
  )

  if (ticketQuery.isPending) {
    return (
      <PageContainer>
        {back}
        <div className="flex flex-col gap-2" aria-busy="true">
          <Skeleton className="h-8 w-56" />
          <Skeleton className="h-4 w-80 max-w-full" />
        </div>
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
          <Card className="gap-0 py-0">
            <ListSkeleton rows={4} />
          </Card>
          <Skeleton className="h-64 w-full" />
        </div>
      </PageContainer>
    )
  }

  if (notFound) {
    return (
      <PageContainer>
        {back}
        <Card>
          <EmptyState
            icon={Search}
            title="Ticket not found"
            description="This ticket does not exist or belongs to a different server."
            action={
              <Button asChild variant="secondary">
                <Link to="/dashboard/tickets">Back to tickets</Link>
              </Button>
            }
          />
        </Card>
      </PageContainer>
    )
  }

  if (ticketQuery.isError || !ticket) {
    return (
      <PageContainer>
        {back}
        <Card>
          <ErrorState
            title="Could not load ticket"
            message={describeApiError(ticketQuery.error, "The ticket request failed.")}
            onRetry={() => void ticketQuery.refetch()}
            retrying={ticketQuery.isRefetching}
            showStatusLink
          />
        </Card>
      </PageContainer>
    )
  }

  const statusVariant = ticket.status === "open" ? "success" : ticket.status === "archived" ? "destructive" : "default"
  const number = padTicket(ticket.ticket_number)
  const canClaim = ticket.status === "open" && ticket.claimed_by === null
  const canClose = ticket.status === "open"

  return (
    <PageContainer>
      {back}
      <PageHeader
        title={`Ticket ${number}`}
        badge={
          <Badge variant={statusVariant} dot>
            {ticket.status}
          </Badge>
        }
        description={
          <>
            {ticket.panel_purpose ?? "No purpose"}, opened <ResolvedUser userId={ticket.opened_by} />{" "}
            <span title={formatDateTime(ticket.opened_at)}>{formatRelativeTime(ticket.opened_at)}</span>
          </>
        }
        actions={
          <>
            <Button variant="secondary" onClick={handleClaim} loading={claimTicket.isPending} disabled={!canClaim || bot.offline} title={bot.reason}>
              {claimTicket.isPending ? "Claiming" : ticket.claimed_by !== null ? "Claimed" : "Claim"}
            </Button>
            <Button variant="secondary" onClick={() => setConfirm("close")} loading={closeTicket.isPending} disabled={!canClose || bot.offline} title={bot.reason}>
              {closeTicket.isPending ? "Closing" : "Close ticket"}
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="secondary" size="icon" aria-label="More actions">
                  <Ellipsis />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                <DropdownMenuItem danger disabled={!isArchived} onSelect={() => setConfirm("delete")}>
                  <Trash2 /> Delete
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </>
        }
      />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <Card className="gap-0 py-0">
          <CardHeader className="border-b py-4">
            <CardTitle>Chat log</CardTitle>
            <CardDescription>
              Plain-text messages sent in this ticket's channel. Attachments and voice messages are never captured. Off by
              default server-side until staff opt in.
              {ticket.status === "open" ? " Refreshes every 10 seconds while the ticket is open." : null}
            </CardDescription>
          </CardHeader>
          <>
              {messagesQuery.data?.enabled && messagesQuery.data.messages.length > 0 ? (
                <div className="flex items-center gap-3 border-b border-border px-5 py-3">
                  <div className="relative flex-1">
                    <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-subtle-foreground" />
                    <Input
                      type="text"
                      data-page-search
                      aria-label="Search the chat log"
                      placeholder="Search messages or authors"
                      value={logSearch}
                      onChange={(e) => setLogSearch(e.target.value)}
                      className="h-[30px] w-full pl-9 text-sm max-md:h-11"
                    />
                  </div>
                  <Badge variant="outline">Captured</Badge>
                </div>
              ) : null}
              {messagesQuery.isPending ? <ListSkeleton rows={3} /> : null}
              {messagesQuery.isError ? (
                <ErrorState
                  title="Could not load the chat log"
                  message={describeApiError(messagesQuery.error)}
                  onRetry={() => void messagesQuery.refetch()}
                  retrying={messagesQuery.isRefetching}
                  className="py-8"
                />
              ) : null}
              {messagesQuery.data && !messagesQuery.data.enabled ? (
                <EmptyState icon={MessageSquare} title="Chat logging is disabled" description="Turn it on in Settings under Tickets to capture messages." />
              ) : null}
              {messagesQuery.data?.enabled ? (
                messagesQuery.data.messages.length === 0 ? (
                  <EmptyState icon={MessageSquare} title="No messages recorded" description="Messages sent in the ticket channel will be captured here." />
                ) : filteredMessages.length === 0 ? (
                  <p className="px-5 py-8 text-center text-sm text-muted-foreground">No messages match "{logSearch.trim()}".</p>
                ) : (
                  <ul className="flex max-h-[560px] flex-col divide-y divide-border overflow-y-auto">
                    {filteredMessages.map((msg) => (
                      <li key={msg.id} className="px-5 py-3">
                        <div className="flex items-center justify-between gap-2 text-sm">
                          <span className="font-medium">
                            <ResolvedUser userId={msg.author_id} />
                          </span>
                          <span className="text-xs text-subtle-foreground">{formatDateTime(msg.sent_at)}</span>
                        </div>
                        <p className="mt-1 pl-8 text-base break-words whitespace-pre-wrap text-foreground">{msg.content}</p>
                      </li>
                    ))}
                  </ul>
                )
              ) : null}
          </>
        </Card>

        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2.5 text-sm">
                <dt className="text-muted-foreground">Opened by</dt>
                <dd>
                  <ResolvedUser userId={ticket.opened_by} />
                </dd>
                <dt className="text-muted-foreground">Panel purpose</dt>
                <dd>{ticket.panel_purpose ?? "None"}</dd>
                <dt className="text-muted-foreground">Claimed by</dt>
                <dd>{ticket.claimed_by ? <ResolvedUser userId={ticket.claimed_by} /> : <span className="text-subtle-foreground">Unclaimed</span>}</dd>
                <dt className="text-muted-foreground">Opened at</dt>
                <dd>{formatDateTime(ticket.opened_at)}</dd>
                <dt className="text-muted-foreground">Closed at</dt>
                <dd>{ticket.closed_at ? formatDateTime(ticket.closed_at) : "None"}</dd>
                <dt className="text-muted-foreground">Archived at</dt>
                <dd>{ticket.archived_at ? formatDateTime(ticket.archived_at) : "None"}</dd>
                <dt className="text-muted-foreground">Channel</dt>
                <dd className="flex items-center gap-1">
                  <span className="rounded-md bg-muted px-1.5 py-0.5 font-mono text-xs">
                    <ResolvedChannel channelId={ticket.channel_id} />
                  </span>
                  <CopyButton value={ticket.channel_id} label="Copy channel ID" />
                </dd>
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Actions</CardTitle>
              <CardDescription>
                Claim marks you as the assigned staff member (only unclaimed open tickets). Close locks the member out and
                deletes the Discord channel 30 seconds later; the ticket then shows as archived here with its chat log. Open
                tickets with no messages for 7 days are closed automatically once a week.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              <Button variant="secondary" className="w-full" onClick={handleClaim} disabled={!canClaim || claimTicket.isPending || bot.offline} title={bot.reason}>
                Claim
              </Button>
              <Button variant="secondary" className="w-full" onClick={() => setConfirm("close")} disabled={!canClose || closeTicket.isPending || bot.offline} title={bot.reason}>
                <Lock /> Close ticket
              </Button>
              {isArchived ? (
                <div className="mt-2 flex flex-col gap-2 border-t border-border pt-4">
                  <p className="text-xs font-semibold tracking-[0.06em] text-danger uppercase">Danger zone</p>
                  <p className="text-xs text-muted-foreground">
                    Delete removes this ticket's record and chat log entirely (this also happens automatically 30 days after
                    archiving).
                  </p>
                  <Button variant="destructive-outline" className="w-full" disabled={deleteTicket.isPending} loading={deleteTicket.isPending} onClick={() => setConfirm("delete")}>
                    <Trash2 /> {deleteTicket.isPending ? "Deleting" : "Delete"}
                  </Button>
                </div>
              ) : null}
            </CardContent>
          </Card>
        </div>
      </div>

      <ConfirmDialog
        open={confirm === "close"}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={`Close ticket ${number}?`}
        description="The member is locked out right away and the Discord channel is deleted 30 seconds later. The ticket record and chat log stay viewable here (it moves to Archived)."
        confirmLabel="Close ticket"
        confirmIcon={<Lock />}
        onConfirm={handleClose}
      />
      <ConfirmDialog
        open={confirm === "delete"}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={`Permanently delete ticket ${number}?`}
        description="This PERMANENTLY erases the ticket and its chat log from the database. There is no recovery: the record will not be viewable here afterward."
        confirmLabel="Delete ticket"
        confirmIcon={<Trash2 />}
        typeToConfirm={String(ticket.ticket_number)}
        onConfirm={handleDelete}
      />
    </PageContainer>
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
