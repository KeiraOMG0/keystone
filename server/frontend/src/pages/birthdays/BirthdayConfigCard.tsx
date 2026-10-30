import { useEffect, useId, useMemo, useState } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Alert } from "@/components/ui/alert"
import { CharCounter, Field } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import { useToast } from "@/components/ui/toast"
import { SaveBar } from "@/components/patterns/SaveBar"
import { ApiError } from "@/api/client"
import {
  useBirthdayChannels,
  useBirthdayRoles,
  useBirthdayTimezones,
  useUpdateBirthdayConfig,
  type BirthdayConfigInput,
  type BirthdayOverview,
} from "@/api/birthdays"
import { useAuth } from "@/hooks/useAuth"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"

interface FormState {
  roleId: string
  channelId: string
  timezone: string
  announce_ping: boolean
  ping_user: boolean
  add_only: boolean
  private_confirms: boolean
  single: string
  plural: string
}

function fromOverview(o: BirthdayOverview): FormState {
  const s = o.settings
  return {
    roleId: s.birthday_role_id ?? "",
    channelId: s.announce_channel_id ?? "",
    timezone: s.guild_timezone ?? "",
    announce_ping: s.announce_ping,
    ping_user: s.ping_user,
    add_only: s.add_only,
    private_confirms: s.private_confirms,
    single: s.announce_message ?? "",
    plural: s.announce_message_pl ?? "",
  }
}

const HARDCODED_MENTION = /<@!?\d+>/g

const PLACEHOLDERS: [string, string][] = [
  ["{mention}", "@mention the birthday person (or their display name when \"Ping in announcement\" is off)"],
  ["{user}", "display name (no ping)"],
  ["{username}", "username"],
  ["{server}", "server name"],
  ["{membercount}", "number of members"],
  ["%e", "@everyone ping. Only full staff (owner or a staff role) can save a message containing it."],
]

/**
 * Birthday settings = /config add-only, private-confirms, birthday-role,
 * set-timezone and /config announce set-channel / set-message / set-ping /
 * ping-user. One SaveBar; the bot validates everything again (role policy,
 * text channel, timezone, %e staff-only) and answers 422 with per-field
 * messages, shown under the fields.
 */
