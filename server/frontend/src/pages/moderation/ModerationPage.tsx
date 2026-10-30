import { useEffect, useMemo, useState, type FormEvent } from "react"
import { Popover } from "radix-ui"
import { Ban, Clock, LogOut, MessageSquareWarning, Search, ShieldOff, TimerOff, Trash2, UserSearch, Users } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge, CountBadge } from "@/components/ui/badge"
import { DepartedBadge } from "@/components/DepartedBadge"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
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
import { Alert } from "@/components/ui/alert"
import { CopyButton } from "@/components/ui/copy-button"
import { useToast } from "@/components/ui/toast"
import { ResolvedUser } from "@/components/ResolvedUser"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { EmptyState, ErrorState, ListSkeleton, TableSkeleton } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { PolicyLinks } from "@/pages/moderation/PolicyLinks"
import {
  MAX_DELETE_MESSAGE_DAYS,
  MAX_REASON_LENGTH,
  MAX_TIMEOUT_MINUTES,
  moderationBlockReason,
  useAddWarning,
  useBanMember,
  useKickMember,
  useMemberSearch,
  useModHistory,
  useModerationLive,
  useRecentWarnings,
  useRemoveWarning,
  useTimeoutMember,
  useUnbanUser,
  useUntimeoutMember,
  useWarnings,
} from "@/api/moderation"
import { ApiError } from "@/api/client"
import { useResolvedUser } from "@/hooks/useResolve"
import { useAuth } from "@/hooks/useAuth"
import { Link } from "react-router-dom"
import { useBotOffline } from "@/hooks/useBotOffline"
import { useLiveConnected } from "@/live/useLive"
import { describeApiError } from "@/lib/apiError"
import { formatDateTime, formatRelativeTime } from "@/lib/format"
import type { ForbiddenError, MemberSearchResult, ModAction, ModActionType } from "@/types/api"

/**
 * PROTECTED. Look up a member (username/nickname typeahead or raw Discord ID),
 * review warnings and the unified mod-action history, and act on them.
 * Backed by /api/moderation/* (server/routes/moderation.py): warnings and
 * timeouts need "moderate_members", kick "kick_members", ban/unban
 * "ban_members". A "forbidden" 403 on any of the page's queries renders
 * <Forbidden403> inline (inside the shell); a refusal of a single action
 * surfaces as that action's error toast, and a rank/identity refusal
 * (403 hierarchy_blocked / actor_not_member, decided by the bot) is also shown
 * as a banner on the Actions card until another member is picked.
 * useModerationLive() is mounted once, here, once the recent-warnings query
 * has data; the queries poll only while the live socket is down.
 *
 * Interaction model: Warn, Timeout, Kick and Ban open a confirm dialog with
 * the reason prefilled (Ban also picks how many days of messages to delete,
 * 0-7, default 0); Warn and Timeout still offer an Undo toast. Remove timeout
 * and Unban apply at once. Removing a warning is confirmed too. The reason box
 * is cleared whenever a different member is selected.
 */
