import { useEffect, useState } from "react"
import { Link, useParams } from "react-router-dom"
import {
  ArrowLeft,
  Ban,
  BadgeCheck,
  Clock,
  Eraser,
  LogOut,
  Mail,
  MessageSquareWarning,
  ShieldOff,
  Target,
  TimerOff,
  Trash2,
  UserX,
  Users,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge, CountBadge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Alert } from "@/components/ui/alert"
import { CopyButton } from "@/components/ui/copy-button"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { LiveCaption } from "@/components/patterns/LivePill"
import { EmptyState, ErrorState } from "@/components/patterns/states"
import { ResolvedUser } from "@/components/ResolvedUser"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { ApiError } from "@/api/client"
import {
  MAX_DELETE_MESSAGE_DAYS,
  MAX_REASON_LENGTH,
  MAX_TIMEOUT_MINUTES,
  moderationBlockReason,
  useAddWarning,
  useBanMember,
  useKickMember,
  useRemoveWarning,
  useTimeoutMember,
  useUnbanUser,
  useUntimeoutMember,
} from "@/api/moderation"
import {
  useInvalidateProfile,
  useMemberProfile,
  useMessageLogExport,
  useVerifyChannels,
  useVerifyMember,
  type MemberProfile,
} from "@/api/members"
import { useCachedUser, useForgetCachedUser } from "@/api/userCache"
import { useAuth } from "@/hooks/useAuth"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import { formatDateTime, formatRelativeTime } from "@/lib/format"
import type { ModActionType, ResolvedUser as ResolvedUserType } from "@/types/api"

type ConfirmKind = "warn" | "timeout" | "kick" | "ban" | "forget" | null

const STATUS_LABEL: Record<string, { label: string; variant: "success" | "destructive" | "warning" | "default" }> = {
  member: { label: "In the server", variant: "success" },
  banned: { label: "Banned", variant: "destructive" },
  kicked: { label: "Kicked", variant: "warning" },
  left: { label: "Left", variant: "default" },
  unbanned: { label: "Unbanned, not back", variant: "default" },
  not_member: { label: "Not in the server", variant: "default" },
}

/**
 * Member profile (`/dashboard/members/:userId`, a richer `/userinfo`): account
 * and membership facts, roles, timeout state, warnings, mod-action history,
 * honeypot record, plus the actions the viewer may take. GET
 * /api/members/{id}/profile is staff_base; warnings/honeypot need
 * moderate_members and history any of moderate/kick/ban (withheld sections
 * come back in `restricted` and show an inline 403). Each action button is
 * shown only with its own permission (warn/timeout moderate_members, kick
 * kick_members, ban/unban ban_members, verify verify_members, DM export and
 * Forget staff only) and the bot re-checks permission and rank on every
 * action. Works for departed users: member-only actions are hidden, Unban is
 * offered when banned, and the last known name/avatar comes from the user
 * cache (GET /api/user-cache/{id}).
 */
export default function MemberProfilePage() {
  const { userId = "" } = useParams()
  const { permissions, isStaff } = useAuth()
  const can = (p: string) => Boolean(permissions[p])
  const validId = /^\d{15,21}$/.test(userId)
  const profileQuery = useMemberProfile(userId, { liveTopicAllowed: can("moderate_members") })
  const cached = useCachedUser(validId ? userId : null)

  if (!validId) {
    return (
      <PageContainer>
        <PageHeader title="Member" />
        <Card>
          <EmptyState icon={UserX} title="That is not a Discord user ID" description="Open a member from the Members page." />
        </Card>
      </PageContainer>
    )
  }

  const err = profileQuery.error
  if (err instanceof ApiError && err.status === 403 && (err.body as { error?: string } | null)?.error === "forbidden") {
    return <Forbidden403 reason="forbidden" requiredPermission="staff_base" />
  }

  const profile = profileQuery.data
  const cachedUser = cached.data?.user
  const name =
    profile?.member?.nick ??
    profile?.user.display_name ??
    cachedUser?.display_name ??
    profile?.user.username ??
    "Unknown user"

  return (
    <PageContainer>
      <PageHeader
        title={profileQuery.isPending ? "Member" : name}
        description="Account, membership and moderation record."
        actions={
          <Button variant="secondary" asChild>
            <Link to="/dashboard/members">
              <ArrowLeft /> Members
            </Link>
          </Button>
        }
      />
      {profileQuery.isPending ? (
        <div className="flex flex-col gap-4">
          <Skeleton className="h-28 w-full rounded-xl" />
          <Skeleton className="h-40 w-full rounded-xl" />
        </div>
      ) : profileQuery.isError ? (
        <Card>
          <ErrorState
            title="Could not load this member"
            message={describeApiError(profileQuery.error)}
            onRetry={() => void profileQuery.refetch()}
            retrying={profileQuery.isRefetching}
            showStatusLink
          />
        </Card>
      ) : profile ? (
        <ProfileBody profile={profile} cachedUser={cachedUser} can={can} isStaff={isStaff} name={name} />
      ) : null}
    </PageContainer>
  )
}

