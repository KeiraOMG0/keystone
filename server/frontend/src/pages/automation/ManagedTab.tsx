import { useState } from "react"
import { ExternalLink, Link2Off, MessageSquare, RefreshCw } from "lucide-react"
import { Link } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState } from "@/components/patterns/states"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import { formatDateTime, formatRelativeTime } from "@/lib/format"
import { useManagedMessages, useSyncManaged, useUntrackManaged, type ManagedMessage } from "@/api/automation"
import { NotAllowed, TabState, channelLabel } from "./shared"

/**
 * /managed list|untrack|sync (manage_templates). New managed messages are
 * posted from a template's editor ("Send to channel" = /template post).
 */
export function ManagedTab({ allowed }: { allowed: boolean }) {
  const query = useManagedMessages(allowed)
  const untrack = useUntrackManaged()
  const sync = useSyncManaged()
  const { offline, reason } = useBotOffline()
  const { toast } = useToast()
  const [toUntrack, setToUntrack] = useState<ManagedMessage | null>(null)

  if (!allowed) return <NotAllowed permission="manage_templates" />
  const messages = query.data?.messages ?? []

  return (
    <TabState query={query} permission="manage_templates" what="managed messages">
      <Card className="gap-0 py-0">
        <CardHeader className="py-4">
          <CardTitle>Managed messages</CardTitle>
          <CardDescription>
            Messages posted from a template that the bot edits whenever the template changes (same as{" "}
            <span className="font-mono">/managed</span>). Post a new one from a template's editor.
          </CardDescription>
        </CardHeader>
        {messages.length === 0 ? (
          <EmptyState
            icon={MessageSquare}
            title="No managed messages"
            action={
              <Button asChild variant="secondary" size="sm">
                <Link to="/dashboard/templates">Open templates</Link>
              </Button>
            }
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Template</TableHead>
                <TableHead>Channel</TableHead>
                <TableHead>Last synced</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {messages.map((m) => (
                <TableRow key={m.message_id}>
                  <TableCell className="font-mono text-xs">{m.template_name ?? "?"}</TableCell>
                  <TableCell>
                    <a href={m.jump_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-primary hover:underline">
                      {channelLabel(m.channel_name, m.channel_id)} <ExternalLink className="size-3" aria-hidden />
                    </a>
                  </TableCell>
                  <TableCell className="text-muted-foreground" title={m.last_synced_at ? formatDateTime(m.last_synced_at) : undefined}>
                    {m.last_synced_at ? formatRelativeTime(m.last_synced_at) : "Never"}
                  </TableCell>
                  <TableCell>{m.sync_failed ? <Badge variant="destructive">Sync failed</Badge> : <Badge variant="success" dot>OK</Badge>}</TableCell>
                  <TableCell className="text-right">
                    <div className="flex justify-end gap-1">
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={offline || sync.isPending}
                        title={reason ?? "Re-render every managed message of this template now"}
                        onClick={() =>
                          sync.mutate(m.template_id, {
                            onSuccess: (r) => toast({ title: `Synced "${r.template_name}"`, variant: "success" }),
                            onError: (e) => toast({ title: "Sync failed", description: describeApiError(e), variant: "destructive" }),
                          })
                        }
                      >
                        <RefreshCw /> Sync
                      </Button>
                      <Button size="sm" variant="ghost" className="text-danger" onClick={() => setToUntrack(m)}>
                        <Link2Off /> Untrack
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>

      <ConfirmDialog
        open={toUntrack !== null}
        onOpenChange={(o) => !o && setToUntrack(null)}
        title="Stop tracking this message?"
        description="The message stays in Discord but no longer updates when the template changes."
        confirmLabel="Untrack"
        confirmIcon={<Link2Off />}
        onConfirm={() =>
          toUntrack &&
          untrack.mutate(toUntrack.message_id, {
            onSuccess: () => toast({ title: "Message untracked", variant: "success" }),
            onError: (e) => toast({ title: "Could not untrack", description: describeApiError(e), variant: "destructive" }),
          })
        }
      />
    </TabState>
  )
}
