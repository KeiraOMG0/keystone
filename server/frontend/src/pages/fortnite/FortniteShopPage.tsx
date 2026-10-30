import { useMemo } from "react"
import { ExternalLink, Send, ShoppingBag } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Field } from "@/components/ui/field"
import { Label } from "@/components/ui/label"
import { useToast } from "@/components/ui/toast"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { ErrorState } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { ApiError } from "@/api/client"
import { useBotOffline } from "@/hooks/useBotOffline"
import { formatDateTime, formatRelativeTime } from "@/lib/format"
import {
  useFortniteChannels,
  useFortnitePostNow,
  useFortniteRoles,
  useFortniteStatus,
  useUpdateFortniteConfig,
} from "@/api/fortnite"
import type { ForbiddenError, UpdateFortniteConfigInput } from "@/types/api"

function forbiddenBody(error: unknown): ForbiddenError | null {
  if (error instanceof ApiError && error.status === 403) {
    const body = error.body as ForbiddenError | null
    if (body && body.error === "forbidden") return body
  }
  return null
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body as { error?: string; message?: string } | null
    if (body?.error === "channel_not_set") return "Set a channel first."
    if (body?.error === "role_not_pingable") return "Pick a normal role (not @everyone or a bot role)."
    if (body?.message) return body.message
    if (body?.error) return body.error.replace(/_/g, " ")
  }
  return error instanceof Error ? error.message : "Unknown error"
}

/**
 * Fortnite item shop settings (/dashboard/fortnite, perm manage_schedules).
 * Edits the same fortnite_shop_config row the /fortnite slash commands write;
 * the bot's 60s loop posts once per shop date shortly after 00:00 UTC.
 */
