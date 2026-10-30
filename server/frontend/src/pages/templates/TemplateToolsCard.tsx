import { useState } from "react"
import { Download, Eye, History, RotateCcw } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmbedPreview } from "@/components/embed/EmbedPreview"
import { DiscordMessage } from "@/components/embed/DiscordMessage"
import { ResolvedUser } from "@/components/ResolvedUser"
import { describeApiError } from "@/lib/apiError"
import { formatDateTime } from "@/lib/format"
import { exportTemplate, useBotPreview, useRollbackTemplate, useTemplateVersions, type TemplateVersion } from "@/api/templates"

/**
 * Template editor side card: parity with /template preview, export, history
 * and rollback (manage_templates, same as the editor). Rollback replaces the
 * saved template, so it is refused while the editor has unsaved edits; after
 * it the editor reloads from the server and managed messages re-sync.
 */
export function TemplateToolsCard({
  templateId,
  name,
  dirty,
  onRolledBack,
}: {
  templateId: number
  name: string
  dirty: boolean
  onRolledBack: () => void | Promise<void>
}) {
  const versions = useTemplateVersions(templateId)
  const rollback = useRollbackTemplate()
  const preview = useBotPreview()
  const { toast } = useToast()
  const [target, setTarget] = useState<TemplateVersion | null>(null)
  const [exporting, setExporting] = useState(false)

  async function handleExport() {
    setExporting(true)
    try {
      const data = await exportTemplate(templateId)
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })
      const url = URL.createObjectURL(blob)
      const a = document.createElement("a")
      a.href = url
      a.download = `${data.name || name}.json`
      a.click()
      URL.revokeObjectURL(url)
      toast({ title: "Template exported", description: "Import it on the Templates page or with /template import.", variant: "success" })
    } catch (error) {
      toast({ title: "Export failed", description: describeApiError(error), variant: "destructive" })
    } finally {
      setExporting(false)
    }
  }

  const list = versions.data?.versions ?? []

  return (
    <Card>
      <CardHeader>
        <CardTitle>Preview, export and history</CardTitle>
        <CardDescription>
          The bot preview renders the last saved version exactly like <span className="font-mono">/template preview</span> (server
          placeholders filled, member placeholders blank).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-2">
          <Button
            variant="secondary"
            size="sm"
            loading={preview.isPending}
            onClick={() =>
              preview.mutate(templateId, {
                onError: (e) => toast({ title: "Preview failed", description: describeApiError(e), variant: "destructive" }),
              })
            }
          >
            <Eye /> Bot preview
          </Button>
          <Button variant="secondary" size="sm" loading={exporting} onClick={handleExport}>
            <Download /> Export JSON
          </Button>
        </div>

        {preview.data ? (
          <DiscordMessage text={preview.data.content ?? undefined}>
            {preview.data.embeds.length === 0 && !preview.data.content ? (
              <p className="text-sm text-muted-foreground">This template renders to nothing.</p>
            ) : null}
            {preview.data.embeds.map((e, i) => (
              <EmbedPreview
                key={i}
                title={e.title}
                description={e.description}
                color={e.color}
                url={e.url}
                footer={e.footer}
                thumbnail_url={e.thumbnail?.url}
                image_url={e.image?.url}
                author={e.author}
                fields={(e.fields ?? []).map((f) => ({ name: f.name, value: f.value, inline: !!f.inline }))}
                timestamp_mode={e.timestamp ? "now" : "none"}
              />
            ))}
          </DiscordMessage>
        ) : null}

        <div className="flex flex-col gap-2">
          <p className="flex items-center gap-1.5 text-sm font-medium text-foreground">
            <History className="size-4" aria-hidden /> Version history
          </p>
          {versions.isPending ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : versions.isError ? (
            <p className="text-sm text-danger">{describeApiError(versions.error, "Could not load history.")}</p>
          ) : list.length === 0 ? (
            <p className="text-sm text-muted-foreground">No snapshots yet. One is saved before every embed save (last 20 kept).</p>
          ) : (
            <ul className="flex max-h-72 flex-col divide-y divide-border overflow-y-auto rounded-lg border border-border">
              {list.map((v) => (
                <li key={v.version_number} className="flex items-center gap-3 px-3 py-2 text-sm">
                  <span className="font-mono text-xs text-muted-foreground">v{v.version_number}</span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-foreground">{v.change_summary || "Snapshot"}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {v.created_at ? formatDateTime(v.created_at.replace(" ", "T") + (v.created_at.includes("Z") ? "" : "Z")) : ""}
                      {v.changed_by ? (
                        <>
                          {" · "}
                          <ResolvedUser userId={v.changed_by} />
                        </>
                      ) : null}
                    </div>
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={dirty || rollback.isPending}
                    title={dirty ? "Save or discard your edits first" : `Roll back to v${v.version_number}`}
                    onClick={() => setTarget(v)}
                  >
                    <RotateCcw /> Restore
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>

      <ConfirmDialog
        open={target !== null}
        onOpenChange={(o) => !o && setTarget(null)}
        title={`Roll "${name}" back to v${target?.version_number ?? ""}?`}
        description="Replaces the saved message text, description, category and embeds with that snapshot (same as /template rollback). Messages posted from this template update too. The current version stays in history."
        confirmLabel="Roll back"
        confirmIcon={<RotateCcw />}
        onConfirm={() =>
          target &&
          rollback.mutate(
            { templateId, version: target.version_number },
            {
              onSuccess: async () => {
                await onRolledBack()
                toast({ title: `Rolled back to v${target.version_number}`, variant: "success" })
              },
              onError: (e) => toast({ title: "Rollback failed", description: describeApiError(e), variant: "destructive" }),
            }
          )
        }
      />
    </Card>
  )
}
