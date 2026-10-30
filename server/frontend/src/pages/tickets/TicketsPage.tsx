import { useMemo, useRef, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { Plus, Search, Ticket as TicketIcon } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge, CountBadge } from "@/components/ui/badge"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow, SortableHead } from "@/components/ui/table"
import { ResolvedUser } from "@/components/ResolvedUser"
import { ResolvedChannel } from "@/components/ResolvedChannel"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { DensityToggle } from "@/components/patterns/DensityToggle"
import { EmptyState, ErrorState, ListSkeleton, TableSkeleton } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { useTickets, useTicketPanels, useTicketsLive } from "@/api/tickets"
import { LiveCaption } from "@/components/patterns/LivePill"
import { useResolvedUsersMap, userMatches } from "@/hooks/useResolve"
import { describeApiError } from "@/lib/apiError"
import { ApiError } from "@/api/client"
import { useMediaQuery } from "@/hooks/useMediaQuery"
import { formatDateTime, formatRelativeTime, padTicket } from "@/lib/format"
import type { ForbiddenError, Ticket, TicketStatus } from "@/types/api"

const STATUS_TABS: { value: TicketStatus | "all"; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "closed", label: "Closed" },
  { value: "archived", label: "Archived" },
  { value: "all", label: "All" },
]

const STATUS_VARIANT: Record<TicketStatus, "success" | "default" | "destructive"> = {
  open: "success",
  closed: "default",
  archived: "destructive",
}

type SortKey = "number" | "opened"

/**
 * PROTECTED. Tickets list with status tabs, a search + density filter row,
 * a sortable table (rows are links), and a Panels card listing the ticket
 * panels (GET /api/ticket-panels). Backed by GET /api/tickets?status=...,
 * which needs "manage_tickets" server-side; a 403 renders <Forbidden403> in
 * place. useTicketsLive() is mounted once, here.
 */
