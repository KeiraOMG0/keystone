import { useEffect, useMemo, useRef, useState } from "react"
import { Link, useParams } from "react-router-dom"
import { ArrowDown, ArrowLeft, ArrowUp, ChevronDown, ChevronRight, Plus, Send, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Skeleton } from "@/components/ui/skeleton"
import { Select } from "@/components/ui/select"
import { Field, CharCounter } from "@/components/ui/field"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { useToast } from "@/components/ui/toast"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { ErrorState } from "@/components/patterns/states"
import { useBreadcrumb } from "@/components/layout/breadcrumbs"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { EmbedPreview } from "@/components/embed/EmbedPreview"
import { DiscordMessage } from "@/components/embed/DiscordMessage"
import { EmbedColorPicker } from "@/components/embed/EmbedColorPicker"
import {
  usePostTemplate,
  useReplaceTemplateEmbeds,
  useTemplate,
  useTemplateChannels,
  useUpdateTemplate,
} from "@/api/templates"
import { ApiError } from "@/api/client"
import { describeApiError } from "@/lib/apiError"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"
import { useBotOffline } from "@/hooks/useBotOffline"
import type { EmbedField, ForbiddenError, TemplateEmbed } from "@/types/api"
import { TemplateToolsCard } from "./TemplateToolsCard"
import { PlaceholdersPanel } from "./PlaceholdersPanel"
import { resolvePlaceholders, usePlaceholders } from "@/api/placeholders"

function emptyEmbed(): TemplateEmbed {
  return {
    title: "",
    description: "",
    color: 0x5865f2,
    url: "",
    footer_text: "",
    footer_icon_url: "",
    thumbnail_url: "",
    image_url: "",
    author_name: "",
    author_icon_url: "",
    author_url: "",
    timestamp_mode: "none",
    fields: [],
  }
}

function nullifyBlank(value: string | null | undefined): string | null {
  if (!value || value.trim().length === 0) return null
  return value
}

interface Draft {
  name: string
  description: string
  category: string
  content: string
  embeds: TemplateEmbed[]
}

/**
 * PROTECTED. Edit one template: details (name / category / description /
 * message text) plus an embed array with a live Discord-style preview.
 *
 * Metadata and embeds are separate backend calls (PATCH .../ and PUT
 * .../embeds, matching TemplateService.replace_embeds' delete-and-reinsert
 * contract) fired together from one "Save changes" so a partial save can never
 * look like a full save. Unsaved edits are guarded by the shared
 * useUnsavedChangesGuard (links, palette, hotkeys, Back/Forward, reload, tab
 * close).
 */
