import { useEffect, useState } from "react"
import { ExternalLink, FlaskConical, PartyPopper, UserPlus } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Label } from "@/components/ui/label"
import { Field } from "@/components/ui/field"
import { Alert } from "@/components/ui/alert"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { useBotOffline } from "@/hooks/useBotOffline"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"
import { describeApiError } from "@/lib/apiError"
import { useAutomationOptions, useGreetings, useTestGreeting, useUpdateGreeting, type GreetingState } from "@/api/automation"
import { NotAllowed, TabState, channelLabel } from "./shared"

const STAFF = "staff_base"

/** /welcome and /boost setchannel|toggle|status (staff only, require_staff). */
export function GreetingsTab({ allowed }: { allowed: boolean }) {
  const query = useGreetings(allowed)
  const options = useAutomationOptions(allowed)
  if (!allowed) return <NotAllowed permission={STAFF} />
  return (
    <TabState query={query} permission={STAFF} what="welcome and boost settings">
      <div className="grid gap-6 md:grid-cols-2">
        {query.data ? (
          <>
            <GreetingCard kind="welcome" state={query.data.welcome} channels={options.data?.channels ?? []} />
            <GreetingCard kind="boost" state={query.data.boost} channels={options.data?.channels ?? []} />
          </>
        ) : null}
      </div>
    </TabState>
  )
}

const COPY = {
  welcome: {
    title: "Welcome message",
    icon: UserPlus,
    what: "when someone joins",
    template: "welcome",
    command: "/welcome",
  },
  boost: {
    title: "Boost announcement",
    icon: PartyPopper,
    what: "when someone boosts the server",
    template: "boost",
    command: "/boost",
  },
} as const

function GreetingCard({
  kind,
  state,
  channels,
}: {
  kind: "welcome" | "boost"
  state: GreetingState
  channels: { id: string; name: string; position: number }[]
}) {
  const copy = COPY[kind]
  const update = useUpdateGreeting()
  const test = useTestGreeting()
  const { offline, reason } = useBotOffline()
  const { toast } = useToast()
  const [channel, setChannel] = useState(state.channel_id ?? "")
  const [confirmTest, setConfirmTest] = useState(false)
  useEffect(() => setChannel(state.channel_id ?? ""), [state.channel_id])
  const dirty = channel !== (state.channel_id ?? "") && channel !== ""
  useUnsavedChangesGuard(dirty)
  const Icon = copy.icon
  const sorted = [...channels].sort((a, b) => a.position - b.position)

  if (!state.available) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>{copy.title}</CardTitle>
        </CardHeader>
        <CardContent>
          <Alert variant="warning" title="Not available">
            The {kind} cog is not loaded in the bot right now.
          </Alert>
        </CardContent>
      </Card>
    )
  }

  const run = (input: { channel_id?: string; enabled?: boolean }, done: string) =>
    update.mutate(
      { kind, ...input },
      {
        onSuccess: () => toast({ title: done, variant: "success" }),
        onError: (e) => toast({ title: "Could not save", description: describeApiError(e), variant: "destructive" }),
      }
    )

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Icon className="size-4" aria-hidden /> {copy.title}
          {state.configured && state.enabled ? <Badge variant="success" dot>On</Badge> : <Badge variant="outline">Off</Badge>}
        </CardTitle>
        <CardDescription>
          Posted {copy.what}. Uses the template named <span className="font-mono">{copy.template}</span> if it exists, otherwise
          the built-in embed. Same as <span className="font-mono">{copy.command}</span>.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex items-center gap-2">
          <Switch
            id={`${kind}-enabled`}
            checked={state.enabled}
            disabled={update.isPending}
            onCheckedChange={(v) => run({ enabled: v }, v ? `${copy.title} on` : `${copy.title} off`)}
          />
          <Label htmlFor={`${kind}-enabled`}>Enabled</Label>
          {state.enabled && !state.configured ? <span className="text-xs text-warning">No channel set, nothing is posted.</span> : null}
        </div>
        <Field
          label="Channel"
          hint={`Current: ${state.configured ? channelLabel(state.channel_name, state.channel_id) : "none"}. Saving a channel also turns it on.`}
        >
          {(p) => (
            <Select {...p} value={channel} onChange={(e) => setChannel(e.target.value)}>
              <option value="">Pick a channel</option>
              {sorted.map((c) => (
                <option key={c.id} value={c.id}>
                  #{c.name}
                </option>
              ))}
            </Select>
          )}
        </Field>
        <div className="flex flex-wrap gap-2">
          <Button disabled={!dirty} loading={update.isPending} onClick={() => run({ channel_id: channel }, "Channel saved")}>
            Save channel
          </Button>
          <Button
            variant="secondary"
            disabled={!state.configured || offline || test.isPending}
            title={reason ?? (!state.configured ? "Set a channel first" : undefined)}
            onClick={() => setConfirmTest(true)}
          >
            <FlaskConical /> Send test
          </Button>
          {test.data?.jump_url ? (
            <Button asChild variant="link" size="sm">
              <a href={test.data.jump_url} target="_blank" rel="noreferrer">
                <ExternalLink /> Open test message
              </a>
            </Button>
          ) : null}
        </div>
      </CardContent>
      <ConfirmDialog
        open={confirmTest}
        onOpenChange={setConfirmTest}
        destructive={false}
        title={`Send a test ${kind} message?`}
        description={
          <>
            This posts the real {kind} message for <strong>you</strong> in{" "}
            <strong>{channelLabel(state.channel_name, state.channel_id)}</strong> (it pings you). Everyone who can see that channel
            will see it.
          </>
        }
        confirmLabel="Post test message"
        confirmIcon={<FlaskConical />}
        onConfirm={() =>
          test.mutate(kind, {
            onSuccess: (r) => toast({ title: "Test message sent", description: `Posted in #${r.channel_name}.`, variant: "success" }),
            onError: (e) => toast({ title: "Test failed", description: describeApiError(e), variant: "destructive" }),
          })
        }
      />
    </Card>
  )
}