export default function TicketsPage() {
  const [status, setStatus] = useState<TicketStatus | "all">("open")
  useTicketsLive()
  const ticketsQuery = useTickets(status)
  const openCountQuery = useTickets("open")
  const panelsQuery = useTicketPanels()
  const [search, setSearch] = useState("")
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "number", dir: "desc" })
  const panelsRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()
  const isPhone = useMediaQuery("(max-width: 639px)")

  // Opener/claimer names come from the same resolve cache <ResolvedUser> fills,
  // so searching by name costs no extra request.
  const peopleIds = useMemo(
    () => (ticketsQuery.data ?? []).flatMap((t) => (t.claimed_by ? [t.opened_by, t.claimed_by] : [t.opened_by])),
    [ticketsQuery.data]
  )
  const people = useResolvedUsersMap(peopleIds)

  const rows = useMemo(() => {
    const query = search.trim().toLowerCase()
    const all = ticketsQuery.data ?? []
    const filtered = !query
      ? all
      : all.filter(
          (t) =>
            String(t.ticket_number).includes(query.replace("#", "")) ||
            t.opened_by.toLowerCase().includes(query) ||
            userMatches(people.get(t.opened_by), query) ||
            (t.claimed_by ? userMatches(people.get(t.claimed_by), query) : false) ||
            (t.panel_purpose?.toLowerCase().includes(query) ?? false)
        )
    const dir = sort.dir === "asc" ? 1 : -1
    return [...filtered].sort((a, b) =>
      sort.key === "number"
        ? (a.ticket_number - b.ticket_number) * dir
        : (new Date(a.opened_at).getTime() - new Date(b.opened_at).getTime()) * dir
    )
  }, [ticketsQuery.data, search, sort, people])

  const forbidden =
    ticketsQuery.isError &&
    ticketsQuery.error instanceof ApiError &&
    ticketsQuery.error.status === 403 &&
    isForbiddenError(ticketsQuery.error.body)
      ? (ticketsQuery.error.body as ForbiddenError)
      : null

  if (forbidden) {
    return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />
  }

  function toggleSort(key: SortKey) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }))
  }
  const dirFor = (key: SortKey) => (sort.key === key ? sort.dir : false)
  const openCount = openCountQuery.data?.length
  const label = STATUS_TABS.find((t) => t.value === status)?.label.toLowerCase() ?? status

  return (
    <PageContainer>
      <PageHeader
        title="Tickets"
        description="Support and verification tickets opened from your panels."
        actions={
          <>
            <Button variant="secondary" onClick={() => panelsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })}>
              Manage panels
            </Button>
            <Button asChild>
              <Link to="/dashboard/tickets/panels/new">
                <Plus /> New ticket panel
              </Link>
            </Button>
          </>
        }
      />

      <Card className="gap-0 overflow-visible py-0">
        <Tabs value={status} onValueChange={(v) => setStatus(v as TicketStatus | "all")} className="gap-0">
          <TabsList className="px-3">
            {STATUS_TABS.map((tab) => (
              <TabsTrigger key={tab.value} value={tab.value}>
                {tab.label}
                {tab.value === "open" && openCount ? <CountBadge count={openCount} /> : null}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        <div className="sticky top-14 z-10 flex flex-wrap items-center gap-3 border-b border-border bg-card px-4 py-3">
          <div className="relative w-full sm:w-72">
            <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-subtle-foreground" />
            <Input
              type="text"
              data-page-search
              aria-label="Search tickets"
              placeholder="Search by number, name or purpose"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="h-[30px] w-full pl-9 text-sm max-md:h-11"
            />
          </div>
          <LiveCaption fallbackText="Live updates paused. Refreshing every 30 s." />
          <div className="ml-auto">
            <DensityToggle />
          </div>
        </div>

        {ticketsQuery.isPending ? (isPhone ? <ListSkeleton rows={5} avatar={false} /> : <TableSkeleton rows={5} cols={6} />) : null}

        {ticketsQuery.isError && !forbidden ? (
          <ErrorState
            title="Could not load tickets"
            message={describeApiError(ticketsQuery.error)}
            onRetry={() => void ticketsQuery.refetch()}
            retrying={ticketsQuery.isRefetching}
            showStatusLink
          />
        ) : null}

        {ticketsQuery.data ? (
          ticketsQuery.data.length === 0 ? (
            <EmptyState
              icon={TicketIcon}
              title={status === "all" ? "No tickets yet" : `No ${label} tickets`}
              description={status === "open" ? "Nothing is waiting on staff right now." : "Try a different tab."}
            />
          ) : rows.length === 0 ? (
            <EmptyState icon={Search} title="No matches" description={`No tickets match "${search.trim()}".`} />
          ) : isPhone ? (
            <ul className="flex flex-col divide-y divide-border">
              {rows.map((t) => (
                <li key={t.id}>
                  <Link to={`/dashboard/tickets/${t.id}`} className="flex min-h-12 flex-col gap-1 px-4 py-3 hover:bg-hover">
                    <span className="flex items-center justify-between gap-2">
                      <span className="font-mono text-sm font-medium text-foreground">{padTicket(t.ticket_number)}</span>
                      <Badge variant={STATUS_VARIANT[t.status]}>{t.status}</Badge>
                    </span>
                    <span className="text-sm">
                      <ResolvedUser userId={t.opened_by} />
                    </span>
                    <span className="text-xs text-subtle-foreground">
                      {t.panel_purpose ?? "No purpose"} · {formatRelativeTime(t.opened_at)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableHead direction={dirFor("number")} onSort={() => toggleSort("number")}>
                    Ticket
                  </SortableHead>
                  <TableHead>Opened by</TableHead>
                  <TableHead>Purpose</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Claimed by</TableHead>
                  <SortableHead align="right" direction={dirFor("opened")} onSort={() => toggleSort("opened")}>
                    Opened
                  </SortableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((t) => (
                  <TicketRow key={t.id} ticket={t} onOpen={() => navigate(`/dashboard/tickets/${t.id}`)} />
                ))}
              </TableBody>
            </Table>
          )
        ) : null}
      </Card>

      <div ref={panelsRef} className="scroll-mt-20">
        <Card className="gap-0 py-0">
          <CardHeader className="border-b py-4">
            <CardTitle>Panels</CardTitle>
            <CardDescription>The buttons members use to open tickets.</CardDescription>
          </CardHeader>
          {panelsQuery.isPending ? <ListSkeleton rows={2} avatar={false} /> : null}
          {panelsQuery.isError ? (
            <ErrorState title="Could not load panels" onRetry={() => void panelsQuery.refetch()} retrying={panelsQuery.isRefetching} className="py-6" />
          ) : null}
          {panelsQuery.data && panelsQuery.data.length === 0 ? (
            <EmptyState
              icon={TicketIcon}
              title="No ticket panels yet"
              description="Create a panel to post an Open Ticket button in a channel."
              action={
                <Button asChild>
                  <Link to="/dashboard/tickets/panels/new">New ticket panel</Link>
                </Button>
              }
              className="py-8"
            />
          ) : null}
          {panelsQuery.data && panelsQuery.data.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Channel</TableHead>
                  <TableHead>Purpose</TableHead>
                  <TableHead>Button</TableHead>
                  <TableHead className="text-right">Max per user</TableHead>
                  <TableHead>Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {panelsQuery.data.map((p) => (
                  <TableRow key={p.id}>
                    <TableCell>
                      <ResolvedChannel channelId={p.channel_id} />
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{p.purpose}</Badge>
                    </TableCell>
                    <TableCell>
                      {p.button_emoji ? `${p.button_emoji} ` : ""}
                      {p.button_label}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{p.max_open_per_user}</TableCell>
                    <TableCell className="text-muted-foreground">{formatDateTime(p.created_at)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : null}
        </Card>
      </div>
    </PageContainer>
  )
}

function TicketRow({ ticket, onOpen }: { ticket: Ticket; onOpen: () => void }) {
  return (
    <TableRow className="cursor-pointer" onClick={onOpen}>
      <TableCell>
        <Link
          to={`/dashboard/tickets/${ticket.id}`}
          onClick={(e) => e.stopPropagation()}
          className="rounded-sm font-mono text-xs font-medium text-primary hover:underline"
        >
          {padTicket(ticket.ticket_number)}
        </Link>
      </TableCell>
      <TableCell>
        <ResolvedUser userId={ticket.opened_by} />
      </TableCell>
      <TableCell className="text-muted-foreground">{ticket.panel_purpose ?? "None"}</TableCell>
      <TableCell>
        <Badge variant={STATUS_VARIANT[ticket.status]}>{ticket.status}</Badge>
      </TableCell>
      <TableCell>
        {ticket.claimed_by ? <ResolvedUser userId={ticket.claimed_by} /> : <span className="text-subtle-foreground">Unclaimed</span>}
      </TableCell>
      <TableCell className="text-right text-muted-foreground" title={formatDateTime(ticket.opened_at)}>
        {formatRelativeTime(ticket.opened_at)}
      </TableCell>
    </TableRow>
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