export default function TemplateEditorPage() {
  const { templateId: templateIdParam } = useParams<{ templateId: string }>()
  const templateId = templateIdParam ? Number(templateIdParam) : null
  const templateQuery = useTemplate(templateId)
  const updateTemplate = useUpdateTemplate()
  const replaceEmbeds = useReplaceTemplateEmbeds()
  const channelsQuery = useTemplateChannels()
  const postTemplate = usePostTemplate()
  const { toast } = useToast()
  const bot = useBotOffline()

  const [selectedChannelId, setSelectedChannelId] = useState("")

  const [draft, setDraft] = useState<Draft>({ name: "", description: "", category: "", content: "", embeds: [] })
  const [saved, setSaved] = useState<string>("")
  const [loaded, setLoaded] = useState(false)
  const [expandedEmbeds, setExpandedEmbeds] = useState<Set<number>>(new Set())
  const [isSaving, setIsSaving] = useState(false)
  const contentRef = useRef<HTMLTextAreaElement>(null)
  // Last text field the user typed in: where a clicked placeholder is inserted.
  const lastFieldRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)
  const placeholdersQuery = usePlaceholders()
  const catalogue = placeholdersQuery.data?.placeholders

  const { name, description, category, content, embeds } = draft
  const dirty = loaded && JSON.stringify(draft) !== saved
  useUnsavedChangesGuard(dirty, "You have unsaved changes to this template. If you leave now they will be lost.")
  useBreadcrumb(loaded ? name || "Template" : null)

  useEffect(() => {
    if (templateQuery.data && !loaded) {
      const t = templateQuery.data
      const d: Draft = {
        name: t.name,
        description: t.description ?? "",
        category: t.category ?? "",
        content: t.content ?? "",
        embeds: t.embeds.length > 0 ? t.embeds : [],
      }
      setDraft(d)
      setSaved(JSON.stringify(d))
      setExpandedEmbeds(t.embeds.length === 1 ? new Set([0]) : new Set())
      setLoaded(true)
    }
  }, [templateQuery.data, loaded])

  const forbidden =
    templateQuery.isError &&
    templateQuery.error instanceof ApiError &&
    templateQuery.error.status === 403 &&
    isForbiddenError(templateQuery.error.body)
      ? (templateQuery.error.body as ForbiddenError)
      : null

  const previewEmbeds = useMemo(() => {
    const r = (t: string | null | undefined) => resolvePlaceholders(t, catalogue)
    return embeds.map((e) => ({
      ...e,
      title: r(e.title),
      description: r(e.description),
      url: r(e.url),
      footer_text: r(e.footer_text),
      footer_icon_url: r(e.footer_icon_url),
      thumbnail_url: r(e.thumbnail_url),
      image_url: r(e.image_url),
      author_name: r(e.author_name),
      author_icon_url: r(e.author_icon_url),
      author_url: r(e.author_url),
      fields: e.fields.map((f) => ({ ...f, name: r(f.name), value: r(f.value) })),
    }))
  }, [embeds, catalogue])

  if (forbidden) {
    return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />
  }

  if (templateId === null || Number.isNaN(templateId)) {
    return (
      <PageContainer width="narrow">
        <Card>
          <ErrorState title="Invalid template ID" message="That link does not point to a template." />
        </Card>
      </PageContainer>
    )
  }

  const patch = (p: Partial<Draft>) => setDraft((d) => ({ ...d, ...p }))
  const setEmbeds = (fn: (prev: TemplateEmbed[]) => TemplateEmbed[]) => setDraft((d) => ({ ...d, embeds: fn(d.embeds) }))

  function toggleEmbedExpanded(index: number) {
    setExpandedEmbeds((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  function updateEmbed(index: number, p: Partial<TemplateEmbed>) {
    setEmbeds((prev) => prev.map((e, i) => (i === index ? { ...e, ...p } : e)))
  }

  function addEmbed() {
    setExpandedEmbeds((expanded) => new Set(expanded).add(embeds.length))
    setEmbeds((prev) => [...prev, emptyEmbed()])
  }

  function removeEmbed(index: number) {
    setEmbeds((prev) => prev.filter((_, i) => i !== index))
    setExpandedEmbeds((prev) => {
      const next = new Set<number>()
      for (const i of prev) {
        if (i < index) next.add(i)
        else if (i > index) next.add(i - 1)
      }
      return next
    })
  }

  function updateField(embedIndex: number, fieldIndex: number, p: Partial<EmbedField>) {
    setEmbeds((prev) =>
      prev.map((e, i) => (i !== embedIndex ? e : { ...e, fields: e.fields.map((f, fi) => (fi === fieldIndex ? { ...f, ...p } : f)) }))
    )
  }

  function addField(embedIndex: number) {
    setEmbeds((prev) => prev.map((e, i) => (i === embedIndex ? { ...e, fields: [...e.fields, { name: "", value: "", inline: false }] } : e)))
  }

  function removeField(embedIndex: number, fieldIndex: number) {
    setEmbeds((prev) => prev.map((e, i) => (i === embedIndex ? { ...e, fields: e.fields.filter((_, fi) => fi !== fieldIndex) } : e)))
  }

  function moveField(embedIndex: number, fieldIndex: number, direction: -1 | 1) {
    setEmbeds((prev) =>
      prev.map((e, i) => {
        if (i !== embedIndex) return e
        const target = fieldIndex + direction
        if (target < 0 || target >= e.fields.length) return e
        const fields = [...e.fields]
        ;[fields[fieldIndex], fields[target]] = [fields[target], fields[fieldIndex]]
        return { ...e, fields }
      })
    )
  }

  function trackField(e: React.FocusEvent<HTMLDivElement>) {
    const t = e.target
    if (t instanceof HTMLTextAreaElement || (t instanceof HTMLInputElement && t.type === "text")) lastFieldRef.current = t
  }

  /** Insert at the cursor of the last focused text field (else the message text), through a
   *  real input event so the field's own onChange updates the draft. */
  function insertPlaceholder(token: string) {
    const remembered = lastFieldRef.current
    const el = remembered && remembered.isConnected ? remembered : contentRef.current
    if (!el) return
    el.focus()
    const start = el.selectionStart ?? el.value.length
    const end = el.selectionEnd ?? el.value.length
    if (document.execCommand?.("insertText", false, token)) return
    const next = el.value.slice(0, start) + token + el.value.slice(end)
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set
    setter?.call(el, next)
    el.dispatchEvent(new Event("input", { bubbles: true }))
    el.setSelectionRange(start + token.length, start + token.length)
  }

  function discard() {
    try {
      const d = JSON.parse(saved) as Draft
      setDraft(d)
      setExpandedEmbeds(d.embeds.length === 1 ? new Set([0]) : new Set())
    } catch {
      /* keep the current draft */
    }
  }

  async function handleSave() {
    if (templateId === null) return
    setIsSaving(true)
    try {
      const cleaned = embeds.map((e) => ({
        ...e,
        title: nullifyBlank(e.title),
        description: nullifyBlank(e.description),
        url: nullifyBlank(e.url),
        footer_text: nullifyBlank(e.footer_text),
        footer_icon_url: nullifyBlank(e.footer_icon_url),
        thumbnail_url: nullifyBlank(e.thumbnail_url),
        image_url: nullifyBlank(e.image_url),
        author_name: nullifyBlank(e.author_name),
        author_icon_url: nullifyBlank(e.author_icon_url),
        author_url: nullifyBlank(e.author_url),
        fields: e.fields.filter((f) => f.name.trim() && f.value.trim()),
      }))

      await Promise.all([
        updateTemplate.mutateAsync({
          templateId,
          name,
          description: nullifyBlank(description),
          category: nullifyBlank(category),
          content: nullifyBlank(content),
        }),
        replaceEmbeds.mutateAsync({ templateId, embeds: cleaned }),
      ])

      setSaved(JSON.stringify(draft))
      toast({ title: "Template saved", variant: "success" })
    } catch (error) {
      toast({
        title: "Failed to save template",
        description: describeApiError(error, "Failed to save template."),
        variant: "destructive",
      })
    } finally {
      setIsSaving(false)
    }
  }

  function handleSend() {
    if (templateId === null || !selectedChannelId) return
    postTemplate.mutate(
      { templateId, channelId: selectedChannelId },
      {
        onSuccess: () => {
          const channelName = channelsQuery.data?.find((c) => c.id === selectedChannelId)?.name
          toast({
            title: "Template sent",
            description: channelName ? `Posted to #${channelName}.` : "Posted to the selected channel.",
            variant: "success",
          })
        },
        onError: (error) => {
          toast({
            title: "Failed to send template",
            description: describeApiError(error, "Failed to send template."),
            variant: "destructive",
          })
        },
      }
    )
  }

  return (
    <PageContainer width="wide">
      <Button asChild variant="ghost" size="sm" className="-ml-2 w-fit">
        <Link to="/dashboard/templates">
          <ArrowLeft /> Back to templates
        </Link>
      </Button>
      <PageHeader
        title={loaded ? name || "Template" : "Edit template"}
        badge={dirty ? <Badge variant="warning">Unsaved changes</Badge> : null}
        description={loaded ? (category ? `Category: ${category}` : "No category") : undefined}
        actions={
          loaded ? (
            <>
              <Button variant="ghost" onClick={discard} disabled={!dirty || isSaving}>
                Discard
              </Button>
              <Button onClick={handleSave} disabled={!dirty || !name.trim()} loading={isSaving}>
                {isSaving ? "Saving" : "Save changes"}
              </Button>
            </>
          ) : null
        }
      />

      {templateQuery.isPending ? (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_440px]" aria-busy="true">
          <Skeleton className="h-96 w-full" />
          <Skeleton className="h-72 w-full" />
        </div>
      ) : null}

      {templateQuery.isError && !forbidden ? (
        <Card>
          {templateQuery.error instanceof ApiError && templateQuery.error.status === 404 ? (
            <ErrorState title="Template not found" message="It may have been deleted." />
          ) : (
            <ErrorState
              title="Could not load template"
              message={describeApiError(templateQuery.error)}
              onRetry={() => void templateQuery.refetch()}
              retrying={templateQuery.isRefetching}
              showStatusLink
            />
          )}
        </Card>
      ) : null}

      {loaded ? (
        <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_440px]">
          <div className="flex min-w-0 flex-col gap-6" onFocusCapture={trackField}>
            <Card>
              <CardHeader>
                <CardTitle>Details</CardTitle>
                <CardDescription>Name, category, and message text (sent alongside any embeds).</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Name" required error={name.trim() ? null : "A template needs a name."}>
                    {(p) => <Input {...p} className="font-mono" value={name} onChange={(e) => patch({ name: e.target.value })} />}
                  </Field>
                  <Field label="Category">{(p) => <Input {...p} value={category} onChange={(e) => patch({ category: e.target.value })} />}</Field>
                </div>
                <Field label="Description">{(p) => <Input {...p} value={description} onChange={(e) => patch({ description: e.target.value })} />}</Field>
                <Field
                  label="Message text"
                  hint="Plain text sent with the embeds. Placeholders (list at the bottom) work in every text field."
                  labelAction={<CharCounter value={content.length} max={2000} />}
                >
                  {(p) => (
                    <Textarea {...p} ref={contentRef} value={content} onChange={(e) => patch({ content: e.target.value })} rows={3} maxLength={2000} />
                  )}
                </Field>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Embeds</CardTitle>
                <CardDescription>Each embed is a collapsible group. The preview updates as you type.</CardDescription>
                <div className="col-start-2 row-span-2 row-start-1 self-start justify-self-end">
                  <Badge variant="outline">{embeds.length} of 10</Badge>
                </div>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {embeds.map((embed, embedIndex) => {
                  const isExpanded = expandedEmbeds.has(embedIndex)
                  return (
                    <div key={embedIndex} className="rounded-lg border border-border">
                      <div className="flex items-center justify-between gap-3 p-3">
                        <button
                          type="button"
                          onClick={() => toggleEmbedExpanded(embedIndex)}
                          className="flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-md text-left"
                          aria-expanded={isExpanded}
                        >
                          {isExpanded ? (
                            <ChevronDown className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                          ) : (
                            <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                          )}
                          <span
                            aria-hidden
                            className="size-3 shrink-0 rounded-full border border-border"
                            style={{ backgroundColor: `#${(embed.color ?? 0).toString(16).padStart(6, "0")}` }}
                          />
                          <span className="truncate text-base font-medium text-foreground">
                            Embed {embedIndex + 1}
                            {embed.title ? <span className="font-normal text-muted-foreground"> · {embed.title}</span> : null}
                          </span>
                        </button>
                        <Button type="button" variant="destructive-outline" size="sm" className="shrink-0" onClick={() => removeEmbed(embedIndex)}>
                          <Trash2 /> Remove embed
                        </Button>
                      </div>

                      {isExpanded ? (
                        <div className="flex flex-col gap-4 border-t border-border p-4">
                          <Field label="Title">{(p) => <Input {...p} value={embed.title ?? ""} onChange={(e) => updateEmbed(embedIndex, { title: e.target.value })} />}</Field>
                          <Field label="Title URL">{(p) => <Input {...p} value={embed.url ?? ""} onChange={(e) => updateEmbed(embedIndex, { url: e.target.value })} />}</Field>
                          <Field label="Description" labelAction={<CharCounter value={(embed.description ?? "").length} max={4096} />}>
                            {(p) => (
                              <Textarea {...p} value={embed.description ?? ""} maxLength={4096} onChange={(e) => updateEmbed(embedIndex, { description: e.target.value })} rows={4} />
                            )}
                          </Field>

                          <div>
                            <p className="mb-1.5 text-sm font-medium text-foreground">Colour</p>
                            <EmbedColorPicker value={embed.color} onChange={(c) => updateEmbed(embedIndex, { color: c })} />
                          </div>

                          <div className="grid gap-4 sm:grid-cols-2">
                            <Field label="Author name">{(p) => <Input {...p} value={embed.author_name ?? ""} onChange={(e) => updateEmbed(embedIndex, { author_name: e.target.value })} />}</Field>
                            <Field label="Author icon URL">{(p) => <Input {...p} value={embed.author_icon_url ?? ""} onChange={(e) => updateEmbed(embedIndex, { author_icon_url: e.target.value })} />}</Field>
                            <Field label="Thumbnail URL">{(p) => <Input {...p} value={embed.thumbnail_url ?? ""} onChange={(e) => updateEmbed(embedIndex, { thumbnail_url: e.target.value })} />}</Field>
                            <Field label="Image URL">{(p) => <Input {...p} value={embed.image_url ?? ""} onChange={(e) => updateEmbed(embedIndex, { image_url: e.target.value })} />}</Field>
                            <Field label="Footer text">{(p) => <Input {...p} value={embed.footer_text ?? ""} onChange={(e) => updateEmbed(embedIndex, { footer_text: e.target.value })} />}</Field>
                            <Field label="Footer icon URL">{(p) => <Input {...p} value={embed.footer_icon_url ?? ""} onChange={(e) => updateEmbed(embedIndex, { footer_icon_url: e.target.value })} />}</Field>
                          </div>

                          <div className="flex items-center gap-2">
                            <Checkbox
                              id={`ts-${embedIndex}`}
                              checked={embed.timestamp_mode === "now"}
                              onCheckedChange={(v) => updateEmbed(embedIndex, { timestamp_mode: v === true ? "now" : "none" })}
                            />
                            <Label htmlFor={`ts-${embedIndex}`}>Show current timestamp in footer</Label>
                          </div>

                          <div className="flex flex-col gap-2">
                            <p className="text-sm font-medium text-foreground">Fields</p>
                            {embed.fields.map((field, fieldIndex) => (
                              <div key={fieldIndex} className="flex flex-col gap-2 rounded-lg border border-border p-3">
                                <div className="flex items-center gap-3">
                                  <Input
                                    type="text"
                                    aria-label={`Field ${fieldIndex + 1} name`}
                                    placeholder="Field name"
                                    value={field.name}
                                    onChange={(e) => updateField(embedIndex, fieldIndex, { name: e.target.value })}
                                    className="flex-1"
                                  />
                                  <div className="flex items-center gap-1.5">
                                    <Checkbox
                                      id={`inl-${embedIndex}-${fieldIndex}`}
                                      checked={Boolean(field.inline)}
                                      onCheckedChange={(v) => updateField(embedIndex, fieldIndex, { inline: v === true })}
                                    />
                                    <Label htmlFor={`inl-${embedIndex}-${fieldIndex}`} className="text-xs text-muted-foreground">
                                      Inline
                                    </Label>
                                  </div>
                                </div>
                                <Textarea
                                  aria-label={`Field ${fieldIndex + 1} value`}
                                  placeholder="Field value"
                                  value={field.value}
                                  onChange={(e) => updateField(embedIndex, fieldIndex, { value: e.target.value })}
                                  rows={2}
                                  className="w-full"
                                />
                                <div className="flex gap-1">
                                  <Button type="button" variant="ghost" size="icon-sm" aria-label="Move field up" disabled={fieldIndex === 0} onClick={() => moveField(embedIndex, fieldIndex, -1)}>
                                    <ArrowUp />
                                  </Button>
                                  <Button type="button" variant="ghost" size="icon-sm" aria-label="Move field down" disabled={fieldIndex === embed.fields.length - 1} onClick={() => moveField(embedIndex, fieldIndex, 1)}>
                                    <ArrowDown />
                                  </Button>
                                  <Button type="button" variant="ghost" size="sm" className="ml-auto text-danger" onClick={() => removeField(embedIndex, fieldIndex)}>
                                    <Trash2 /> Remove
                                  </Button>
                                </div>
                              </div>
                            ))}
                            <Button type="button" variant="secondary" size="sm" className="w-fit" onClick={() => addField(embedIndex)}>
                              <Plus /> Add field
                            </Button>
                          </div>
                        </div>
                      ) : null}
                    </div>
                  )
                })}

                <Button type="button" variant="secondary" className="w-fit" onClick={addEmbed} disabled={embeds.length >= 10}>
                  <Plus /> Add embed
                </Button>
              </CardContent>
            </Card>

            <PlaceholdersPanel onInsert={insertPlaceholder} />
          </div>

          <div className="flex flex-col gap-6 lg:sticky lg:top-20">
            <Card>
              <CardHeader>
                <CardTitle>Preview</CardTitle>
                <CardDescription>Placeholders show example values (you as the member).</CardDescription>
              </CardHeader>
              <CardContent>
                <DiscordMessage text={resolvePlaceholders(content, catalogue) || undefined}>
                  {previewEmbeds.length === 0 && !content.trim() ? <p className="text-sm text-muted-foreground">Nothing to preview yet. Add message text or an embed.</p> : null}
                  {previewEmbeds.map((embed, i) => (
                    <EmbedPreview
                      key={i}
                      title={embed.title}
                      description={embed.description}
                      color={embed.color}
                      url={embed.url}
                      footer={{ text: embed.footer_text, icon_url: embed.footer_icon_url }}
                      thumbnail_url={embed.thumbnail_url}
                      image_url={embed.image_url}
                      author={{ name: embed.author_name, icon_url: embed.author_icon_url, url: embed.author_url }}
                      fields={embed.fields}
                      timestamp_mode={embed.timestamp_mode}
                    />
                  ))}
                </DiscordMessage>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Send to channel</CardTitle>
                <CardDescription>Sends the last saved version, not unsaved edits. The message auto-updates whenever you save this template again.</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-3">
                {channelsQuery.isError ? (
                  <p className="text-sm text-danger">Could not load channels.</p>
                ) : (
                  <Field label="Channel">
                    {(p) => (
                      <Select {...p} value={selectedChannelId} onChange={(e) => setSelectedChannelId(e.target.value)} disabled={channelsQuery.isPending}>
                        <option value="" disabled>
                          {channelsQuery.isPending ? "Loading channels…" : "Select a channel"}
                        </option>
                        {(channelsQuery.data ?? []).map((channel) => (
                          <option key={channel.id} value={channel.id}>
                            #{channel.name}
                          </option>
                        ))}
                      </Select>
                    )}
                  </Field>
                )}
                <Button
                  type="button"
                  variant="secondary"
                  className="w-fit"
                  onClick={handleSend}
                  disabled={!selectedChannelId || bot.offline}
                  loading={postTemplate.isPending}
                  title={bot.reason}
                >
                  <Send /> {postTemplate.isPending ? "Sending" : "Send test message"}
                </Button>
              </CardContent>
            </Card>

            {templateId !== null ? (
              <TemplateToolsCard
                templateId={templateId}
                name={name}
                dirty={dirty}
                onRolledBack={async () => {
                  // Reload the draft from the server's rolled-back version.
                  await templateQuery.refetch()
                  setLoaded(false)
                }}
              />
            ) : null}
          </div>
        </div>
      ) : null}
    </PageContainer>
  )
}

function isForbiddenError(body: unknown): body is ForbiddenError {
  return (
    typeof body === "object" &&
    body !== null &&
    "error" in body &&
    (body as { error: unknown }).error === "forbidden"
  )
}
