import { useEffect, useMemo, useState, type ReactNode } from "react"
import { useSearchParams } from "react-router-dom"
import { Lock, RotateCcw, RefreshCw } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Alert } from "@/components/ui/alert"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { useToast } from "@/components/ui/toast"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { StaffOnlyBadge } from "@/components/patterns/StaffOnlyBadge"
import { SaveBar } from "@/components/patterns/SaveBar"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { ErrorState } from "@/components/patterns/states"
import {
  settingsFieldErrors,
  useSettings,
  useUpdateSettings,
  type SettingFieldMeta,
  type SettingFieldStatus,
  type SettingGroup,
  type SettingsOptions,
  type SettingValue,
} from "@/api/settings"
import { isSupervisorUnavailable, useSupervisorRestart } from "@/api/supervisor"
import { ApiError } from "@/api/client"
import { describeApiError } from "@/lib/apiError"
import { discordIdError } from "@/lib/format"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"
import { useAuth } from "@/hooks/useAuth"
import { cn } from "@/lib/utils"
import type { ForbiddenError } from "@/types/api"

const GROUP_ORDER: SettingGroup[] = ["verification", "logging", "antispam", "tickets", "music", "permissions"]

const GROUP_COPY: Record<SettingGroup, { title: string; description: string }> = {
  verification: { title: "Verification", description: "Roles, category, and behaviour for the member verification flow." },
  logging: { title: "Logging", description: "Where moderation and join/leave activity gets posted." },
  tickets: { title: "Tickets", description: "Ticket chat-log capture and archive retention." },
  music: { title: "Music", description: "Defaults for the music player." },
  antispam: { title: "Anti-spam", description: "Automatic spam detection: what counts as spam and what happens to the sender. Changes apply within seconds." },
  permissions: { title: "Permissions", description: "Core ownership setting. View-only here." },
}

/** How long to keep polling for the restarted bot before giving up. */
const RESTART_WATCH_MS = 180_000

/** Supervisor restart reason (server/routes/supervisor.py caps it at 300 chars). */
function restartReason(username: string | undefined, keys: string[]): string {
  const reason = `Settings changed by ${username || "unknown"}: ${keys.length ? keys.join(", ") : "apply saved settings"}`
  return reason.length > 300 ? reason.slice(0, 297) + "..." : reason
}

type Draft = Record<string, SettingValue>

/**
 * STAFF-ONLY. Form for services/settings_service.py SETTINGS_SCHEMA.
 *
 * The bot validates everything (IDs must exist and be the right kind, roles
 * must be assignable by the bot, templates must exist, ranges) and answers
 * 422 with per-field messages, shown inline. Only the ticket chat-log /
 * purge settings and the anti-spam group apply live ("Live" badge); the rest
 * are applied by main.py at bot startup ("Applies on restart"), so the page
 * shows saved vs running values (the bot's GET reports both, so pending
 * changes survive reloads) and a "Restart bot now" banner while anything is
 * pending. "Save & restart bot" (shown when the draft touches a
 * restart-required field) saves, then requests the restart in one go. Both
 * go through POST /api/supervisor/restart (require_staff_even_if_bot_down)
 * with the reason "Settings changed by <user>: <keys>". `null` in the draft =
 * reset to the .env default. `?tab=` selects the group (the command palette
 * deep-links to it).
 */
