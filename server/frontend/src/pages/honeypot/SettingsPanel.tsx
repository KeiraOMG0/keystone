import { useState, type FormEvent } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Field } from "@/components/ui/field"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"
import { SaveBar } from "@/components/patterns/SaveBar"
import { useUpdateHoneypotConfig } from "@/api/honeypot"
import { useBotOffline } from "@/hooks/useBotOffline"
import { discordIdError } from "@/lib/format"
import type { HoneypotAction, HoneypotConfig } from "@/types/api"
import { ACTION_HELP, apiErrorMessage } from "./format"

/** The editable fields of a config, as a comparable string. */
function settingsSig(c: HoneypotConfig): string {
  return JSON.stringify([c.action, c.dm_before, c.appeal_enabled, c.delete_message_days, c.log_channel_id ?? ""])
}

/**
 * Editable settings (PUT /config). NOT remounted on live updates any more:
 * when the server config changes (another moderator saved, or a
 * `config_changed` live event), a clean draft silently follows it, but a
 * DIRTY draft is kept and a "changed elsewhere" notice offers to load the
 * latest values. The SaveBar guards the page while dirty.
 */
export function SettingsPanel({ config, canManage }: { config: HoneypotConfig; canManage: boolean }) {
  const update = useUpdateHoneypotConfig()
  const { toast } = useToast()
  const bot = useBotOffline()
  const [action, setAction] = useState<HoneypotAction>(config.action)
  const [dmBefore, setDmBefore] = useState(config.dm_before)
  const [appealEnabled, setAppealEnabled] = useState(config.appeal_enabled)
  const [days, setDays] = useState(String(config.delete_message_days))
  const [logChannel, setLogChannel] = useState(config.log_channel_id ?? "")
  const [logTouched, setLogTouched] = useState(false)
  /** The server config the draft was started from. */
  const [base, setBase] = useState<HoneypotConfig>(config)
  const [changedElsewhere, setChangedElsewhere] = useState(false)

  function loadFrom(c: HoneypotConfig) {
    setAction(c.action)
    setDmBefore(c.dm_before)
    setAppealEnabled(c.appeal_enabled)
    setDays(String(c.delete_message_days))
    setLogChannel(c.log_channel_id ?? "")
    setLogTouched(false)
    setBase(c)
    setChangedElsewhere(false)
  }

  const draftSig = JSON.stringify([action, dmBefore, appealEnabled, Number(days), logChannel.trim()])
  // Server config changed under us (adjusting state during render, React's
  // recommended alternative to an effect for "reset when a prop changes").
  if (settingsSig(config) !== settingsSig(base)) {
    const draftWasClean = draftSig === settingsSig(base)
    if (draftWasClean || draftSig === settingsSig(config)) {
      // Clean draft, or our own save came back: just follow the server.
      loadFrom(config)
    } else {
      setBase(config)
      setChangedElsewhere(true)
    }
  }

  const daysNum = Number(days)
  const daysValid = days.trim() !== "" && Number.isInteger(daysNum) && daysNum >= 0 && daysNum <= 7
  const logError = discordIdError(logChannel)

  const changes =
    (action !== config.action ? 1 : 0) +
    (dmBefore !== config.dm_before ? 1 : 0) +
    (appealEnabled !== config.appeal_enabled ? 1 : 0) +
    (days !== String(config.delete_message_days) ? 1 : 0) +
    (logChannel.trim() !== (config.log_channel_id ?? "") ? 1 : 0)

  function reset() {
    loadFrom(config)
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!daysValid || logError) return
    update.mutate(
      {
        action,
        dm_before: dmBefore,
        appeal_enabled: appealEnabled,
        delete_message_days: daysNum,
        log_channel_id: logChannel.trim() === "" ? null : logChannel.trim(),
      },
      {
        onSuccess: () => toast({ title: "Settings saved", variant: "success" }),
        onError: (err) => toast({ title: "Could not save settings", description: apiErrorMessage(err), variant: "destructive" }),
      }
    )
  }

  return (
    <form id="hp-settings" onSubmit={onSubmit} className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Settings</CardTitle>
          <CardDescription>Changes apply to the next catch.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-5">
          {!canManage ? <Alert variant="info">Only members with Ban Members can change this.</Alert> : null}
          {changedElsewhere && changes > 0 ? (
            <Alert
              variant="warning"
              title="Changed elsewhere"
              action={
                <Button type="button" variant="secondary" size="sm" onClick={reset}>
                  Load latest
                </Button>
              }
            >
              Someone else saved these settings while you were editing. Your unsaved changes are kept; saving will
              overwrite theirs. Load the latest values to discard yours.
            </Alert>
          ) : null}
          <Field label="Action on catch" hint={ACTION_HELP[action]}>
            {(p) => (
              <Select {...p} value={action} disabled={!canManage} onChange={(e) => setAction(e.target.value as HoneypotAction)}>
                <option value="ban">Ban</option>
                <option value="softban">Softban</option>
                <option value="kick">Kick</option>
              </Select>
            )}
          </Field>

          <div className="flex items-center justify-between gap-4">
            <div>
              <Label htmlFor="hps-dm">DM the user before acting</Label>
              <p className="text-xs text-muted-foreground">Best effort; failures are recorded on the catch.</p>
            </div>
            <Switch id="hps-dm" checked={dmBefore} disabled={!canManage} onCheckedChange={setDmBefore} />
          </div>

          <div className="flex items-center justify-between gap-4">
            <div>
              <Label htmlFor="hps-appeal">Include appeal link in ban DMs</Label>
              <p className="text-xs text-muted-foreground">Only applies to the ban action.</p>
            </div>
            <Switch id="hps-appeal" checked={appealEnabled} disabled={!canManage} onCheckedChange={setAppealEnabled} />
          </div>

          <Field label="Delete message history (days, 0 to 7)" error={daysValid ? undefined : "Enter a whole number from 0 to 7."}>
            {(p) => (
              <Input
                {...p}
                type="number"
                min={0}
                max={7}
                value={days}
                disabled={!canManage}
                onChange={(e) => setDays(e.target.value)}
                className="w-28"
              />
            )}
          </Field>

          <Field
            label="Log channel ID (optional)"
            hint="Leave blank to use the moderation log channel."
            error={logTouched ? logError : null}
          >
            {(p) => (
              <Input
                {...p}
                className="font-mono"
                inputMode="numeric"
                placeholder="Defaults to the moderation log channel"
                value={logChannel}
                disabled={!canManage}
                onBlur={() => setLogTouched(true)}
                onChange={(e) => setLogChannel(e.target.value)}
              />
            )}
          </Field>
        </CardContent>
      </Card>
      {canManage ? (
        <SaveBar
          formId="hp-settings"
          changes={changes}
          onDiscard={reset}
          saving={update.isPending}
          saveLabel="Save settings"
          disabled={!daysValid || bot.offline}
          blockedReason={logError && logTouched ? "Fix the log channel ID first." : bot.reason}
        />
      ) : null}
    </form>
  )
}