function ProfileBody({
  profile,
  cachedUser,
  can,
  isStaff,
  name,
}: {
  profile: MemberProfile
  cachedUser: ResolvedUserType | undefined
  can: (p: string) => boolean
  isStaff: boolean
  name: string
}) {
  const userId = profile.user_id
  const { toast } = useToast()
  const bot = useBotOffline()
  const invalidate = useInvalidateProfile()
  const addWarning = useAddWarning()
  const removeWarning = useRemoveWarning()
  const kick = useKickMember()
  const ban = useBanMember()
  const unban = useUnbanUser()
  const timeout = useTimeoutMember()
  const untimeout = useUntimeoutMember()
  const verify = useVerifyMember()
  const exportLog = useMessageLogExport()
  const forget = useForgetCachedUser()
  const verifyChannels = useVerifyChannels(can("verify_members") && profile.in_guild && !profile.member?.verified)

  const [reason, setReason] = useState("")
  const [duration, setDuration] = useState("60")
  const [deleteDays, setDeleteDays] = useState("0")
  const [verifyChannel, setVerifyChannel] = useState("")
  const [exportWindow, setExportWindow] = useState("24h")
  const [confirm, setConfirm] = useState<ConfirmKind>(null)
  const [blocked, setBlocked] = useState<string | null>(null)

  useEffect(() => {
    const first = verifyChannels.data?.channels[0]?.id
    if (first && !verifyChannel) setVerifyChannel(first)
  }, [verifyChannels.data, verifyChannel])

  const reasonValue = reason.trim() || null
  const busy =
    addWarning.isPending || kick.isPending || ban.isPending || unban.isPending || timeout.isPending || untimeout.isPending || verify.isPending

  const done = (title: string) => () => {
    setReason("")
    invalidate(userId)
    toast({ title, variant: "success" })
  }
  const failed = (title: string) => (error: unknown) => {
    const b = moderationBlockReason(error)
    if (b) setBlocked(b)
    toast({ title, description: describeApiError(error), variant: "destructive" })
  }

  const member = profile.member
  const inGuild = profile.in_guild
  const banned = profile.ban.banned
  const status = !inGuild && profile.status === "not_member" && cachedUser?.status ? cachedUser.status : profile.status
  const statusInfo = STATUS_LABEL[status] ?? STATUS_LABEL.not_member
  const avatar = profile.user.avatar_url ?? cachedUser?.avatar_url ?? undefined
  const username = profile.user.username ?? cachedUser?.username ?? null
  const departedAt = !inGuild ? cachedUser?.departed_at ?? profile.last_departure?.at ?? null : null
  const departedBy = !inGuild ? cachedUser?.departed_by_id ?? null : null
  const timeoutMinutes = Number(duration)
  const advisory = blocked ?? (profile.actor_block_reason && inGuild ? profile.actor_block_reason : null)

  const memberActions = inGuild && (can("moderate_members") || can("kick_members") || can("ban_members") || can("verify_members"))
  const anyAction = memberActions || can("moderate_members") || can("ban_members") || isStaff

  return (
    <div className="flex flex-col gap-4">
      {/* Header */}
      <Card>
        <CardContent className="flex flex-wrap items-center gap-4">
          <Avatar className={inGuild ? "size-16" : "size-16 opacity-70 grayscale"}>
            <AvatarImage src={avatar} alt="" />
            <AvatarFallback className="text-lg">{name.slice(0, 2).toUpperCase()}</AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1 basis-56">
            <h2 className="truncate text-lg font-semibold text-foreground">{name}</h2>
            <p className="truncate text-sm text-muted-foreground">
              {username ? `@${username}` : "Discord could not tell us who this is"}
              {!inGuild && cachedUser ? " (last known)" : ""}
            </p>
            <p className="mt-1 flex items-center gap-1 font-mono text-xs text-subtle-foreground">
              {userId}
              <CopyButton value={userId} />
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={statusInfo.variant}>{statusInfo.label}</Badge>
            {profile.user.bot ? <Badge variant="info">Bot</Badge> : null}
            {member?.verified ? (
              <Badge variant="success">
                <BadgeCheck /> Verified
              </Badge>
            ) : member?.unverified ? (
              <Badge variant="warning">Unverified</Badge>
            ) : null}
            {member?.timed_out_until ? (
              <Badge variant="warning">
                <Clock /> Timed out until {formatDateTime(member.timed_out_until)}
              </Badge>
            ) : null}
            {member?.is_guild_owner ? <Badge variant="accent">Server owner</Badge> : null}
          </div>
        </CardContent>
      </Card>

      {/* Facts */}
      <Card>
        <CardHeader>
          <CardTitle>About</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
            <Fact label="Account created" value={formatDateTime(profile.user.created_at)} />
            {member ? (
              <>
                <Fact label="Joined the server" value={member.joined_at ? formatDateTime(member.joined_at) : "Unknown"} />
                {member.premium_since ? <Fact label="Boosting since" value={formatDateTime(member.premium_since)} /> : null}
                {member.pending ? <Fact label="Membership screening" value="Not completed yet" /> : null}
              </>
            ) : (
              <>
                {cachedUser?.joined_at ? <Fact label="Last joined" value={formatDateTime(cachedUser.joined_at)} /> : null}
                <Fact label="Left the server" value={departedAt ? formatDateTime(departedAt) : "Unknown"} />
                {departedBy ? (
                  <div>
                    <dt className="text-muted-foreground">Removed by</dt>
                    <dd className="mt-0.5">
                      <ResolvedUser userId={departedBy} link />
                    </dd>
                  </div>
                ) : null}
                {cachedUser?.last_seen ? <Fact label="Last seen" value={formatRelativeTime(cachedUser.last_seen)} /> : null}
                <Fact
                  label="Ban"
                  value={banned === true ? `Banned${profile.ban.reason ? `: ${profile.ban.reason}` : ""}` : banned === false ? "Not banned" : "Unknown (the bot can't read bans)"}
                />
              </>
            )}
          </dl>
          {member ? (
            <div className="mt-4 border-t border-border pt-4">
              <p className="mb-2 text-sm text-muted-foreground">Roles ({member.roles.length})</p>
              {member.roles.length === 0 ? (
                <p className="text-sm text-subtle-foreground">No roles.</p>
              ) : (
                <ul className="flex flex-wrap gap-1.5">
                  {member.roles.map((r) => (
                    <li key={r.id}>
                      <Badge variant="outline">
                        <span aria-hidden className="size-2 rounded-full bg-muted-foreground" style={r.color ? { backgroundColor: r.color } : undefined} />
                        {r.name}
                      </Badge>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : null}
        </CardContent>
      </Card>

      {/* Actions */}
      {anyAction ? (
        <Card>
          <CardHeader>
            <CardTitle>Actions</CardTitle>
            <CardDescription>
              {inGuild
                ? "The reason is used for whichever action you take. Every action is logged with your name."
                : "This user is not in the server, so only actions that work on departed users are shown."}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            {advisory ? (
              <Alert variant="danger" title="You can't act on this member">
                {advisory}
              </Alert>
            ) : null}
            {bot.offline ? <p className="text-sm text-danger">{bot.reason}</p> : null}
            {can("moderate_members") || can("kick_members") || can("ban_members") ? (
              <Input
                aria-label="Reason"
                placeholder="Reason (optional)"
                value={reason}
                maxLength={MAX_REASON_LENGTH}
                onChange={(e) => setReason(e.target.value)}
              />
            ) : null}
            <div className="flex flex-wrap items-center gap-2">
              {can("moderate_members") ? (
                <Button onClick={() => setConfirm("warn")} disabled={busy || bot.offline} title={bot.reason} loading={addWarning.isPending}>
                  <MessageSquareWarning /> Warn
                </Button>
              ) : null}
              {inGuild && can("moderate_members") ? (
                <>
                  <div className="w-36">
                    <Select aria-label="Timeout duration" value={duration} onChange={(e) => setDuration(e.target.value)}>
                      <option value="5">5 minutes</option>
                      <option value="60">1 hour</option>
                      <option value="1440">1 day</option>
                      <option value="10080">1 week</option>
                      <option value={String(MAX_TIMEOUT_MINUTES)}>28 days</option>
                    </Select>
                  </div>
                  <Button variant="secondary" onClick={() => setConfirm("timeout")} disabled={busy || bot.offline} title={bot.reason} loading={timeout.isPending}>
                    <Clock /> Timeout
                  </Button>
                  {member?.timed_out_until ? (
                    <Button
                      variant="secondary"
                      disabled={busy || bot.offline}
                      title={bot.reason}
                      loading={untimeout.isPending}
                      onClick={() => untimeout.mutate({ user_id: userId, reason: reasonValue }, { onSuccess: done("Timeout removed"), onError: failed("Could not remove the timeout") })}
                    >
                      <TimerOff /> Remove timeout
                    </Button>
                  ) : null}
                </>
              ) : null}
            </div>
            {(inGuild && can("kick_members")) || can("ban_members") ? (
              <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
                {inGuild && can("kick_members") ? (
                  <Button variant="destructive-outline" onClick={() => setConfirm("kick")} disabled={busy || bot.offline} title={bot.reason}>
                    <LogOut /> Kick
                  </Button>
                ) : null}
                {can("ban_members") && banned !== true ? (
                  <Button variant="destructive" onClick={() => setConfirm("ban")} disabled={busy || bot.offline} title={bot.reason}>
                    <Ban /> Ban
                  </Button>
                ) : null}
                {can("ban_members") && !inGuild && banned !== false ? (
                  <Button
                    variant="secondary"
                    disabled={busy || bot.offline}
                    title={bot.reason}
                    loading={unban.isPending}
                    onClick={() => unban.mutate({ user_id: userId, reason: reasonValue }, { onSuccess: done("User unbanned"), onError: failed("Could not unban") })}
                  >
                    <ShieldOff /> Unban
                  </Button>
                ) : null}
              </div>
            ) : null}
            {inGuild && can("verify_members") && !member?.verified ? (
              <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
                <div className="w-56">
                  <Select
                    aria-label="Channel for the verify message"
                    value={verifyChannel}
                    onChange={(e) => setVerifyChannel(e.target.value)}
                    disabled={!verifyChannels.data || verifyChannels.data.channels.length === 0}
                  >
                    {verifyChannels.data?.channels.length === 0 ? <option value="">No Unverified channels</option> : null}
                    {verifyChannels.data?.channels.map((c) => (
                      <option key={c.id} value={c.id}>
                        #{c.name}
                      </option>
                    ))}
                  </Select>
                </div>
                <Button
                  variant="secondary"
                  disabled={busy || bot.offline || !verifyChannel}
                  title={bot.reason ?? "Same as !verify: swaps the Unverified role for Verified and posts the verify message in the channel you pick."}
                  loading={verify.isPending}
                  onClick={() =>
                    verify.mutate(
                      { user_id: userId, channel_id: verifyChannel },
                      {
                        onSuccess: (r) => toast({ title: "Member verified", description: r.message_status, variant: "success" }),
                        onError: failed("Could not verify"),
                      }
                    )
                  }
                >
                  <BadgeCheck /> Verify
                </Button>
                {verifyChannels.isError ? <span className="text-sm text-danger">{describeApiError(verifyChannels.error)}</span> : null}
              </div>
            ) : null}
            {isStaff ? (
              <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
                <div className="w-32">
                  <Select aria-label="Message log window" value={exportWindow} onChange={(e) => setExportWindow(e.target.value)}>
                    <option value="1h">Last hour</option>
                    <option value="6h">Last 6 hours</option>
                    <option value="24h">Last 24 hours</option>
                    <option value="3d">Last 3 days</option>
                    <option value="7d">Last 7 days</option>
                  </Select>
                </div>
                <Button
                  variant="secondary"
                  disabled={bot.offline || exportLog.isPending}
                  title={bot.reason ?? "Same as !msglog: the bot DMs the export to you. It is never downloaded here."}
                  loading={exportLog.isPending}
                  onClick={() =>
                    exportLog.mutate(
                      { kind: "user", user_id: userId, window: exportWindow },
                      {
                        onSuccess: (r) => toast({ title: "Sent to your DMs", description: `${r.count} message(s).`, variant: "success" }),
                        onError: (e) => toast({ title: "Could not send the export", description: describeApiError(e), variant: "destructive" }),
                      }
                    )
                  }
                >
                  <Mail /> DM me their messages
                </Button>
                {!inGuild && cachedUser ? (
                  <Button variant="ghost" onClick={() => setConfirm("forget")} disabled={forget.isPending} loading={forget.isPending}>
                    <Eraser /> Forget cached profile
                  </Button>
                ) : null}
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={
          confirm === "warn"
            ? "Warn this member?"
            : confirm === "timeout"
              ? "Time out this member?"
              : confirm === "kick"
                ? "Kick this member?"
                : confirm === "ban"
                  ? "Ban this user?"
                  : "Forget this cached profile?"
        }
        description={
          confirm === "forget"
            ? "Their last known name, avatar and departure details are removed from the bot's user cache. Warnings and moderation history are kept."
            : confirm === "ban"
              ? "They are removed from the server (if present) until unbanned. Logged with your name."
              : confirm === "kick"
                ? "They can rejoin with a new invite. Logged with your name."
                : confirm === "timeout"
                  ? `They can't send messages or join voice for ${timeoutMinutes >= 1440 ? `${timeoutMinutes / 1440} day(s)` : `${timeoutMinutes} minute(s)`}. Logged with your name.`
                  : "The warning is added to their record. Logged with your name."
        }
        confirmLabel={confirm === "forget" ? "Forget profile" : confirm === "ban" ? "Ban" : confirm === "kick" ? "Kick" : confirm === "timeout" ? "Time out" : "Warn"}
        destructive={confirm !== "warn" && confirm !== "timeout"}
        onConfirm={() => {
          const which = confirm
          if (which === "warn") addWarning.mutate({ user_id: userId, reason: reasonValue }, { onSuccess: done("Warning added"), onError: failed("Could not warn") })
          else if (which === "timeout")
            timeout.mutate({ user_id: userId, duration_minutes: timeoutMinutes, reason: reasonValue }, { onSuccess: done("Member timed out"), onError: failed("Could not time out") })
          else if (which === "kick") kick.mutate({ user_id: userId, reason: reasonValue }, { onSuccess: done("Member kicked"), onError: failed("Could not kick") })
          else if (which === "ban")
            ban.mutate(
              { user_id: userId, reason: reasonValue, delete_message_days: Number(deleteDays) },
              { onSuccess: done("User banned"), onError: failed("Could not ban") }
            )
          else if (which === "forget")
            forget.mutate(userId, {
              onSuccess: () => toast({ title: "Cached profile forgotten", variant: "success" }),
              onError: (e) => toast({ title: "Could not forget the profile", description: describeApiError(e), variant: "destructive" }),
            })
        }}
      >
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
      </ConfirmDialog>

      {/* Records */}
      <Card className="gap-0 py-0">
        <Tabs defaultValue="warnings" className="gap-0">
          <TabsList className="px-3">
            <TabsTrigger value="warnings">
              Warnings {profile.warnings ? <CountBadge count={profile.warnings.length} /> : null}
            </TabsTrigger>
            <TabsTrigger value="history">
              Mod actions {profile.history ? <CountBadge count={profile.history.length} /> : null}
            </TabsTrigger>
            <TabsTrigger value="honeypot">Honeypot</TabsTrigger>
          </TabsList>
          <div className="flex justify-end px-5 pt-3">
            <LiveCaption fallbackText="Refreshing every 30 s." />
          </div>
          <TabsContent value="warnings">
            {profile.warnings === null ? (
              <InlineForbidden permission="moderate_members" />
            ) : profile.warnings.length === 0 ? (
              <EmptyState icon={MessageSquareWarning} title="No warnings" description="A clean record." />
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
                  {profile.warnings.map((w) => (
                    <TableRow key={w.id}>
                      <TableCell className="text-muted-foreground">{formatDateTime(w.created_at)}</TableCell>
                      <TableCell className="max-w-[320px] whitespace-normal">{w.reason ?? "No reason given"}</TableCell>
                      <TableCell>
                        <ResolvedUser userId={w.moderator_id} link />
                      </TableCell>
                      <TableCell className="text-right">
                        {can("moderate_members") ? (
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={removeWarning.isPending || bot.offline}
                            aria-label={`Remove warning ${w.id}`}
                            onClick={() =>
                              removeWarning.mutate(
                                { warning_id: w.id, user_id: userId },
                                { onSuccess: done("Warning removed"), onError: failed("Could not remove the warning") }
                              )
                            }
                          >
                            <Trash2 /> Remove
                          </Button>
                        ) : null}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </TabsContent>
          <TabsContent value="history">
            {profile.history === null ? (
              <InlineForbidden permission="moderate_members, kick_members or ban_members" />
            ) : profile.history.length === 0 ? (
              <EmptyState icon={Users} title="No mod actions" description="Kicks, bans, timeouts and warnings show up here." />
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
                  {profile.history.map((a) => (
                    <TableRow key={a.id}>
                      <TableCell className="text-muted-foreground">{formatDateTime(a.created_at)}</TableCell>
                      <TableCell>
                        <Badge variant={actionVariant(a.action_type)}>{a.action_type.replace("_", " ")}</Badge>
                      </TableCell>
                      <TableCell className="max-w-[280px] whitespace-normal">{a.reason ?? "No reason given"}</TableCell>
                      <TableCell>
                        <ResolvedUser userId={a.moderator_id} link />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </TabsContent>
          <TabsContent value="honeypot">
            {profile.honeypot === null ? (
              <InlineForbidden permission="moderate_members" />
            ) : profile.honeypot.catches.length === 0 && !profile.honeypot.latest_appeal ? (
              <EmptyState icon={Target} title="Never caught" description="No honeypot catches for this user." />
            ) : (
              <div className="flex flex-col gap-3 p-5">
                {profile.honeypot.latest_appeal ? (
                  <p className="text-sm">
                    Latest appeal: <Badge variant="info">{profile.honeypot.latest_appeal.status}</Badge>{" "}
                    <span className="text-muted-foreground">{formatDateTime(profile.honeypot.latest_appeal.created_at)}</span>
                  </p>
                ) : null}
                <ul className="flex flex-col divide-y divide-border">
                  {profile.honeypot.catches.map((c) => (
                    <li key={c.id} className="flex flex-col gap-0.5 py-2 text-sm">
                      <span>
                        <Badge variant={c.action_result === "failed" ? "destructive" : "warning"}>{c.action}</Badge>{" "}
                        <span className="text-muted-foreground">{formatDateTime(c.created_at)}</span>
                      </span>
                      <span className="truncate text-muted-foreground">{c.content || "(no text)"}</span>
                    </li>
                  ))}
                </ul>
                <Link to="/dashboard/honeypot" className="text-sm text-primary hover:underline">
                  Open the Honeypot page
                </Link>
              </div>
            )}
          </TabsContent>
        </Tabs>
      </Card>
    </div>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-foreground">{value}</dd>
    </div>
  )
}

function InlineForbidden({ permission }: { permission: string }) {
  return (
    <EmptyState
      icon={ShieldOff}
      title="You can't see this section"
      description={`It needs ${permission}.`}
    />
  )
}

function actionVariant(t: ModActionType): "destructive" | "warning" | "success" | "default" {
  if (t === "ban" || t === "kick") return "destructive"
  if (t === "timeout" || t === "warn") return "warning"
  if (t === "unban" || t === "untimeout") return "success"
  return "default"
}