export default function FortniteShopPage() {
  const statusQuery = useFortniteStatus()
  const channelsQuery = useFortniteChannels()
  const rolesQuery = useFortniteRoles()
  const update = useUpdateFortniteConfig()
  const postNow = useFortnitePostNow()
  const { offline, reason } = useBotOffline()
  const { toast } = useToast()

  const channels = useMemo(
    () => [...(channelsQuery.data ?? [])].sort((a, b) => a.position - b.position),
    [channelsQuery.data]
  )
  const roles = useMemo(
    () =>
      [...(rolesQuery.data ?? [])]
        // @everyone would post a broken <@&guild id> mention (the bot rejects it too).
        .filter((r) => !r.managed && r.name !== "@everyone")
        .sort((a, b) => b.position - a.position),
    [rolesQuery.data]
  )

  const forbidden = forbiddenBody(statusQuery.error)
  if (forbidden) return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />

  const st = statusQuery.data
  const cfg = st?.config

  const save = (input: UpdateFortniteConfigInput, done: string) =>
    update.mutate(input, {
      onSuccess: () => toast({ title: done, variant: "success" }),
      onError: (err) => toast({ title: "Could not save", description: errorText(err), variant: "destructive" }),
    })

  const busy = update.isPending || offline

  return (
    <PageContainer width="form">
      <PageHeader
        title="Fortnite shop"
        description="Daily item shop post, shortly after the 00:00 UTC rotation. Members browse it any time with !shop."
        actions={
          <Button
            onClick={() =>
              postNow.mutate(undefined, {
                onSuccess: (r) =>
                  toast({ title: `Posted the ${r.shop_date} shop`, description: `${r.message_count} messages`, variant: "success" }),
                onError: (err) => toast({ title: "Could not post", description: errorText(err), variant: "destructive" }),
              })
            }
            loading={postNow.isPending}
            disabled={postNow.isPending || offline || !cfg?.channel_id}
            title={reason ?? (!cfg?.channel_id ? "Set a channel first" : undefined)}
          >
            <Send /> Post now
          </Button>
        }
      />

      {statusQuery.isPending ? (
        <Card>
          <CardContent className="flex flex-col gap-3">
            <Skeleton className="h-6 w-1/3" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
          </CardContent>
        </Card>
      ) : statusQuery.isError || !st || !cfg ? (
        <Card>
          <ErrorState
            title="Could not load the Fortnite shop settings"
            message={errorText(statusQuery.error)}
            onRetry={() => statusQuery.refetch()}
            retrying={statusQuery.isFetching}
            showStatusLink
          />
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <ShoppingBag className="size-4" aria-hidden /> Daily post
                {cfg.enabled && cfg.channel_id ? (
                  <Badge variant="success" dot>On</Badge>
                ) : (
                  <Badge variant="outline">Off</Badge>
                )}
              </CardTitle>
              <CardDescription>
                Posted once per shop date (restarts never double-post). "Post now" does not count as the daily post.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="flex items-center gap-2">
                <Switch
                  id="fn-enabled"
                  checked={cfg.enabled}
                  disabled={busy}
                  onCheckedChange={(v) => save({ enabled: v }, v ? "Daily post enabled" : "Daily post disabled")}
                />
                <Label htmlFor="fn-enabled">Post the shop every day</Label>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Channel" hint={!cfg.channel_id ? "Required for the daily post." : undefined}>
                  {(p) => (
                    <Select
                      {...p}
                      value={cfg.channel_id ?? ""}
                      disabled={busy || channelsQuery.isPending}
                      onChange={(e) =>
                        save(e.target.value ? { channel_id: e.target.value } : { clear_channel: true }, "Channel saved")
                      }
                    >
                      <option value="">No channel</option>
                      {channels.map((c) => (
                        <option key={c.id} value={c.id}>
                          #{c.name}
                        </option>
                      ))}
                    </Select>
                  )}
                </Field>
                <Field label="Ping role" hint="Optional. Pinged once by the daily post.">
                  {(p) => (
                    <Select
                      {...p}
                      value={cfg.ping_role_id ?? ""}
                      disabled={busy || rolesQuery.isPending}
                      onChange={(e) =>
                        save(e.target.value ? { ping_role_id: e.target.value } : { clear_ping_role: true }, "Ping role saved")
                      }
                    >
                      <option value="">No ping</option>
                      {roles.map((r) => (
                        <option key={r.id} value={r.id}>
                          {r.name}
                        </option>
                      ))}
                    </Select>
                  )}
                </Field>
              </div>
              <div className="flex items-center gap-2">
                <Switch
                  id="fn-tracks"
                  checked={cfg.include_jam_tracks}
                  disabled={busy}
                  onCheckedChange={(v) =>
                    save({ include_jam_tracks: v }, v ? "Jam Tracks will be listed" : "Jam Tracks will be summarised")
                  }
                />
                <Label htmlFor="fn-tracks">List every Jam Track (off: one summary line)</Label>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Status</CardTitle>
              <CardDescription>
                Source: fortnite-api.com. Post time is 00:{String(st.post_delay_minutes).padStart(2, "0")} UTC.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-3 text-sm">
              <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5">
                <dt className="text-muted-foreground">Last posted</dt>
                <dd>
                  {cfg.last_posted_date ? (
                    <>
                      {cfg.last_posted_date}
                      {cfg.last_posted_at ? (
                        <span className="text-muted-foreground"> ({formatRelativeTime(cfg.last_posted_at)})</span>
                      ) : null}
                    </>
                  ) : (
                    "Never"
                  )}
                </dd>
                <dt className="text-muted-foreground">Next post</dt>
                <dd>
                  {!st.next_post_at
                    ? "Not scheduled (disabled or no channel)"
                    : st.post_pending && new Date(st.next_post_at).getTime() <= Date.now() + 5_000
                      ? "Today's post is due, within a minute"
                      : st.post_pending
                        ? `Today's post: retrying at ${formatDateTime(st.next_post_at)}`
                        : formatDateTime(st.next_post_at)}
                </dd>
                <dt className="text-muted-foreground">Cached shop</dt>
                <dd>{st.cache.shop_date ? `${st.cache.shop_date}, ${st.cache.entry_count} offers` : "None yet"}</dd>
                <dt className="text-muted-foreground">Wishlists</dt>
                <dd>
                  {st.wishlist.entries} items from {st.wishlist.users} members (/fortnite wishlist add)
                </dd>
              </dl>
              {cfg.last_error ? (
                <Alert variant="warning" title="Last problem">
                  {cfg.last_error}
                  {cfg.last_error_at ? ` (${formatRelativeTime(cfg.last_error_at)})` : ""}
                </Alert>
              ) : null}
              {st.last_fetch_error ? <Alert variant="warning" title="Last fetch failed">{st.last_fetch_error}</Alert> : null}
              {postNow.data?.jump_url ? (
                <Button asChild size="sm" variant="secondary" className="w-fit">
                  <a href={postNow.data.jump_url} target="_blank" rel="noreferrer">
                    <ExternalLink /> Open the last manual post
                  </a>
                </Button>
              ) : null}
            </CardContent>
          </Card>
        </>
      )}
    </PageContainer>
  )
}
