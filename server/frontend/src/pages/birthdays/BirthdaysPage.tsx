import { useMemo, useState } from "react"
import { Cake, Pencil, Plus, Search, Trash2 } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { LiveCaption } from "@/components/patterns/LivePill"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { EmptyState, ErrorState, TableSkeleton } from "@/components/patterns/states"
import { ResolvedUser } from "@/components/ResolvedUser"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { ApiError } from "@/api/client"
import { useBirthdays, useBirthdaysLive, useRemoveBirthday, type BirthdayEntry, type BirthdayOverview } from "@/api/birthdays"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import type { ForbiddenError } from "@/types/api"
import { BirthdayConfigCard } from "./BirthdayConfigCard"
import { BirthdayEntryDialog } from "./BirthdayEntryDialog"
import { BirthdayToolsCard } from "./BirthdayToolsCard"

type Tab = "birthdays" | "settings" | "tools"

function forbiddenBody(error: unknown): ForbiddenError | null {
  if (error instanceof ApiError && error.status === 403) {
    const body = error.body as ForbiddenError | null
    if (body && body.error === "forbidden") return body
  }
  return null
}

function relative(offset: number): string {
  if (offset === 0) return "Today"
  if (offset === 1) return "Tomorrow"
  if (offset === -1) return "Yesterday"
  return offset > 0 ? `In ${offset} days` : `${-offset} days ago`
}

/**
 * Birthdays (/dashboard/birthdays, perm manage_birthdays; staff bypass). The
 * dashboard twin of /birthday show-nearest, /override, /config and
 * /birthdays export|import: same service, same validation, every change
 * audited. Live on the `birthdays` topic (polls only while the socket is down).
 * Members still set their own birthday with /birthday set in Discord.
 */
export default function BirthdaysPage() {
  const query = useBirthdays()
  const data = query.data
  useBirthdaysLive(data !== undefined)
  const { offline, reason } = useBotOffline()
  const [tab, setTab] = useState<Tab>("birthdays")
  const [dialog, setDialog] = useState<{ open: boolean; entry: BirthdayEntry | null }>({ open: false, entry: null })

  const forbidden = forbiddenBody(query.error)
  if (forbidden) return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />

  return (
    <PageContainer>
      <PageHeader
        title="Birthdays"
        description="Birthday announcements, the birthday role and every registered birthday. Members set their own with /birthday set date in Discord."
        actions={
          <Button onClick={() => setDialog({ open: true, entry: null })} disabled={offline || !data} title={reason}>
            <Plus /> Add birthday
          </Button>
        }
      />

      {query.isPending ? (
        <Card>
          <CardContent className="flex flex-col gap-3">
            <Skeleton className="h-6 w-1/3" />
            <TableSkeleton rows={5} cols={4} />
          </CardContent>
        </Card>
      ) : query.isError || !data ? (
        <Card>
          <ErrorState
            title="Could not load birthdays"
            message={describeApiError(query.error)}
            onRetry={() => query.refetch()}
            retrying={query.isFetching}
            showStatusLink
          />
        </Card>
      ) : (
        <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
          <TabsList>
            <TabsTrigger value="birthdays">Birthdays</TabsTrigger>
            <TabsTrigger value="settings">Settings</TabsTrigger>
            <TabsTrigger value="tools">Test, reset, import/export</TabsTrigger>
          </TabsList>
          <TabsContent value="birthdays" className="flex flex-col gap-4">
            <StatusRow data={data} />
            <NearestCard data={data} />
            <AllBirthdaysCard data={data} onEdit={(entry) => setDialog({ open: true, entry })} />
          </TabsContent>
          <TabsContent value="settings">
            <BirthdayConfigCard overview={data} />
          </TabsContent>
          <TabsContent value="tools">
            <BirthdayToolsCard overview={data} />
          </TabsContent>
        </Tabs>
      )}

      <BirthdayEntryDialog
        open={dialog.open}
        entry={dialog.entry}
        onOpenChange={(open) => setDialog((d) => ({ ...d, open }))}
        existingIds={new Set(data?.entries.map((e) => e.user_id) ?? [])}
      />
    </PageContainer>
  )
}

function StatusRow({ data }: { data: BirthdayOverview }) {
  const { status } = data
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Stat label="Registered" value={String(status.registered)} />
      <Stat label="Queued (next 14 days)" value={String(status.scheduled)} />
      <Stat label="Server timezone" value={status.server_timezone} hint={`Today there: ${status.server_today}`} />
      <Stat
        label="Announcements"
        value={status.channel.name ? `#${status.channel.name}` : "Off"}
        badge={
          !status.channel.id ? (
            <Badge variant="outline">No channel</Badge>
          ) : !status.channel.exists ? (
            <Badge variant="destructive">Channel deleted</Badge>
          ) : !status.channel.can_send ? (
            <Badge variant="warning">Can't send</Badge>
          ) : status.role.id && !status.role.usable ? (
            <Badge variant="warning">Role not assignable</Badge>
          ) : (
            <Badge variant="success" dot>On</Badge>
          )
        }
      />
    </div>
  )
}