export default function SettingsPage() {
  const [restartWatchUntil, setRestartWatchUntil] = useState<number | null>(null)
  // applied_at of the bot that was running when the restart was requested.
  const [watchBaseline, setWatchBaseline] = useState<number | null | undefined>(undefined)
  const { user } = useAuth()
  const settingsQuery = useSettings({ refetchInterval: restartWatchUntil ? 5_000 : false })
  const updateSettings = useUpdateSettings()
  const restart = useSupervisorRestart()
  const { toast } = useToast()
  const [searchParams, setSearchParams] = useSearchParams()
  const [draft, setDraft] = useState<Draft>({})
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({})
  const [confirmRestart, setConfirmRestart] = useState(false)
  const [confirmSaveRestart, setConfirmSaveRestart] = useState(false)
  // Shared unsaved-changes guard (links, palette, hotkeys, Back/Forward, reload, tab close).
  useUnsavedChangesGuard(Object.keys(draft).length > 0)

  const data = settingsQuery.data
  const schema = data?.schema
  const pendingKeys = data?.pending_restart ?? []

  // Stop the post-restart polling once nothing is pending or it timed out.
  useEffect(() => {
    if (!restartWatchUntil) return
    const restarted = data?.applied_at != null && watchBaseline !== undefined && data.applied_at !== watchBaseline
    if (data && !settingsQuery.isError && (data.pending_restart.length === 0 || restarted)) {
      setRestartWatchUntil(null)
      if (data.pending_restart.length === 0) {
        toast({ title: "Bot restarted", description: "Saved settings are now in use.", variant: "success" })
      } else {
        toast({
          title: "Bot restarted",
          description: "Some saved settings still aren't in use; check the fields marked Pending restart.",
          variant: "destructive",
        })
      }
      return
    }
    const t = window.setTimeout(() => setRestartWatchUntil(null), Math.max(0, restartWatchUntil - Date.now()))
    return () => window.clearTimeout(t)
  }, [restartWatchUntil, watchBaseline, data, settingsQuery.isError, toast])

  const grouped = useMemo(() => {
    const map = new Map<SettingGroup, SettingFieldMeta[]>()
    for (const field of schema ?? []) {
      const list = map.get(field.group) ?? []
      list.push(field)
      map.set(field.group, list)
    }
    return map
  }, [schema])

  const fieldsByKey = useMemo(() => new Map((schema ?? []).map((f) => [f.key, f])), [schema])

  const localErrors = useMemo(() => {
    const out: Record<string, string> = {}
    for (const [key, value] of Object.entries(draft)) {
      const field = fieldsByKey.get(key)
      if (!field) continue
      const err = localError(field, value)
      if (err) out[key] = err
    }
    return out
  }, [draft, fieldsByKey])

  if (settingsQuery.isError && !data && settingsQuery.error instanceof ApiError && settingsQuery.error.status === 403) {
    const body = settingsQuery.error.body as ForbiddenError | { error?: string } | null
    return (
      <Forbidden403
        reason={body?.error === "not_a_member" ? "not_a_member" : "forbidden"}
        requiredPermission={(body as ForbiddenError | null)?.required_permission ?? "staff"}
      />
    )
  }

  const requestedTab = searchParams.get("tab") as SettingGroup | null
  const activeTab =
    requestedTab && grouped.has(requestedTab) ? requestedTab : (GROUP_ORDER.find((g) => grouped.has(g)) ?? GROUP_ORDER[0])
  const groups = GROUP_ORDER.filter((g) => grouped.has(g))

  const selectGroup = (next: SettingGroup) =>
    setSearchParams(
      (prev) => {
        const params = new URLSearchParams(prev)
        params.set("tab", next)
        return params
      },
      { replace: true }
    )

  const errors: Record<string, string> = { ...serverErrors, ...localErrors }
  const changes = Object.keys(draft).length
  const blockedReason = Object.keys(localErrors).length ? "Fix the highlighted fields first." : undefined
  // Draft keys that only take effect after a bot restart.
  const restartDraftKeys = Object.keys(draft).filter((k) => {
    const f = fieldsByKey.get(k)
    return f !== undefined && !f.live && !f.read_only
  })

  function setValue(key: string, value: SettingValue) {
    setServerErrors((prev) => {
      if (!(key in prev)) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
    setDraft((prev) => {
      const next = { ...prev }
      const saved = data?.values[key]
      // Editing back to the saved value is not a change.
      if (value !== null && saved !== undefined && String(value) === String(saved ?? "")) {
        delete next[key]
      } else {
        next[key] = value
      }
      return next
    })
  }

  function handleSave(opts?: { restartAfter?: boolean }) {
    if (changes === 0 || blockedReason) return
    const payload: Draft = {}
    for (const [key, value] of Object.entries(draft)) {
      const field = fieldsByKey.get(key)
      payload[key] = field?.type === "int" && typeof value === "string" && /^-?\d+$/.test(value.trim()) ? Number(value) : value
    }
    updateSettings.mutate(payload, {
      onSuccess: (res) => {
        setDraft({})
        setServerErrors({})
        const pendingNow = (res.changed ?? []).filter((k) => res.fields[k]?.pending_restart)
        if (opts?.restartAfter) {
          setConfirmSaveRestart(false)
          if (res.pending_restart.length) {
            toast({ title: "Settings saved", description: "Restarting the bot to apply them.", variant: "success" })
            requestRestart(res.pending_restart, res.applied_at)
          } else {
            toast({
              title: res.changed?.length ? "Settings saved" : "Nothing changed",
              description: "Nothing needs a restart, so the bot was left running.",
              variant: "success",
            })
          }
          return
        }
        toast({
          title: res.changed?.length ? "Settings saved" : "Nothing changed",
          description: pendingNow.length
            ? `${pendingNow.length} ${pendingNow.length === 1 ? "change applies" : "changes apply"} after the bot restarts. Use "Restart bot now" when ready.`
            : undefined,
          variant: "success",
        })
      },
      onError: (error) => {
        setConfirmSaveRestart(false)
        const fieldErrors = settingsFieldErrors(error)
        if (fieldErrors) {
          setServerErrors(fieldErrors)
          const firstGroup = Object.keys(fieldErrors)
            .map((k) => fieldsByKey.get(k)?.group)
            .find((g): g is SettingGroup => !!g)
          if (firstGroup && !Object.keys(fieldErrors).some((k) => fieldsByKey.get(k)?.group === activeTab)) {
            selectGroup(firstGroup)
          }
          toast({ title: "Some settings are invalid", description: "Check the highlighted fields.", variant: "destructive" })
          return
        }
        toast({ title: "Failed to save settings", description: describeApiError(error), variant: "destructive" })
      },
    })
  }

  function requestRestart(keys: string[], baseline: number | null | undefined) {
    restart.mutate(
      { target: "bot", reason: restartReason(user?.username, keys) },
      {
        onSuccess: (res) => {
          setConfirmRestart(false)
          if (!res.accepted.length) {
            toast({
              title: "Restart not started",
              description: res.skipped.map((s) => s.message).join("; ") || undefined,
              variant: "destructive",
            })
            return
          }
          setWatchBaseline(baseline ?? null)
          setRestartWatchUntil(Date.now() + RESTART_WATCH_MS)
          toast({ title: "Restarting the bot", description: "This page refreshes when it is back.", variant: "info" })
        },
        onError: (err) => {
          setConfirmRestart(false)
          const body = err instanceof ApiError ? (err.body as { message?: string; retry_after_s?: number } | null) : null
          const description =
            err instanceof ApiError && err.status === 429
              ? `Restarted moments ago. Try again in ${body?.retry_after_s ?? 30}s.`
              : err instanceof ApiError && err.status === 409
                ? (body?.message ?? "A restart is already in progress.")
                : isSupervisorUnavailable(err)
                  ? "The bot isn't running under supervisor.py. Restart it by hand to apply the settings."
                  : describeApiError(err)
          toast({ title: "Restart not requested", description, variant: "destructive" })
        },
      }
    )
  }

  const pendingLabels = pendingKeys.map((k) => fieldsByKey.get(k)?.label ?? k)

  return (
    <PageContainer width="form">
      <PageHeader
        title="Settings"
        badge={<StaffOnlyBadge />}
        description="Bot behaviour you can change without touching the server files."
      />

      {settingsQuery.isPending ? (
        <div className="grid gap-6 md:grid-cols-[200px_minmax(0,1fr)]" aria-busy="true">
          <Skeleton className="hidden h-48 md:block" />
          <Card>
            <CardContent className="flex flex-col gap-5">
              {Array.from({ length: 4 }).map((_, i) => (
                <div key={i} className="flex flex-col gap-2">
                  <Skeleton className="h-4 w-40" />
                  <Skeleton className="h-9 w-full max-w-xs" />
                </div>
              ))}
            </CardContent>
          </Card>
        </div>
      ) : null}

      {settingsQuery.isError && !data ? (
        <Card>
          <ErrorState
            title="Could not load settings"
            message={describeApiError(settingsQuery.error, "Try again, or check the service status.")}
            onRetry={() => void settingsQuery.refetch()}
            retrying={settingsQuery.isRefetching}
            showStatusLink
          />
        </Card>
      ) : null}

      {data ? (
        <>
          {restartWatchUntil ? (
            <Alert variant="info" title="Restarting the bot">
              Waiting for it to come back. Saved settings show as applied once it has restarted.
            </Alert>
          ) : pendingKeys.length ? (
            <Alert
              variant="warning"
              title={`${pendingKeys.length} saved ${pendingKeys.length === 1 ? "change applies" : "changes apply"} after a restart`}
              action={
                <Button size="sm" variant="secondary" onClick={() => setConfirmRestart(true)} disabled={restart.isPending}>
                  <RefreshCw aria-hidden /> Restart bot now
                </Button>
              }
            >
              The running bot still uses the old value for: {pendingLabels.join(", ")}.
            </Alert>
          ) : null}

          {settingsQuery.isError ? (
            <Alert variant="warning">
              Couldn't refresh the settings ({describeApiError(settingsQuery.error)}). Showing the last values loaded.
            </Alert>
          ) : null}

          {!data.guild_available ? (
            <Alert variant="info">
              The bot hasn't loaded the server yet, so role and channel pickers are empty and ID changes can't be checked. Try again in a moment.
            </Alert>
          ) : null}

          <div className="grid items-start gap-6 md:grid-cols-[200px_minmax(0,1fr)]">
            <nav aria-label="Settings groups" className="flex gap-1 overflow-x-auto md:flex-col md:overflow-visible">
              {groups.map((group) => {
                const keys = (grouped.get(group) ?? []).map((f) => f.key)
                const hasError = keys.some((k) => k in errors)
                const hasPending = keys.some((k) => pendingKeys.includes(k) || k in draft)
                return (
                  <button
                    key={group}
                    type="button"
                    aria-current={group === activeTab ? "page" : undefined}
                    onClick={() => selectGroup(group)}
                    className={cn(
                      "flex min-h-9 items-center gap-2 rounded-lg px-3 text-left text-base font-medium whitespace-nowrap transition-colors duration-[120ms] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring max-md:min-h-11",
                      group === activeTab ? "bg-primary-soft text-primary-soft-foreground" : "text-muted-foreground hover:bg-hover hover:text-foreground"
                    )}
                  >
                    {GROUP_COPY[group].title}
                    {hasError ? (
                      <span className="ml-auto size-2 rounded-full bg-danger" aria-label="has errors" />
                    ) : hasPending ? (
                      <span className="ml-auto size-2 rounded-full bg-warning" aria-label="has changes" />
                    ) : group === "permissions" ? (
                      <Lock className="ml-auto size-3.5" aria-label="view only" />
                    ) : null}
                  </button>
                )
              })}
            </nav>

            <Card>
              <CardHeader>
                <CardTitle>{GROUP_COPY[activeTab].title}</CardTitle>
                <CardDescription>{GROUP_COPY[activeTab].description}</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-6">
                {(grouped.get(activeTab) ?? []).map((field) => {
                  const status = data.fields[field.key]
                  const inDraft = field.key in draft
                  const draftValue = draft[field.key]
                  const value = inDraft ? (draftValue === null ? (status?.default ?? null) : draftValue) : (data.values[field.key] ?? null)
                  return (
                    <SettingRow
                      key={field.key}
                      field={field}
                      value={value}
                      status={status}
                      options={data.options}
                      guildAvailable={data.guild_available}
                      resetting={inDraft && draftValue === null}
                      error={errors[field.key]}
                      onChange={(v) => setValue(field.key, v)}
                      onReset={() => setDraft((prev) => ({ ...prev, [field.key]: null }))}
                    />
                  )
                })}
              </CardContent>
            </Card>
          </div>

          <SaveBar
            changes={changes}
            onDiscard={() => {
              setDraft({})
              setServerErrors({})
            }}
            onSave={() => handleSave()}
            saving={updateSettings.isPending}
            blockedReason={blockedReason}
            extra={
              restartDraftKeys.length ? (
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => setConfirmSaveRestart(true)}
                  disabled={Boolean(blockedReason) || updateSettings.isPending || restart.isPending || !!restartWatchUntil}
                  title="Save, then restart the bot so the restart-only changes take effect"
                >
                  <RefreshCw aria-hidden /> Save &amp; restart bot
                </Button>
              ) : undefined
            }
          />

          <ConfirmDialog
            open={confirmRestart}
            onOpenChange={setConfirmRestart}
            title="Restart the bot?"
            description="The bot goes offline for about a minute (music stops, commands pause) and starts again with the saved settings."
            confirmLabel="Restart bot"
            onConfirm={() => requestRestart(pendingKeys, data.applied_at)}
            loading={restart.isPending}
          />

          <ConfirmDialog
            open={confirmSaveRestart}
            onOpenChange={setConfirmSaveRestart}
            title="Save and restart the bot?"
            description={`Saves ${changes} ${changes === 1 ? "change" : "changes"}, then restarts the bot so these apply: ${restartDraftKeys
              .map((k) => fieldsByKey.get(k)?.label ?? k)
              .join(", ")}. The bot goes offline for about a minute (music stops, commands pause).`}
            confirmLabel="Save & restart"
            onConfirm={() => handleSave({ restartAfter: true })}
            loading={updateSettings.isPending || restart.isPending}
          />
        </>
      ) : null}
    </PageContainer>
  )
}

// ---------------------------------------------------------------------------
// Field row
// ---------------------------------------------------------------------------

function SettingRow({
  field,
  value,
  status,
  options,
  guildAvailable,
  resetting,
  error,
  onChange,
  onReset,
}: {
  field: SettingFieldMeta
  value: SettingValue
  status: SettingFieldStatus | undefined
  options: SettingsOptions
  guildAvailable: boolean
  resetting: boolean
  error: string | undefined
  onChange: (value: SettingValue) => void
  onReset: () => void
}) {
  const id = `setting-${field.key}`
  const descId = `${id}-desc`
  const errId = `${id}-err`
  const describedBy = error ? `${descId} ${errId}` : descId

  const badge = field.read_only ? (
    <Badge variant="outline">
      <Lock aria-hidden /> View only
    </Badge>
  ) : field.live ? (
    <Badge variant="success" title="Applies within seconds of saving">
      Live
    </Badge>
  ) : (
    <Badge variant="default" title="Saved values apply when the bot restarts">
      Applies on restart
    </Badge>
  )

  const canReset = !field.read_only && status?.source === "db" && !resetting

  const header = (
    <div className="flex flex-wrap items-center gap-2">
      <Label htmlFor={id}>{field.label}</Label>
      {badge}
      {status?.pending_restart ? <Badge variant="warning">Pending restart</Badge> : null}
      {resetting ? <Badge variant="info">Resets to default on save</Badge> : null}
      {canReset ? (
        <Button type="button" variant="link" size="xs" className="ml-auto" onClick={onReset}>
          <RotateCcw aria-hidden /> Reset to default
        </Button>
      ) : null}
    </div>
  )

  const notes = (
    <>
      {error ? (
        <p id={errId} role="alert" className="text-xs text-danger">
          {error}
        </p>
      ) : null}
      {status?.invalid_stored ? <p className="text-xs text-warning">{status.invalid_stored}</p> : null}
      {status?.warning ? <p className="text-xs text-warning">{status.warning}</p> : null}
      {status?.pending_restart ? (
        <p className="text-xs text-muted-foreground">
          Saved: {display(field, status.saved, options)}. The bot is using {display(field, status.running, options)} until it restarts.
        </p>
      ) : null}
    </>
  )

  if (field.type === "bool") {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1">
            {header}
            <p id={descId} className="text-xs text-muted-foreground">
              {field.description}
            </p>
          </div>
          <Switch
            id={id}
            aria-describedby={describedBy}
            checked={value === true}
            disabled={field.read_only}
            onCheckedChange={(v) => onChange(v)}
          />
        </div>
        {notes}
      </div>
    )
  }

  const invalid = error ? true : undefined
  let control: ReactNode

  if (field.read_only) {
    control = (
      <Input id={id} aria-describedby={describedBy} value={value === null ? "Not set" : String(value)} disabled className="w-full max-w-sm font-mono" />
    )
  } else if (field.type === "snowflake" && guildAvailable && pickerItems(field, options).length) {
    const items = pickerItems(field, options)
    const current = value === null ? "" : String(value)
    const known = items.some((i) => i.id === current)
    control = (
      <Select id={id} aria-describedby={describedBy} aria-invalid={invalid} value={current} onChange={(e) => onChange(e.target.value)} className="max-w-sm">
        {current === "" ? <option value="">Not set</option> : null}
        {!known && current ? <option value={current}>Unknown ({current})</option> : null}
        {items.map((i) => (
          <option key={i.id} value={i.id} disabled={i.disabled && i.id !== current}>
            {i.label}
          </option>
        ))}
      </Select>
    )
  } else if (field.kind === "template" && options.templates.length) {
    const current = value === null ? "" : String(value)
    control = (
      <Select id={id} aria-describedby={describedBy} aria-invalid={invalid} value={current} onChange={(e) => onChange(e.target.value)} className="max-w-sm">
        {!options.templates.includes(current) ? <option value={current}>{current ? `${current} (missing)` : "Not set"}</option> : null}
        {options.templates.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </Select>
    )
  } else if (field.type === "int") {
    control = (
      <Input
        id={id}
        type="number"
        inputMode="numeric"
        min={field.min ?? undefined}
        max={field.max ?? undefined}
        step={1}
        aria-describedby={describedBy}
        aria-invalid={invalid}
        value={value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value)}
        className="w-full max-w-xs"
      />
    )
  } else {
    // Snowflake without a picker (guild not loaded) or a free-text string.
    const preview = field.type === "snowflake" && value ? resolvedName(field, String(value), options) : null
    control = (
      <div className="flex flex-col gap-1">
        <Input
          id={id}
          type="text"
          inputMode={field.type === "snowflake" ? "numeric" : undefined}
          autoComplete="off"
          aria-describedby={describedBy}
          aria-invalid={invalid}
          value={value === null ? "" : String(value)}
          onChange={(e) => onChange(e.target.value)}
          className={cn("w-full max-w-sm", field.type === "snowflake" && "font-mono")}
        />
        {preview ? <p className="text-xs text-muted-foreground">{preview}</p> : null}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-1.5">
      {header}
      <p id={descId} className="text-xs text-muted-foreground">
        {field.description}
      </p>
      {control}
      {notes}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface PickerItem {
  id: string
  label: string
  disabled: boolean
}

function pickerItems(field: SettingFieldMeta, options: SettingsOptions): PickerItem[] {
  if (field.kind === "role") {
    return options.roles.map((r) => ({
      id: r.id,
      label: r.assignable ? `@${r.name}` : `@${r.name} (bot can't assign)`,
      disabled: !r.assignable,
    }))
  }
  if (field.kind === "category") {
    return options.categories.map((c) => ({ id: c.id, label: c.name, disabled: false }))
  }
  if (field.kind === "text_channel") {
    return options.text_channels.map((c) => ({
      id: c.id,
      label: `#${c.name}${c.category ? ` (${c.category})` : ""}${c.bot_can_post ? "" : " (bot can't post)"}`,
      disabled: !c.bot_can_post,
    }))
  }
  return []
}

function resolvedName(field: SettingFieldMeta, id: string, options: SettingsOptions): string | null {
  if (field.kind === "role") {
    const r = options.roles.find((x) => x.id === id)
    return r ? `@${r.name}` : null
  }
  if (field.kind === "category") return options.categories.find((x) => x.id === id)?.name ?? null
  if (field.kind === "text_channel") {
    const c = options.text_channels.find((x) => x.id === id)
    return c ? `#${c.name}` : null
  }
  return null
}

function display(field: SettingFieldMeta, value: SettingValue, options: SettingsOptions): string {
  if (value === null || value === "") return "not set"
  if (typeof value === "boolean") return value ? "on" : "off"
  if (field.type === "snowflake") return resolvedName(field, String(value), options) ?? String(value)
  return String(value)
}

/** Client-side mirror of the bot's type/range checks (the bot re-checks everything). */
function localError(field: SettingFieldMeta, value: SettingValue): string | null {
  if (value === null || field.type === "bool") return null
  const s = String(value).trim()
  if (field.type === "int") {
    if (!/^-?\d+$/.test(s)) return "Must be a whole number."
    const n = Number(s)
    if (field.min !== null && n < field.min) return `Must be at least ${field.min}.`
    if (field.max !== null && n > field.max) return `Must be at most ${field.max}.`
    return null
  }
  if (field.type === "snowflake") {
    if (!s) return "Pick one, or use Reset to default."
    return discordIdError(s)
  }
  if (!s) return "Can't be empty."
  if (field.max !== null && s.length > field.max) return `Must be at most ${field.max} characters.`
  if (field.pattern && !new RegExp(field.pattern).test(s)) return "Must be ytsearchN or scsearchN, where N is 1 to 99 (e.g. ytsearch7)."
  return null
}
