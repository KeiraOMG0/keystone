import { useState } from "react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"
import { useCreateRejoinInvite, useUpdateHoneypotConfig } from "@/api/honeypot"
import { useGuildChannels } from "@/api/reactionRoles"
import { useBotOffline } from "@/hooks/useBotOffline"
import { discordIdError } from "@/lib/format"
import type { HoneypotConfig } from "@/types/api"
import { apiErrorMessage } from "./format"

/**
 * Rejoin link (migration 030): the invite shown to people whose appeal was approved
 * (appeal page + approval DM), optionally also put in kick/softban DMs. Saves each
 * change at once, like ScrambleCard. The bot checks a pasted link is a permanent
 * invite to this server, or creates one itself (needs Create Invite).
 */
export function RejoinCard({ config, canManage }: { config: HoneypotConfig; canManage: boolean }) {
  const update = useUpdateHoneypotConfig()
  const create = useCreateRejoinInvite()
  const channels = useGuildChannels() // needs manage_reaction_roles; falls back to typing an ID
  const { toast } = useToast()
  const bot = useBotOffline()
  const [url, setUrl] = useState("")
  const [channelId, setChannelId] = useState("")
  const disabled = !canManage || bot.offline
  const title = !canManage ? "Only members with Ban Members can change this" : bot.reason
  const idError = channels.data ? null : discordIdError(channelId)

  function fail(what: string) {
    return (err: unknown) => toast({ title: what, description: apiErrorMessage(err), variant: "destructive" })
  }

  function saveUrl(value: string | null) {
    update.mutate(
      { rejoin_invite_url: value },
      {
        onSuccess: () => {
          setUrl("")
          toast({ title: value ? "Rejoin link saved" : "Rejoin link removed", variant: "success" })
        },
        onError: fail("Could not save the link"),
      }
    )
  }

  function createInvite() {
    create.mutate(channelId.trim(), {
      onSuccess: () => toast({ title: "Invite created and saved", variant: "success" }),
      onError: fail("Could not create the invite"),
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Rejoin link</CardTitle>
        <CardDescription>
          Shown to people whose appeal was approved, on the appeal page and in the approval DM, next to the one-click
          "Rejoin server" button. Must be a permanent invite to this server.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="text-sm">
          <span className="text-muted-foreground">Current link: </span>
          {config.rejoin_invite_url ? (
            <>
              <a
                href={config.rejoin_invite_url}
                target="_blank"
                rel="noreferrer noopener"
                className="break-all text-primary underline underline-offset-4"
              >
                {config.rejoin_invite_url}
              </a>{" "}
              <button
                type="button"
                className="ml-2 rounded-sm text-sm text-danger hover:underline disabled:opacity-50"
                disabled={disabled || update.isPending}
                title={title}
                onClick={() => saveUrl(null)}
              >
                Remove
              </button>
            </>
          ) : (
            <span>None</span>
          )}
        </div>

        <Field label="Paste an invite" hint="For example https://discord.gg/abc123 (never expires, unlimited uses).">
          {(p) => (
            <div className="flex flex-wrap gap-2">
              <Input
                {...p}
                className="min-w-0 flex-1 basis-56"
                placeholder="https://discord.gg/..."
                value={url}
                disabled={disabled}
                onChange={(e) => setUrl(e.target.value)}
              />
              <Button
                type="button"
                variant="secondary"
                disabled={disabled || update.isPending || url.trim() === ""}
                title={title}
                onClick={() => saveUrl(url.trim())}
              >
                Save link
              </Button>
            </div>
          )}
        </Field>

        <Field
          label="Or let the bot create one"
          hint="A permanent invite to this channel (the bot needs Create Invite there)."
          error={!channels.data && channelId.length > 0 ? idError : null}
        >
          {(p) => (
            <div className="flex flex-wrap gap-2">
              {channels.data ? (
                <Select
                  {...p}
                  className="min-w-0 flex-1 basis-56"
                  value={channelId}
                  disabled={disabled}
                  onChange={(e) => setChannelId(e.target.value)}
                >
                  <option value="">Choose a channel...</option>
                  {channels.data.map((c) => (
                    <option key={c.id} value={c.id}>
                      #{c.name}
                    </option>
                  ))}
                </Select>
              ) : (
                <Input
                  {...p}
                  className="min-w-0 flex-1 basis-56 font-mono"
                  inputMode="numeric"
                  placeholder="Channel ID"
                  value={channelId}
                  disabled={disabled}
                  onChange={(e) => setChannelId(e.target.value.trim())}
                />
              )}
              <Button
                type="button"
                variant="secondary"
                disabled={disabled || create.isPending || channelId === "" || Boolean(idError)}
                aria-busy={create.isPending || undefined}
                title={title}
                onClick={createInvite}
              >
                {create.isPending ? "Creating..." : "Create invite"}
              </Button>
            </div>
          )}
        </Field>

        <div className="flex items-center justify-between gap-4">
          <div>
            <Label htmlFor="hps-invite-dm">Also put it in kick and softban DMs</Label>
            <p className="text-xs text-muted-foreground">
              They can rejoin straight away. If the account is still hacked, whoever controls it gets the link too (the
              trap would catch them again). Never added to ban DMs.
            </p>
          </div>
          <Switch
            id="hps-invite-dm"
            checked={config.invite_in_removal_dm}
            disabled={disabled || update.isPending || !config.rejoin_invite_url}
            title={!config.rejoin_invite_url ? "Set a rejoin link first" : title}
            onCheckedChange={(on) =>
              update.mutate(
                { invite_in_removal_dm: on },
                {
                  onSuccess: () => toast({ title: on ? "Link added to kick/softban DMs" : "Link removed from DMs", variant: "success" }),
                  onError: fail("Could not update"),
                }
              )
            }
          />
        </div>
      </CardContent>
    </Card>
  )
}
