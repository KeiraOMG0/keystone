import { useState } from "react"
import { Gavel, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { useToast } from "@/components/ui/toast"
import { ApiError } from "@/api/client"
import { useAppealTestCases, useClearAppealTestCases, useCreateAppealTestCase } from "@/api/debug"
import { useBotOffline } from "@/hooks/useBotOffline"
import { discordIdError } from "@/lib/format"
import type { AppealTestState } from "@/types/api"

const STATES: { value: AppealTestState; label: string; next: string }[] = [
  {
    value: "eligible",
    label: "Can appeal (test ban only)",
    next: "As the alt: open /appeal, Sign in with Discord, submit an appeal. It appears in Honeypot > Appeals marked Test.",
  },
  {
    value: "pending",
    label: "Appeal pending review",
    next: "Approve or deny it in Honeypot > Appeals. The alt then sees the result on /appeal after signing in.",
  },
  {
    value: "approved",
    label: "Appeal approved",
    next: "As the alt (not in the server): open /appeal, sign in, click Rejoin server. It should be added back.",
  },
]

function message(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { error?: string; message?: string } | null
    return body?.message ?? body?.error?.replace(/_/g, " ") ?? `Request failed (${err.status})`
  }
  return "Something went wrong."
}

/**
 * Honeypot appeal-flow test cases (owner-only Debug page, migration 031). Unlike the
 * test actions, this WRITES honeypot rows (tagged Test, kept out of stats and the catch
 * feed) so the real sign-in / review / rejoin can be tried with an alt. Nobody is
 * banned and nothing is posted; "Delete test data" removes every test row.
 */
export function AppealTestCard() {
  const cases = useAppealTestCases(true)
  const create = useCreateAppealTestCase()
  const clear = useClearAppealTestCases()
  const { toast } = useToast()
  const bot = useBotOffline()
  const [userId, setUserId] = useState("")
  const [state, setState] = useState<AppealTestState>("eligible")
  const idError = discordIdError(userId)
  const chosen = STATES.find((s) => s.value === state)!

  function submit() {
    create.mutate(
      { user_id: userId.trim(), state },
      {
        onSuccess: (r) =>
          toast({ title: `Test case created for ${r.username ?? userId}`, description: chosen.next, variant: "success" }),
        onError: (err) => toast({ title: "Could not create the test case", description: message(err), variant: "destructive" }),
      }
    )
  }

  const list = cases.data?.cases ?? []
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Gavel className="size-4" aria-hidden /> Appeal flow test
        </CardTitle>
        <CardDescription>
          Gives an alt account a TEST honeypot ban on record (nobody is actually banned or messaged) so you can try the
          real appeal sign-in, review and one-click rejoin. Test rows are labelled Test and kept out of the stats and the
          catch feed. Use a separate browser profile logged in to Discord as the alt.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
          <Field label="Alt account's User ID" error={userId.length > 0 ? idError : null}>
            {(p) => (
              <Input
                {...p}
                className="font-mono"
                inputMode="numeric"
                placeholder="123456789012345678"
                value={userId}
                onChange={(e) => setUserId(e.target.value.trim())}
              />
            )}
          </Field>
          <Field label="Start at">
            {(p) => (
              <Select {...p} value={state} onChange={(e) => setState(e.target.value as AppealTestState)}>
                {STATES.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </Select>
            )}
          </Field>
          <Button
            type="button"
            onClick={submit}
            disabled={bot.offline || create.isPending || userId === "" || Boolean(idError)}
            title={bot.reason}
          >
            {create.isPending ? "Creating..." : "Create test case"}
          </Button>
        </div>
        <p className="text-sm text-muted-foreground">Next: {chosen.next}</p>

        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-medium">Current test cases</h3>
          <Button
            type="button"
            variant="secondary"
            size="sm"
            disabled={bot.offline || clear.isPending || list.length === 0}
            onClick={() =>
              clear.mutate(undefined, {
                onSuccess: (r) =>
                  toast({ title: `Deleted ${r.catches} test catch(es) and ${r.appeals} test appeal(s)`, variant: "success" }),
                onError: (err) => toast({ title: "Could not delete", description: message(err), variant: "destructive" }),
              })
            }
          >
            <Trash2 aria-hidden /> Delete test data
          </Button>
        </div>
        {cases.isPending ? (
          <p className="text-sm text-muted-foreground">Loading...</p>
        ) : cases.isError ? (
          <p className="text-sm text-danger">Could not load test cases: {message(cases.error)}</p>
        ) : list.length === 0 ? (
          <p className="text-sm text-muted-foreground">None.</p>
        ) : (
          <ul className="flex flex-col divide-y divide-border rounded-lg border border-border text-sm">
            {list.map((c) => (
              <li key={c.catch_id} className="flex flex-wrap items-center gap-2 px-3 py-2">
                <span className="font-medium">{c.username ?? "unknown"}</span>
                <span className="font-mono text-xs text-muted-foreground">{c.user_id}</span>
                <Badge variant="outline">{c.status ?? "can appeal"}</Badge>
                {c.rejoined_at ? <Badge variant="success">rejoined</Badge> : null}
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-muted-foreground">
          Deleting test data never touches Discord: an alt that rejoined stays in the server.
        </p>
      </CardContent>
    </Card>
  )
}