function Stat({ label, value, hint, badge }: { label: string; value: string; hint?: string; badge?: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-1">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className="flex flex-wrap items-center gap-2 truncate text-lg font-semibold">
          {value} {badge}
        </span>
        {hint ? <span className="text-xs text-subtle-foreground">{hint}</span> : null}
      </CardContent>
    </Card>
  )
}

function NearestCard({ data }: { data: BirthdayOverview }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center justify-between gap-2">
          Recent and upcoming
          <LiveCaption fallbackText="Live updates paused. Refreshing every 60 s." />
        </CardTitle>
        <CardDescription>The last 7 and next 14 days in the server's timezone (like /birthday show-nearest).</CardDescription>
      </CardHeader>
      <CardContent>
        {data.nearest.length === 0 ? (
          <EmptyState icon={Cake} title="No birthdays around now" description="Nobody's birthday falls in the last 7 or next 14 days." />
        ) : (
          <ul className="flex flex-col divide-y divide-border">
            {data.nearest.map((g) => (
              <li key={g.date} className="flex flex-wrap items-center gap-x-4 gap-y-2 py-2">
                <span className="w-28 shrink-0 text-sm">
                  <span className="font-medium">{new Date(`${g.date}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</span>
                  <span className={g.offset === 0 ? "ml-2 text-success" : "ml-2 text-muted-foreground"}>{relative(g.offset)}</span>
                </span>
                <span className="flex flex-wrap gap-3">
                  {g.user_ids.map((id) => (
                    <ResolvedUser key={id} userId={id} />
                  ))}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function AllBirthdaysCard({ data, onEdit }: { data: BirthdayOverview; onEdit: (e: BirthdayEntry) => void }) {
  const { toast } = useToast()
  const { offline, reason } = useBotOffline()
  const remove = useRemoveBirthday()
  const [q, setQ] = useState("")
  const [confirm, setConfirm] = useState<BirthdayEntry | null>(null)
  const [sort, setSort] = useState<"next" | "date">("next")

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const filtered = data.entries.filter(
      (e) =>
        !needle ||
        e.user_id.includes(needle) ||
        (e.display_name ?? "").toLowerCase().includes(needle) ||
        (e.username ?? "").toLowerCase().includes(needle) ||
        e.date_display.toLowerCase().includes(needle) ||
        (e.user_timezone ?? "").toLowerCase().includes(needle)
    )
    return [...filtered].sort((a, b) => (sort === "next" ? a.days_until - b.days_until : a.month - b.month || a.day - b.day))
  }, [data.entries, q, sort])

  return (
    <Card>
      <CardHeader>
        <CardTitle>All birthdays</CardTitle>
        <CardDescription>{data.entries.length} registered. Editing here works even in add-only mode.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-0 flex-1 basis-60">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search name, ID, date or timezone" aria-label="Search birthdays" className="pl-9" />
          </div>
          <Button variant="secondary" size="sm" onClick={() => setSort((s) => (s === "next" ? "date" : "next"))}>
            Sort: {sort === "next" ? "next up" : "calendar"}
          </Button>
        </div>
        {data.entries.length === 0 ? (
          <EmptyState icon={Cake} title="No birthdays yet" description="Members add theirs with /birthday set date, or add one here." />
        ) : rows.length === 0 ? (
          <EmptyState icon={Search} title="No matches" description="Try another name, ID or date." />
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Member</TableHead>
                  <TableHead>Birthday</TableHead>
                  <TableHead>Timezone</TableHead>
                  <TableHead>Next</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((e) => (
                  <TableRow key={e.user_id}>
                    <TableCell>
                      <ResolvedUser userId={e.user_id} />
                    </TableCell>
                    <TableCell className="tabular-nums">{e.date_display}</TableCell>
                    <TableCell className="text-muted-foreground">{e.user_timezone ?? "Server"}</TableCell>
                    <TableCell>{e.days_until === 0 ? <Badge variant="success">Today</Badge> : `in ${e.days_until} d`}</TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button variant="ghost" size="icon-sm" onClick={() => onEdit(e)} disabled={offline} title={reason ?? "Edit"} aria-label="Edit birthday">
                          <Pencil />
                        </Button>
                        <Button variant="ghost" size="icon-sm" onClick={() => setConfirm(e)} disabled={offline} title={reason ?? "Remove"} aria-label="Remove birthday">
                          <Trash2 />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(open) => !open && setConfirm(null)}
        title={`Remove ${confirm?.display_name ?? "this member"}'s birthday?`}
        description="Same as /override remove-birthday: the date and timezone are deleted and no announcement is made. They can add it again with /birthday set."
        confirmLabel="Remove birthday"
        loading={remove.isPending}
        onConfirm={() => {
          if (!confirm) return
          const name = confirm.display_name ?? "the member"
          remove.mutate(confirm.user_id, {
            onSuccess: () => toast({ title: `Removed ${name}'s birthday`, variant: "success" }),
            onError: (err) => toast({ title: "Could not remove", description: describeApiError(err), variant: "destructive" }),
          })
        }}
      />
    </Card>
  )
}
