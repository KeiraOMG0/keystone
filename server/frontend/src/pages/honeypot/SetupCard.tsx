import { useState, type FormEvent } from "react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Field } from "@/components/ui/field"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Alert } from "@/components/ui/alert"
import { useToast } from "@/components/ui/toast"
import { useSetupHoneypot } from "@/api/honeypot"
import { useBotOffline } from "@/hooks/useBotOffline"
import type { HoneypotAction } from "@/types/api"
import { ACTION_HELP, apiErrorMessage } from "./format"

const STEPS = [
  ["Create the trap", "The bot adds a public category and channel at the top of your channel list that everyone can see and type in."],
  ["Bots walk into it", "Hacked accounts and spam bots post in every channel they find. Staff and bots are ignored."],
  ["It is dealt with automatically", "The account gets your chosen action (ban, softban or kick), the catch is logged here live, and banned members can appeal."],
]

/** Shown when no honeypot exists yet: creates category + channel via POST /setup. */
export function SetupCard({ canManage }: { canManage: boolean }) {
  const setup = useSetupHoneypot()
  const { toast } = useToast()
  const bot = useBotOffline()
  const [categoryName, setCategoryName] = useState("Do Not Type")
  const [channelName, setChannelName] = useState("do-not-type")
  const [action, setAction] = useState<HoneypotAction>("ban")
  const [dmBefore, setDmBefore] = useState(true)
  const [appealEnabled, setAppealEnabled] = useState(true)

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    setup.mutate(
      {
        category_name: categoryName.trim() || undefined,
        channel_name: channelName.trim() || undefined,
        position_top: true,
        action,
        dm_before: dmBefore,
        appeal_enabled: appealEnabled && action === "ban",
      },
      {
        onSuccess: (data) => {
          toast({
            title: "Honeypot created",
            description: data.warnings.length ? data.warnings.join(" ") : "The trap channel is live.",
            variant: "success",
          })
        },
        onError: (err) => toast({ title: "Could not create honeypot", description: apiErrorMessage(err), variant: "destructive" }),
      }
    )
  }

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
      <form onSubmit={onSubmit}>
        <Card>
          <CardHeader>
            <CardTitle>Create honeypot</CardTitle>
            <CardDescription>
              Adds a public channel everyone can see and type in. Hacked accounts and spam bots post in every channel
              they find, so anyone who posts here (other than staff and bots) is punished automatically.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            {!canManage ? (
              <Alert variant="info">Only members with Ban Members can create or change the honeypot. You can read this page but not change it.</Alert>
            ) : null}
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Category name">
                {(p) => <Input {...p} value={categoryName} maxLength={100} disabled={!canManage} onChange={(e) => setCategoryName(e.target.value)} />}
              </Field>
              <Field label="Channel name">
                {(p) => <Input {...p} value={channelName} maxLength={100} disabled={!canManage} onChange={(e) => setChannelName(e.target.value)} />}
              </Field>
            </div>

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
                <Label htmlFor="hp-dm">DM the user first</Label>
                <p className="text-xs text-muted-foreground">Explains what happened and how to secure a hacked account.</p>
              </div>
              <Switch id="hp-dm" checked={dmBefore} disabled={!canManage} onCheckedChange={setDmBefore} />
            </div>

            <div className="flex items-center justify-between gap-4">
              <div>
                <Label htmlFor="hp-appeal">Allow appeals</Label>
                <p className="text-xs text-muted-foreground">
                  Bans include a link to a public appeal form. {action !== "ban" ? "Only available for the Ban action." : ""}
                </p>
              </div>
              <Switch id="hp-appeal" checked={appealEnabled && action === "ban"} onCheckedChange={setAppealEnabled} disabled={!canManage || action !== "ban"} />
            </div>
          </CardContent>
          <CardFooter className="justify-between gap-3">
            <p className="text-xs text-muted-foreground">Needs the Ban Members permission</p>
            <Button type="submit" disabled={!canManage || bot.offline} title={bot.reason} loading={setup.isPending}>
              {setup.isPending ? "Creating" : "Create honeypot"}
            </Button>
          </CardFooter>
        </Card>
      </form>

      <Card className="h-fit">
        <CardHeader>
          <CardTitle>How it works</CardTitle>
        </CardHeader>
        <CardContent>
          <ol className="flex flex-col gap-4">
            {STEPS.map(([title, body], i) => (
              <li key={title} className="flex gap-3">
                <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary-soft text-xs font-semibold text-primary-soft-foreground">
                  {i + 1}
                </span>
                <div>
                  <p className="text-base font-medium text-foreground">{title}</p>
                  <p className="text-sm text-muted-foreground">{body}</p>
                </div>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>
    </div>
  )
}