export function BirthdayConfigCard({ overview }: { overview: BirthdayOverview }) {
  const { toast } = useToast()
  const { isStaff } = useAuth()
  const { offline, reason } = useBotOffline()
  const update = useUpdateBirthdayConfig()
  const roles = useBirthdayRoles()
  const channels = useBirthdayChannels()
  const timezones = useBirthdayTimezones()
  const tzListId = useId()
  const max = overview.limits.announce_message_max

  const saved = useMemo(() => fromOverview(overview), [overview])
  const [form, setForm] = useState<FormState>(saved)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [warnings, setWarnings] = useState<string[]>([])

  // Re-sync from the server (live updates / other editors) only while clean.
  const [base, setBase] = useState<FormState>(saved)
  const dirtyKeys = (Object.keys(form) as (keyof FormState)[]).filter((k) => form[k] !== base[k])
  useEffect(() => {
    if (dirtyKeys.length === 0) {
      setForm(saved)
      setBase(saved)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved])

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setForm((f) => ({ ...f, [k]: v }))

  const tzTrim = form.timezone.trim()
  const tzList = timezones.data?.timezones
  const tzUnknown = tzTrim !== "" && tzList !== undefined && !tzList.some((z) => z.toLowerCase() === tzTrim.toLowerCase())
  const everyoneBlocked =
    !isStaff &&
    ((form.single !== base.single && form.single.includes("%e")) || (form.plural !== base.plural && form.plural.includes("%e")))
  const hardcoded = [...form.single.matchAll(HARDCODED_MENTION), ...form.plural.matchAll(HARDCODED_MENTION)].map((m) => m[0])
  const overLimit = form.single.length > max || form.plural.length > max

  const sortedChannels = useMemo(
    () => [...(channels.data ?? [])].sort((a, b) => a.position - b.position),
    [channels.data]
  )
  const roleOptions = roles.data?.roles ?? []
  const currentRoleMissing = form.roleId !== "" && roles.data && !roleOptions.some((r) => r.id === form.roleId)

  function save() {
    const input: BirthdayConfigInput = {}
    if (form.roleId !== base.roleId) {
      if (form.roleId) input.birthday_role_id = form.roleId
      else input.clear_birthday_role = true
    }
    if (form.channelId !== base.channelId) {
      if (form.channelId) input.announce_channel_id = form.channelId
      else input.clear_announce_channel = true
    }
    if (form.timezone !== base.timezone) {
      if (tzTrim) input.guild_timezone = tzTrim
      else input.clear_guild_timezone = true
    }
    if (form.single !== base.single || form.plural !== base.plural) {
      input.set_messages = true
      input.announce_message = form.single
      input.announce_message_pl = form.plural
    }
    for (const k of ["announce_ping", "ping_user", "add_only", "private_confirms"] as const) {
      if (form[k] !== base[k]) input[k] = form[k]
    }
    setFieldErrors({})
    update.mutate(input, {
      onSuccess: (data) => {
        const next = fromOverview(data)
        setForm(next)
        setBase(next)
        setWarnings(data.warnings ?? [])
        toast({ title: "Birthday settings saved", variant: "success" })
      },
      onError: (e) => {
        const body = e instanceof ApiError ? (e.body as { fields?: Record<string, string> } | null) : null
        if (body?.fields) setFieldErrors(body.fields)
        toast({ title: "Could not save", description: describeApiError(e), variant: "destructive" })
      },
    })
  }

  const blockedReason = offline
    ? reason
    : tzUnknown
      ? "Unknown timezone"
      : everyoneBlocked
        ? "%e needs full staff"
        : overLimit
          ? `Messages are limited to ${max} characters`
          : undefined

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Role, channel and timezone</CardTitle>
          <CardDescription>
            The role is given for the day (in the member's timezone) and taken off afterwards. It follows the
            self-role rules: no @everyone, bot or managed roles, nothing at or above the bot or your own top role,
            no moderator permissions, no staff role or role holding bot permissions.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <Field
            label="Birthday role"
            error={fieldErrors.birthday_role_id}
            hint={overview.status.role.exists && !overview.status.role.usable ? "The bot can't assign the current role (it is at or above the bot)." : "Optional."}
          >
            {(p) => (
              <Select {...p} value={form.roleId} disabled={roles.isPending} onChange={(e) => set("roleId", e.target.value)}>
                <option value="">No birthday role</option>
                {currentRoleMissing ? <option value={form.roleId}>Deleted role ({form.roleId})</option> : null}
                {roleOptions.map((r) => (
                  <option key={r.id} value={r.id} disabled={r.blocked_reason !== null && r.id !== base.roleId}>
                    {r.name}
                    {r.blocked_reason ? ` (not allowed: ${r.blocked_reason})` : ""}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field
            label="Announcement channel"
            error={fieldErrors.announce_channel_id}
            hint={
              overview.status.channel.exists && !overview.status.channel.can_send
                ? "The bot is missing Send Messages in the current channel."
                : "No channel = no announcement (the role is still given)."
            }
          >
            {(p) => (
              <Select {...p} value={form.channelId} disabled={channels.isPending} onChange={(e) => set("channelId", e.target.value)}>
                <option value="">No announcements</option>
                {form.channelId && channels.data && !sortedChannels.some((c) => c.id === form.channelId) ? (
                  <option value={form.channelId}>Deleted channel ({form.channelId})</option>
                ) : null}
                {sortedChannels.map((c) => (
                  <option key={c.id} value={c.id}>
                    #{c.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Field
            label="Server timezone"
            error={fieldErrors.guild_timezone ?? (tzUnknown ? "Not a timezone the bot knows." : undefined)}
            hint="For members without their own timezone. Empty = UTC."
          >
            {(p) => (
              <>
                <Input {...p} list={tzListId} value={form.timezone} onChange={(e) => set("timezone", e.target.value)} placeholder="UTC" autoComplete="off" />
                <datalist id={tzListId}>
                  {(tzList ?? []).map((z) => (
                    <option key={z} value={z} />
                  ))}
                </datalist>
              </>
            )}
          </Field>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Options</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <ToggleRow id="bd-ping" checked={form.announce_ping} onChange={(v) => set("announce_ping", v)}
            label="Ping in announcement" hint="On: {mention} pings the member. Off: it shows their display name." />
          <ToggleRow id="bd-pinguser" checked={form.ping_user} onChange={(v) => set("ping_user", v)}
            label="Ping the birthday person separately" hint="Sends a short @mention message just before the announcement, so they are notified even if the message uses {user}." />
          <ToggleRow id="bd-addonly" checked={form.add_only} onChange={(v) => set("add_only", v)}
            label="Add-only mode" hint="Members can set their birthday once; only managers (here or /override) can change it." />
          <ToggleRow id="bd-private" checked={form.private_confirms} onChange={(v) => set("private_confirms", v)}
            label="Private confirmations" hint="/birthday replies are only visible to the member who ran them." />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Announcement message</CardTitle>
          <CardDescription>Line breaks are kept. Leave a message empty to use the default.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Field
            label="Single birthday"
            error={fieldErrors.announce_message}
            labelAction={<CharCounter value={form.single.length} max={max} />}
            hint="Default: Please wish a happy birthday to {mention}!"
          >
            {(p) => (
              <Textarea {...p} rows={5} value={form.single} onChange={(e) => set("single", e.target.value)}
                placeholder={"e.g.\n🎂 happy birthday {mention}!\n✦ today we celebrate {user}"} />
            )}
          </Field>
          <Field
            label="Several birthdays on the same day"
            error={fieldErrors.announce_message_pl}
            labelAction={<CharCounter value={form.plural.length} max={max} />}
            hint="Empty = the single message is used. {mention} / {user} become a comma-separated list."
          >
            {(p) => (
              <Textarea {...p} rows={4} value={form.plural} onChange={(e) => set("plural", e.target.value)}
                placeholder="Leave blank to fall back to the single message" />
            )}
          </Field>
          {everyoneBlocked ? (
            <Alert variant="warning" title="%e (@everyone) needs full staff">
              Only the owner or a staff role can save a message containing %e. Remove it, or ask a staff member to
              save it.
            </Alert>
          ) : null}
          {hardcoded.length > 0 ? (
            <Alert variant="warning" title="Hardcoded mention">
              Found {hardcoded.join(", ")}. Use {"{mention}"} instead so it pings the actual birthday person, not a
              fixed user.
            </Alert>
          ) : null}
          {warnings.map((w) => (
            <Alert key={w} variant="warning">{w}</Alert>
          ))}
          <div className="rounded-lg border border-border p-3 text-sm">
            <p className="mb-2 font-medium">Placeholders</p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
              {PLACEHOLDERS.map(([k, v]) => (
                <div key={k} className="contents">
                  <dt><code className="text-xs">{k}</code></dt>
                  <dd className="text-muted-foreground">{v}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-2 text-xs text-muted-foreground">
              Tip: write {"{user}"} in a styled message and turn on "Ping the birthday person separately" to notify
              them without a mention in the text. Role mentions typed into the message never ping.
            </p>
          </div>
        </CardContent>
      </Card>

      <SaveBar
        changes={dirtyKeys.length}
        onDiscard={() => {
          setForm(base)
          setFieldErrors({})
        }}
        onSave={save}
        saving={update.isPending}
        blockedReason={blockedReason}
      />
    </div>
  )
}

function ToggleRow({
  id,
  checked,
  onChange,
  label,
  hint,
}: {
  id: string
  checked: boolean
  onChange: (v: boolean) => void
  label: string
  hint: string
}) {
  return (
    <div className="flex items-start gap-3">
      <Switch id={id} checked={checked} onCheckedChange={onChange} className="mt-0.5" />
      <div className="flex flex-col">
        <Label htmlFor={id}>{label}</Label>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </div>
    </div>
  )
}