export default function ModerationPage() {
  const [searchInput, setSearchInput] = useState("")
  const [activeUserId, setActiveUserId] = useState("")
  const [recentFilter, setRecentFilter] = useState("")
  const { toast } = useToast()
  const bot = useBotOffline()

  const live = useLiveConnected()
  // Kick-only / ban-only roles may use this page too (nav: MODERATION_ANY_PERMISSION).
  // Warnings + timeouts need moderate_members, kick kick_members, ban/unban
  // ban_members; sections the viewer lacks are hidden or shown as an inline 403.
  const { permissions } = useAuth()
  const canModerate = Boolean(permissions.moderate_members)
  const canKick = Boolean(permissions.kick_members)
  const canBan = Boolean(permissions.ban_members)
  const canProfile = Boolean(permissions.staff_base)
  const recentWarningsQuery = useRecentWarnings(canModerate)
  useModerationLive({ enabled: canModerate && recentWarningsQuery.data !== undefined })

  const filteredRecent = useMemo(() => {
    const q = recentFilter.trim().toLowerCase()
    const warnings = recentWarningsQuery.data?.warnings ?? []
    if (!q) return warnings
    return warnings.filter(
      (w) =>
        (w.reason?.toLowerCase().includes(q) ?? false) ||
        w.moderator_id.toLowerCase().includes(q) ||
        String(w.user_id).toLowerCase().includes(q)
    )
  }, [recentWarningsQuery.data, recentFilter])
  const warningsQuery = useWarnings(canModerate ? activeUserId : "")
  const historyQuery = useModHistory(activeUserId)
  const addWarning = useAddWarning()
  const removeWarning = useRemoveWarning()
  const kickMember = useKickMember()
  const banMember = useBanMember()
  const unbanUser = useUnbanUser()
  const timeoutMember = useTimeoutMember()
  const untimeoutMember = useUntimeoutMember()

  const [reason, setReason] = useState("")
  const [durationChoice, setDurationChoice] = useState("60")
  const [customMinutes, setCustomMinutes] = useState("30")
  const [deleteDays, setDeleteDays] = useState("0")
  const [confirm, setConfirm] = useState<null | ConfirmKind>(null)
  // Last rank/identity refusal from the bot for the selected member.
  const [blockedReason, setBlockedReason] = useState<string | null>(null)

  // Debounced ~300ms so the typeahead doesn't fire a request per keystroke.
  const [debouncedSearchInput, setDebouncedSearchInput] = useState("")
  const [searchPopoverOpen, setSearchPopoverOpen] = useState(false)
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearchInput(searchInput), 300)
    return () => clearTimeout(timer)
  }, [searchInput])
  const memberSearchQuery = useMemberSearch(debouncedSearchInput)
  const memberResults = memberSearchQuery.data?.members ?? []
  // Enter only acts on results for what is in the box NOW. If the debounced
  // query is behind (or still loading), Enter flushes the debounce and picks
  // the first match once THOSE results arrive.
  const [pendingEnter, setPendingEnter] = useState<string | null>(null)

  function runSearch(userId: string) {
    setSearchInput(userId)
    if (userId !== activeUserId) {
      // A different member: never carry the reason, an open confirm or an
      // old refusal over to them.
      setReason("")
      setConfirm(null)
      setDeleteDays("0")
      setBlockedReason(null)
    }
    setActiveUserId(userId)
    setSearchPopoverOpen(false)
    setPendingEnter(null)
  }

  const resultsAreCurrent =
    debouncedSearchInput === searchInput && memberSearchQuery.data !== undefined && !memberSearchQuery.isPlaceholderData

  useEffect(() => {
    if (pendingEnter === null || !resultsAreCurrent || searchInput.trim() !== pendingEnter) return
    const first = memberSearchQuery.data?.members[0]
    if (first) runSearch(first.id)
    else setPendingEnter(null)
    // runSearch is recreated every render; the inputs above are what matter.
  }, [pendingEnter, resultsAreCurrent, searchInput, memberSearchQuery.data])

  function handleSearch(e: FormEvent) {
    e.preventDefault()
    const trimmed = searchInput.trim()
    if (/^\d+$/.test(trimmed)) {
      runSearch(trimmed)
    } else if (trimmed.length >= 2) {
      if (resultsAreCurrent) {
        if (memberResults.length > 0) runSearch(memberResults[0].id) // Enter picks the first match
      } else {
        setDebouncedSearchInput(searchInput)
        setPendingEnter(trimmed)
      }
    }
  }

  // activeUserId is a Discord snowflake held as a STRING throughout: never
  // wrap it in Number() (IDs past MAX_SAFE_INTEGER would be corrupted).
  const errToast = (title: string) => (error: unknown) => {
    const blocked = moderationBlockReason(error)
    if (blocked) setBlockedReason(blocked)
    toast({ title, description: describeApiError(error), variant: "destructive" })
  }
  const reasonValue = reason.trim() || null

  function handleWarn() {
    if (!activeUserId) return
    const uid = activeUserId
    addWarning.mutate(
      { user_id: uid, reason: reasonValue },
      {
        onSuccess: (res) => {
          setReason("")
          toast({
            title: "Warning added",
            variant: "success",
            action: {
              label: "Undo",
              onClick: () =>
                removeWarning.mutate(
                  { warning_id: res.warning_id, user_id: uid },
                  { onSuccess: () => toast({ title: "Warning removed", variant: "success" }), onError: errToast("Could not undo the warning") }
                ),
            },
          })
        },
        onError: errToast("Failed to add warning"),
      }
    )
  }

  function handleRemoveWarning(warningId: number) {
    if (!activeUserId) return
    removeWarning.mutate(
      { warning_id: warningId, user_id: activeUserId },
      { onSuccess: () => toast({ title: "Warning removed", variant: "success" }), onError: errToast("Failed to remove warning") }
    )
  }

  function handleKick() {
    if (!activeUserId) return
    kickMember.mutate(
      { user_id: activeUserId, reason: reasonValue },
      { onSuccess: () => toast({ title: "User kicked", variant: "success" }), onError: errToast("Failed to kick user") }
    )
  }

  function handleBan() {
    if (!activeUserId) return
    banMember.mutate(
      { user_id: activeUserId, reason: reasonValue, delete_message_days: Number(deleteDays) },
      { onSuccess: () => toast({ title: "User banned", variant: "success" }), onError: errToast("Failed to ban user") }
    )
  }

  function handleUnban() {
    if (!activeUserId) return
    unbanUser.mutate(
      { user_id: activeUserId, reason: reasonValue },
      { onSuccess: () => toast({ title: "User unbanned", variant: "success" }), onError: errToast("Failed to unban user") }
    )
  }

  const timeoutMinutes = durationChoice === "custom" ? Number(customMinutes) : Number(durationChoice)
  const timeoutValid = Number.isInteger(timeoutMinutes) && timeoutMinutes >= 1 && timeoutMinutes <= MAX_TIMEOUT_MINUTES

  function handleTimeout() {
    if (!activeUserId || !timeoutValid) return
    const uid = activeUserId
    timeoutMember.mutate(
      { user_id: uid, duration_minutes: timeoutMinutes, reason: reasonValue },
      {
        onSuccess: () =>
          toast({
            title: "User timed out",
            description: `For ${describeMinutes(timeoutMinutes)}.`,
            variant: "success",
            action: {
              label: "Undo",
              onClick: () =>
                untimeoutMember.mutate(
                  { user_id: uid, reason: null },
                  { onSuccess: () => toast({ title: "Timeout removed", variant: "success" }), onError: errToast("Could not undo the timeout") }
                ),
            },
          }),
        onError: errToast("Failed to time out user"),
      }
    )
  }

  function handleUntimeout() {
    if (!activeUserId) return
    untimeoutMember.mutate(
      { user_id: activeUserId, reason: reasonValue },
      { onSuccess: () => toast({ title: "Timeout removed", variant: "success" }), onError: errToast("Failed to remove timeout") }
    )
  }

  const forbidden = forbiddenFrom(recentWarningsQuery.error) ?? forbiddenFrom(warningsQuery.error) ?? forbiddenFrom(historyQuery.error)

  if (!canModerate && !canKick && !canBan) {
    return <Forbidden403 reason="forbidden" requiredPermission="moderate_members" />
  }
  if (forbidden && canModerate) {
    return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />
  }

  const warnings = warningsQuery.data?.warnings ?? []
  const actions = historyQuery.data?.actions ?? []
  const timedOutUntil = currentTimeout(actions)
  const busy =
    addWarning.isPending || timeoutMember.isPending || untimeoutMember.isPending || kickMember.isPending || banMember.isPending || unbanUser.isPending

  return (
    <PageContainer>
      <PageHeader
        title="Moderation"
        description="Look up a member, review their history, and take action."
        actions={<PolicyLinks />}
      />

      <div className="grid gap-6 lg:grid-cols-[340px_minmax(0,1fr)] lg:grid-rows-[auto_1fr]">
        {/* Find a member */}
        <Card className="order-1 h-fit lg:col-start-1 lg:row-start-1">
          <CardHeader>
            <CardTitle>Find a member</CardTitle>
            <CardDescription>Type a username or nickname, or paste a Discord user ID.</CardDescription>
          </CardHeader>
          <CardContent>
            <Popover.Root open={searchPopoverOpen && memberResults.length > 0} onOpenChange={setSearchPopoverOpen}>
              <Popover.Anchor asChild>
                <form onSubmit={handleSearch} className="flex gap-2">
                  <div className="relative flex-1">
                    <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-subtle-foreground" />
                    <Input
                      type="text"
                      data-page-search
                      aria-label="Find a member"
                      placeholder="Username, nickname or ID"
                      value={searchInput}
                      onChange={(e) => {
                        setSearchInput(e.target.value)
                        setPendingEnter(null)
                        setSearchPopoverOpen(true)
                      }}
                      onFocus={() => setSearchPopoverOpen(true)}
                      className="w-full pl-9"
                    />
                  </div>
                  <Button type="submit" disabled={searchInput.trim().length === 0}>
                    Search
                  </Button>
                </form>
              </Popover.Anchor>
              <Popover.Portal>
                <Popover.Content
                  className="z-50 w-(--radix-popover-trigger-width) overflow-hidden rounded-xl border border-border bg-popover text-popover-foreground shadow-md outline-none"
                  align="start"
                  sideOffset={4}
                  onOpenAutoFocus={(e) => e.preventDefault()}
                >
                  <ul className="max-h-72 overflow-y-auto p-1.5">
                    {memberResults.map((member) => (
                      <MemberSearchRow key={member.id} member={member} onSelect={runSearch} />
                    ))}
                  </ul>
                </Popover.Content>
              </Popover.Portal>
            </Popover.Root>
          </CardContent>
        </Card>

        {/* Recently warned */}
        <Card className="order-3 gap-0 py-0 lg:col-start-1 lg:row-start-2 lg:self-start">
          <CardHeader className="border-b py-4">
            <CardTitle>Recently warned</CardTitle>
            <CardDescription>Latest warnings across the server.{live ? " Updates live." : ""}</CardDescription>
          </CardHeader>
          {recentWarningsQuery.data && recentWarningsQuery.data.warnings.length > 0 ? (
            <div className="border-b border-border px-4 py-3">
              <Input
                type="text"
                aria-label="Filter recent warnings"
                placeholder="Filter by reason, moderator or ID"
                value={recentFilter}
                onChange={(e) => setRecentFilter(e.target.value)}
                className="h-[30px] w-full text-sm"
              />
            </div>
          ) : null}
          {!canModerate ? (
            <p className="px-5 py-6 text-center text-sm text-muted-foreground">Warnings need the moderate_members permission.</p>
          ) : recentWarningsQuery.isPending ? (
            <ListSkeleton rows={4} />
          ) : null}
          {recentWarningsQuery.isError ? (
            <ErrorState
              title="Could not load recent warnings"
              message={describeApiError(recentWarningsQuery.error)}
              onRetry={() => void recentWarningsQuery.refetch()}
              retrying={recentWarningsQuery.isRefetching}
              className="py-6"
            />
          ) : null}
          {recentWarningsQuery.data ? (
            recentWarningsQuery.data.warnings.length === 0 ? (
              <EmptyState icon={MessageSquareWarning} title="No warnings yet" description="Search a member above to look someone up directly." className="py-8" />
            ) : filteredRecent.length === 0 ? (
              <p className="px-5 py-6 text-center text-sm text-muted-foreground">No recent warnings match "{recentFilter.trim()}".</p>
            ) : (
              <ul className="flex max-h-[520px] flex-col divide-y divide-border overflow-y-auto">
                {filteredRecent.map((w) => (
                  <li key={w.id}>
                    <button
                      type="button"
                      onClick={() => runSearch(String(w.user_id))}
                      className="flex min-h-12 w-full flex-col gap-0.5 px-5 py-3 text-left transition-colors duration-[120ms] hover:bg-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                    >
                      <span className="text-base font-medium">
                        <ResolvedUser userId={String(w.user_id)} />
                      </span>
                      <span className="truncate text-sm text-muted-foreground">{w.reason ?? "No reason given"}</span>
                      <span className="text-xs text-subtle-foreground">{formatRelativeTime(w.created_at)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )
          ) : null}
        </Card>

        {/* Member panel */}
        <div className="order-2 flex min-w-0 flex-col gap-4 lg:col-start-2 lg:row-span-2 lg:row-start-1">
          {!activeUserId ? (
            <Card>
              <EmptyState
                icon={UserSearch}
                title="Search for a member to see their history"
                description="Find someone on the left, or pick a recently warned member."
              />
            </Card>
          ) : (
            <>
              <MemberHeader
                userId={activeUserId}
                warningCount={warningsQuery.data ? warnings.length : null}
                timedOutUntil={timedOutUntil}
                profileLink={canProfile}
              />

              <Card>
                <CardHeader>
                  <CardTitle>Actions</CardTitle>
                  <CardDescription>The reason below is used for whichever action you take.</CardDescription>
                </CardHeader>
                <CardContent className="flex flex-col gap-4">
                  {blockedReason ? (
                    <Alert variant="danger" title="You can't act on this member">
                      {blockedReason}
                    </Alert>
                  ) : null}
                  <Input
                    type="text"
                    aria-label="Reason"
                    placeholder="Reason (optional)"
                    value={reason}
                    maxLength={MAX_REASON_LENGTH}
                    onChange={(e) => setReason(e.target.value)}
                  />
                  {bot.offline ? <p className="text-sm text-danger">{bot.reason}</p> : null}
                  {canModerate ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      className="max-md:h-11"
                      onClick={() => setConfirm("warn")}
                      loading={addWarning.isPending}
                      disabled={busy || bot.offline}
                      title={bot.reason}
                    >
                      <MessageSquareWarning />
                      {addWarning.isPending ? "Warning" : "Warn"}
                    </Button>

                    <div className="flex flex-wrap items-center gap-2">
                      <div className="w-36">
                        <Select aria-label="Timeout duration" value={durationChoice} onChange={(e) => setDurationChoice(e.target.value)}>
                          <option value="5">5 minutes</option>
                          <option value="10">10 minutes</option>
                          <option value="60">1 hour</option>
                          <option value="1440">1 day</option>
                          <option value="10080">1 week</option>
                          <option value="custom">Custom…</option>
                        </Select>
                      </div>
                      {durationChoice === "custom" ? (
                        <Input
                          type="number"
                          min={1}
                          max={MAX_TIMEOUT_MINUTES}
                          step={1}
                          title={`1 to ${MAX_TIMEOUT_MINUTES} minutes (28 days)`}
                          aria-label="Custom timeout in minutes"
                          aria-invalid={!timeoutValid}
                          value={customMinutes}
                          onChange={(e) => setCustomMinutes(e.target.value)}
                          className="w-24"
                        />
                      ) : null}
                      <Button
                        type="button"
                        variant="secondary"
                        className="max-md:h-11"
                        onClick={() => setConfirm("timeout")}
                        loading={timeoutMember.isPending}
                        disabled={busy || bot.offline || !timeoutValid}
                        title={bot.reason}
                      >
                        <Clock />
                        {timeoutMember.isPending ? "Timing out" : "Timeout"}
                      </Button>
                    </div>

                    <Button
                      type="button"
                      variant="secondary"
                      className="max-md:h-11"
                      onClick={handleUntimeout}
                      loading={untimeoutMember.isPending}
                      disabled={busy || bot.offline}
                      title={bot.reason}
                    >
                      <TimerOff />
                      {untimeoutMember.isPending ? "Removing" : "Remove timeout"}
                    </Button>
                  </div>
                  ) : null}

                  {canKick || canBan ? (
                  <div className={canModerate ? "flex flex-wrap items-center gap-2 border-t border-border pt-4" : "flex flex-wrap items-center gap-2"}>
                    {canKick ? (
                    <Button
                      type="button"
                      variant="destructive-outline"
                      className="max-md:h-11"
                      onClick={() => setConfirm("kick")}
                      disabled={busy || bot.offline}
                      title={bot.reason}
                    >
                      <LogOut />
                      Kick
                    </Button>
                    ) : null}
                    {canBan ? (
                    <>
                    <Button
                      type="button"
                      variant="destructive"
                      className="max-md:h-11"
                      onClick={() => setConfirm("ban")}
                      disabled={busy || bot.offline}
                      title={bot.reason}
                    >
                      <Ban />
                      Ban
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      className="max-md:h-11"
                      onClick={handleUnban}
                      loading={unbanUser.isPending}
                      disabled={busy || bot.offline}
                      title={bot.reason}
                    >
                      <ShieldOff />
                      {unbanUser.isPending ? "Unbanning" : "Unban"}
                    </Button>
                    </>
                    ) : null}
                  </div>
                  ) : null}
                </CardContent>
              </Card>

              <AlertDialog open={confirm !== null} onOpenChange={(o) => !o && setConfirm(null)}>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>{confirm ? CONFIRM_COPY[confirm].title : ""}</AlertDialogTitle>
                    <AlertDialogDescription>
                      {confirm === "timeout"
                        ? `They can't send messages or join voice for ${describeMinutes(timeoutMinutes)}. The action is logged with your name.`
                        : confirm
                          ? CONFIRM_COPY[confirm].description
                          : ""}
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <div className="flex flex-col gap-3 py-1">
                    <div className="rounded-lg bg-muted px-3 py-2">
                      <ResolvedUser userId={activeUserId} />
                    </div>
                    <Input
                      type="text"
                      aria-label="Reason"
                      placeholder="Reason (optional)"
                      value={reason}
                      maxLength={MAX_REASON_LENGTH}
                      onChange={(e) => setReason(e.target.value)}
                    />
                    {confirm === "ban" ? (
                      <label className="flex flex-col gap-1.5 text-sm text-muted-foreground">
                        Delete their recent messages
                        <Select aria-label="Delete message history" value={deleteDays} onChange={(e) => setDeleteDays(e.target.value)}>
                          {Array.from({ length: MAX_DELETE_MESSAGE_DAYS + 1 }, (_, d) => (
                            <option key={d} value={String(d)}>
                              {d === 0 ? "Don't delete any" : `Previous ${d} day${d === 1 ? "" : "s"}`}
                            </option>
                          ))}
                        </Select>
                      </label>
                    ) : null}
                  </div>
                  <AlertDialogFooter>
                    <AlertDialogCancel autoFocus>Cancel</AlertDialogCancel>
                    <Button
                      variant={confirm === "warn" || confirm === "timeout" ? "default" : "destructive"}
                      onClick={() => {
                        const which = confirm
                        setConfirm(null)
                        if (which === "ban") handleBan()
                        else if (which === "kick") handleKick()
                        else if (which === "warn") handleWarn()
                        else if (which === "timeout") handleTimeout()
                      }}
                    >
                      {confirm === "ban" ? <Ban /> : confirm === "kick" ? <LogOut /> : confirm === "timeout" ? <Clock /> : <MessageSquareWarning />}
                      {confirm ? CONFIRM_COPY[confirm].button : ""}
                    </Button>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>

              <Card className="gap-0 py-0">
                <Tabs defaultValue="warnings" className="gap-0">
                  <TabsList className="px-3">
                    <TabsTrigger value="warnings">
                      Warnings
                      {warningsQuery.data ? <CountBadge count={warnings.length} /> : null}
                    </TabsTrigger>
                    <TabsTrigger value="actions">
                      Mod actions
                      {historyQuery.data ? <CountBadge count={actions.length} /> : null}
                    </TabsTrigger>
                  </TabsList>

                  <TabsContent value="warnings">
                    {!canModerate ? (
                      <EmptyState icon={MessageSquareWarning} title="You can't see warnings" description="They need the moderate_members permission." />
                    ) : warningsQuery.isPending ? (
                      <TableSkeleton rows={3} cols={4} />
                    ) : null}
                    {warningsQuery.isError && !forbidden ? (
                      <ErrorState
                        title="Could not load warnings"
                        message={describeApiError(warningsQuery.error)}
                        onRetry={() => void warningsQuery.refetch()}
                        retrying={warningsQuery.isRefetching}
                        showStatusLink
                      />
                    ) : null}
                    {warningsQuery.data ? (
                      warnings.length === 0 ? (
                        <EmptyState icon={MessageSquareWarning} title="No warnings" description="This member has a clean record." />
                      ) : (
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>When</TableHead>
                              <TableHead>Reason</TableHead>
                              <TableHead>Moderator</TableHead>
                              <TableHead className="text-right">
                                <span className="sr-only">Actions</span>
                              </TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {warnings.map((w) => (
                              <TableRow key={w.id}>
                                <TableCell className="text-muted-foreground">{formatDateTime(w.created_at)}</TableCell>
                                <TableCell className="max-w-[320px] truncate whitespace-normal">{w.reason ?? "No reason given"}</TableCell>
                                <TableCell>
                                  <ResolvedUser userId={w.moderator_id} link />
                                </TableCell>
                                <TableCell className="text-right">
                                  <AlertDialog>
                                    <AlertDialogTrigger asChild>
                                      <Button type="button" variant="ghost" size="sm" disabled={removeWarning.isPending} aria-label={`Remove warning ${w.id}`}>
                                        <Trash2 /> Remove
                                      </Button>
                                    </AlertDialogTrigger>
                                    <AlertDialogContent>
                                      <AlertDialogHeader>
                                        <AlertDialogTitle>Remove warning #{w.id}?</AlertDialogTitle>
                                        <AlertDialogDescription>
                                          This erases the warning from their record and cannot be undone. The removal is logged.
                                        </AlertDialogDescription>
                                      </AlertDialogHeader>
                                      <AlertDialogFooter>
                                        <AlertDialogCancel autoFocus>Cancel</AlertDialogCancel>
                                        <Button variant="destructive" onClick={() => handleRemoveWarning(w.id)}>
                                          <Trash2 /> Remove warning
                                        </Button>
                                      </AlertDialogFooter>
                                    </AlertDialogContent>
                                  </AlertDialog>
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      )
                    ) : null}
                  </TabsContent>

                  <TabsContent value="actions">
                    {historyQuery.isPending ? <TableSkeleton rows={3} cols={4} /> : null}
                    {historyQuery.isError ? (
                      <ErrorState
                        title="Could not load history"
                        message={describeApiError(historyQuery.error)}
                        onRetry={() => void historyQuery.refetch()}
                        retrying={historyQuery.isRefetching}
                        showStatusLink
                      />
                    ) : null}
                    {historyQuery.data ? (
                      actions.length === 0 ? (
                        <EmptyState icon={Users} title="No mod actions yet" description="Kicks, bans, timeouts and warnings will be listed here." />
                      ) : (
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>When</TableHead>
                              <TableHead>Action</TableHead>
                              <TableHead>Reason</TableHead>
                              <TableHead>Moderator</TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {actions.map((a) => (
                              <TableRow key={a.id}>
                                <TableCell className="text-muted-foreground">{formatDateTime(a.created_at)}</TableCell>
                                <TableCell>
                                  <Badge variant={actionVariant(a.action_type)}>{actionLabel(a.action_type)}</Badge>
                                  {a.duration_minutes != null ? (
                                    <span className="ml-2 text-xs text-muted-foreground">{describeMinutes(a.duration_minutes)}</span>
                                  ) : null}
                                </TableCell>
                                <TableCell className="max-w-[280px] truncate whitespace-normal">{a.reason ?? "No reason given"}</TableCell>
                                <TableCell>
                                  <ResolvedUser userId={a.moderator_id} link />
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      )
                    ) : null}
                  </TabsContent>
                </Tabs>
              </Card>
            </>
          )}
        </div>
      </div>
    </PageContainer>
  )
}

/** Member header: avatar, names, copyable ID, warning count and (derived) timeout state. */
function MemberHeader({
  userId,
  warningCount,
  timedOutUntil,
  profileLink,
}: {
  userId: string
  warningCount: number | null
  timedOutUntil: Date | null
  /** Show "Open profile" (the profile route needs staff_base). */
  profileLink: boolean
}) {
  const { data, isLoading } = useResolvedUser(userId)
  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-4">
        {isLoading ? (
          <Skeleton className="size-14 rounded-full" />
        ) : (
          <Avatar className="size-14">
            <AvatarImage src={data?.avatar_url} alt="" />
            <AvatarFallback className="text-lg">{(data?.display_name ?? "?").slice(0, 2).toUpperCase()}</AvatarFallback>
          </Avatar>
        )}
        <div className="min-w-0 flex-1 basis-56">
          <h2 className="truncate text-lg font-semibold text-foreground">{data?.display_name ?? "Unknown member"}</h2>
          <p className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
            <span className="truncate">{data ? `@${data.username}` : data === null ? "This user is not in the server" : ""}</span>
            {data?.departed ? <DepartedBadge user={data} /> : null}
          </p>
          <p className="mt-1 flex items-center gap-1 font-mono text-xs text-subtle-foreground">
            {userId}
            <CopyButton value={userId} />
          </p>
        </div>
        <CardAction className="flex flex-wrap items-center gap-2">
          {profileLink ? (
            <Button variant="secondary" size="sm" asChild>
              <Link to={`/dashboard/members/${userId}`}>Open profile</Link>
            </Button>
          ) : null}
          {timedOutUntil ? (
            <Badge variant="warning">
              <Clock /> Timed out until {timedOutUntil.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}
            </Badge>
          ) : null}
          {warningCount !== null ? (
            <Badge variant={warningCount > 0 ? "warning" : "default"}>
              {warningCount} {warningCount === 1 ? "warning" : "warnings"}
            </Badge>
          ) : null}
        </CardAction>
      </CardContent>
    </Card>
  )
}

/** One row in the "Find a member" typeahead dropdown. */
function MemberSearchRow({ member, onSelect }: { member: MemberSearchResult; onSelect: (userId: string) => void }) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(member.id)}
        className="flex min-h-10 w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-base text-foreground outline-none transition-colors hover:bg-accent focus-visible:bg-accent"
      >
        <Avatar size="sm">
          <AvatarImage src={member.avatar_url} alt="" />
          <AvatarFallback>{member.display_name.slice(0, 1).toUpperCase()}</AvatarFallback>
        </Avatar>
        <div className="flex min-w-0 flex-col">
          <span className="truncate font-medium text-foreground">{member.display_name}</span>
          <span className="truncate text-xs text-muted-foreground">@{member.username}</span>
        </div>
      </button>
    </li>
  )
}

const ACTION_LABELS: Record<ModAction["action_type"], string> = {
  kick: "Kick",
  ban: "Ban",
  unban: "Unban",
  timeout: "Timeout",
  untimeout: "Untimeout",
  warn: "Warn",
  warning_removed: "Warning removed",
}

function actionLabel(actionType: ModActionType): string {
  return ACTION_LABELS[actionType] ?? actionType
}

function actionVariant(t: ModActionType): "destructive" | "warning" | "success" | "default" {
  if (t === "ban" || t === "kick") return "destructive"
  if (t === "timeout" || t === "warn") return "warning"
  if (t === "unban" || t === "untimeout") return "success"
  return "default"
}

function describeMinutes(m: number): string {
  if (m % 10080 === 0) return `${m / 10080} week${m === 10080 ? "" : "s"}`
  if (m % 1440 === 0) return `${m / 1440} day${m === 1440 ? "" : "s"}`
  if (m % 60 === 0) return `${m / 60} hour${m === 60 ? "" : "s"}`
  return `${m} minute${m === 1 ? "" : "s"}`
}

/** Derived from the history already loaded: the latest timeout that is still running and not followed by an untimeout. */
function currentTimeout(actions: ModAction[]): Date | null {
  const sorted = [...actions].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
  for (const a of sorted) {
    if (a.action_type === "untimeout") return null
    if (a.action_type === "timeout" && a.expires_at) {
      const d = new Date(a.expires_at)
      return d.getTime() > Date.now() ? d : null
    }
  }
  return null
}

function isForbiddenError(body: unknown): body is ForbiddenError {
  return (
    typeof body === "object" &&
    body !== null &&
    "error" in body &&
    (body as { error: unknown }).error === "forbidden"
  )
}

/** The dashboard's own permission 403 ("forbidden"), never a bot refusal. */
function forbiddenFrom(error: unknown): ForbiddenError | null {
  return error instanceof ApiError && error.status === 403 && isForbiddenError(error.body) ? error.body : null
}

type ConfirmKind = "warn" | "timeout" | "kick" | "ban"

const CONFIRM_COPY: Record<ConfirmKind, { title: string; description: string; button: string }> = {
  warn: {
    title: "Warn this member?",
    description: "The warning is added to their record. The action is logged with your name.",
    button: "Warn member",
  },
  timeout: { title: "Time out this member?", description: "", button: "Time out member" },
  kick: {
    title: "Kick this member?",
    description: "They can rejoin with a new invite. The action is logged with your name.",
    button: "Kick member",
  },
  ban: {
    title: "Ban this member?",
    description: "This removes them from the server until they are unbanned. The action is logged with your name.",
    button: "Ban member",
  },
}
