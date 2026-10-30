import { Shuffle } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field } from "@/components/ui/field"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"
import { useScrambleHoneypot, useUpdateHoneypotConfig } from "@/api/honeypot"
import { useBotOffline } from "@/hooks/useBotOffline"
import type { HoneypotConfig, HoneypotConfigUpdate } from "@/types/api"
import { apiErrorMessage, formatDateTime, formatRelative } from "./format"

const INTERVALS = [1, 2, 3, 4, 6, 8, 12, 24, 48, 72, 168]

function intervalLabel(h: number): string {
  if (h >= 24 && h % 24 === 0) return h === 24 ? "1 day" : `${h / 24} days`
  return h === 1 ? "1 hour" : `${h} hours`
}

/**
 * Name scrambling (bot: HoneypotService.scramble_names, migration 029). Saves
 * each change at once (PUT /config) instead of joining the SettingsPanel draft,
 * so a scheduled rename arriving as a `config_changed` event never collides
 * with unsaved settings.
 */
export function ScrambleCard({ config, canManage }: { config: HoneypotConfig; canManage: boolean }) {
  const update = useUpdateHoneypotConfig()
  const scramble = useScrambleHoneypot()
  const { toast } = useToast()
  const bot = useBotOffline()
  const disabled = !canManage || bot.offline || !config.channel_exists
  const title = !canManage ? "Only members with Ban Members can change this" : bot.reason
  const options = INTERVALS.includes(config.scramble_interval_hours)
    ? INTERVALS
    : [...INTERVALS, config.scramble_interval_hours].sort((a, b) => a - b)

  function save(body: HoneypotConfigUpdate, done: string) {
    update.mutate(body, {
      onSuccess: () => toast({ title: done, variant: "success" }),
      onError: (err) => toast({ title: "Could not update", description: apiErrorMessage(err), variant: "destructive" }),
    })
  }

  function scrambleNow() {
    scramble.mutate(undefined, {
      onSuccess: (r) =>
        toast({
          title: "Names scrambled",
          description: [`Channel: ${r.channel.new}`, r.category ? `Category: ${r.category.new}` : null, ...r.warnings]
            .filter(Boolean)
            .join("\n"),
          variant: "success",
        }),
      onError: (err) => toast({ title: "Could not scramble", description: apiErrorMessage(err), variant: "destructive" }),
    })
  }

  const next = !config.scramble_enabled
    ? "Off"
    : config.next_scramble_at
      ? formatDateTime(config.next_scramble_at)
      : "Within 5 minutes"

  return (
    <Card>
      <CardHeader>
        <CardTitle>Name scrambling</CardTitle>
        <CardDescription>
          Renames the honeypot channel and its category to random letters on a timer, so spam tools can't learn to skip
          it by name. The emoji and separators around the name are kept; the warning inside the channel stays the same.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
          <dt className="text-muted-foreground">Channel</dt>
          <dd className="break-words">{config.channel_name ?? "Missing"}</dd>
          <dt className="text-muted-foreground">Category</dt>
          <dd className="break-words">{config.category_name ?? "None"}</dd>
          <dt className="text-muted-foreground">Last scrambled</dt>
          <dd>
            {config.last_scrambled_at
              ? `${formatDateTime(config.last_scrambled_at)} (${formatRelative(config.last_scrambled_at)})`
              : "Never"}
          </dd>
          <dt className="text-muted-foreground">Next</dt>
          <dd>{next}</dd>
        </dl>

        <div className="flex items-center justify-between gap-4">
          <div>
            <Label htmlFor="hps-scramble">Scramble automatically</Label>
            <p className="text-xs text-muted-foreground">The bot checks every 5 minutes; restarts don't reset the timer.</p>
          </div>
          <Switch
            id="hps-scramble"
            checked={config.scramble_enabled}
            disabled={disabled || update.isPending}
            title={title}
            onCheckedChange={(on) => save({ scramble_enabled: on }, on ? "Automatic scrambling on" : "Automatic scrambling off")}
          />
        </div>

        <Field label="Scramble every">
          {(p) => (
            <Select
              {...p}
              className="w-40"
              value={String(config.scramble_interval_hours)}
              disabled={disabled || update.isPending}
              title={title}
              onChange={(e) => save({ scramble_interval_hours: Number(e.target.value) }, "Interval saved")}
            >
              {options.map((h) => (
                <option key={h} value={h}>
                  {intervalLabel(h)}
                </option>
              ))}
            </Select>
          )}
        </Field>

        <div className="flex flex-wrap items-center gap-3">
          <Button
            type="button"
            variant="secondary"
            onClick={scrambleNow}
            disabled={disabled || scramble.isPending}
            aria-busy={scramble.isPending || undefined}
            title={title}
          >
            <Shuffle aria-hidden />
            {scramble.isPending ? "Scrambling..." : "Scramble now"}
          </Button>
          <p className="text-xs text-muted-foreground">Discord allows 2 renames per 10 minutes.</p>
        </div>
      </CardContent>
    </Card>
  )
}
