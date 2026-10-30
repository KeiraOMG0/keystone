import { useRef, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { ArrowLeft } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Alert } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Field } from "@/components/ui/field"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { useBreadcrumb } from "@/components/layout/breadcrumbs"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { EmbedPreview } from "@/components/embed/EmbedPreview"
import { DiscordButton, DiscordMessage } from "@/components/embed/DiscordMessage"
import { useToast } from "@/components/ui/toast"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { useTicketPanels } from "@/api/tickets"
import { describeApiError } from "@/lib/apiError"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"
import { useTemplate, useTemplates } from "@/api/templates"
import { useCreateTicketPanel, useGuildCategories, useTicketPanelChannels } from "@/api/ticketPanels"
import { ApiError } from "@/api/client"
import { useBotOffline } from "@/hooks/useBotOffline"
import type { CreateTicketPanelRequest, CreateTicketPanelResponse, ForbiddenError } from "@/types/api"

/**
 * PROTECTED. Ticket panel creation: a grouped form (Where / Behaviour /
 * Button) beside a sticky Discord-style message preview. Submits to POST
 * /api/ticket-panels (needs "manage_tickets"). On success it shows whether the
 * panel message actually posted to Discord (message_id) or was created but
 * failed to post (post_errors): these are DIFFERENT outcomes and never
 * conflated into one generic "success".
 *
 * Double posts: Create is disabled while the request runs (plus a ref guard
 * against a double click before the re-render), and posting again asks first
 * when this exact panel was just posted from this page or the channel already
 * has a panel. The form is guarded by useUnsavedChangesGuard until posted.
 */
export default function TicketPanelBuilderPage() {
  const channelsQuery = useTicketPanelChannels()
  const categoriesQuery = useGuildCategories()
  const templatesQuery = useTemplates()
  const createPanel = useCreateTicketPanel()
  const panelsQuery = useTicketPanels()
  const submitting = useRef(false)
  const [lastPosted, setLastPosted] = useState<string | null>(null)
  const [confirmRepost, setConfirmRepost] = useState<string | null>(null)
  const { toast } = useToast()
  const navigate = useNavigate()
  const bot = useBotOffline()
  useBreadcrumb("New panel")

  const [channelId, setChannelId] = useState("")
  const [templateName, setTemplateName] = useState("")
  const [openCategoryId, setOpenCategoryId] = useState("")
  const [transcriptChannelId, setTranscriptChannelId] = useState("")
  const [maxOpenPerUser, setMaxOpenPerUser] = useState("1")
  const [namingPattern, setNamingPattern] = useState("ticket-{number}")
  const [buttonLabel, setButtonLabel] = useState("Open Ticket")
  const [buttonEmoji, setButtonEmoji] = useState("")
  const [purpose, setPurpose] = useState("support")
  const [result, setResult] = useState<CreateTicketPanelResponse | null>(null)

  // All ids are Discord snowflakes: pass the strings through unchanged, never Number().
  const payload: CreateTicketPanelRequest = {
    channel_id: channelId,
    template_name: templateName || null,
    open_category_id: openCategoryId || null,
    // Closed tickets are deleted 30 s after close, so there is no closed category any more.
    closed_category_id: null,
    transcript_channel_id: transcriptChannelId || null,
    max_open_per_user: Number(maxOpenPerUser) || 1,
    naming_pattern: namingPattern || "ticket-{number}",
    button_label: buttonLabel || "Open Ticket",
    button_emoji: buttonEmoji || null,
    purpose,
  }
  const signature = JSON.stringify(payload)
  const pristine = JSON.stringify({ ...payload, channel_id: "" }) === DEFAULT_SIGNATURE && !channelId
  // Dirty = edited and not exactly what was just posted.
  useUnsavedChangesGuard(!pristine && signature !== lastPosted && !createPanel.isPending)

  const forbidden =
    channelsQuery.isError &&
    channelsQuery.error instanceof ApiError &&
    channelsQuery.error.status === 403 &&
    isForbiddenError(channelsQuery.error.body)
      ? (channelsQuery.error.body as ForbiddenError)
      : null

  if (forbidden) {
    return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />
  }

  const selectedTemplate = templatesQuery.data?.find((t) => t.name === templateName)
  const example = (namingPattern || "ticket-{number}").replace("{number}", "0142").replace("{username}", "sunny.day")

  const channelHasPanel = (panelsQuery.data ?? []).some((p) => p.channel_id === channelId && p.message_id !== null)

  /** Create clicked: ask first when this would be a second copy of a posted panel. */
  function handleSubmit() {
    if (!channelId || submitting.current || createPanel.isPending) return
    if (signature === lastPosted) {
      setConfirmRepost("You just posted this exact panel. Posting again puts a second, identical Open Ticket message in the channel.")
      return
    }
    if (channelHasPanel) {
      setConfirmRepost("This channel already has a posted ticket panel. Posting another one adds a second Open Ticket message there.")
      return
    }
    submit()
  }

  function submit() {
    if (!channelId || submitting.current) return
    submitting.current = true
    setResult(null)
    const sent = signature
    createPanel.mutate(
      payload,
      {
        onSettled: () => {
          submitting.current = false
        },
        onSuccess: (data) => {
          setResult(data)
          // The panel row exists even when the Discord post failed: either way a
          // repeat click would create another panel, so remember it.
          setLastPosted(sent)
          if (data.message_id) {
            toast({
              title: "Panel created and posted to Discord",
              description: `Message ID ${data.message_id}`,
              variant: "success",
              action: { label: "View panel", onClick: () => navigate("/dashboard/tickets") },
            })
          } else {
            toast({
              title: "Panel created, but it did NOT post to Discord",
              description: data.post_errors[0],
              variant: "destructive",
            })
          }
        },
        onError: (error) => {
          toast({
            title: "Failed to create panel",
            description: describeApiError(error),
            variant: "destructive",
          })
        },
      }
    )
  }

  const channelOptions = (channelsQuery.data ?? []).map((c) => (
    <option key={c.id} value={String(c.id)}>
      #{c.name}
    </option>
  ))
  const categoryOptions = (categoriesQuery.data ?? []).map((c) => (
    <option key={c.id} value={String(c.id)}>
      {c.name}
    </option>
  ))

  return (
    <PageContainer width="form">
      <PageHeader
        title="New ticket panel"
        description="Creates the panel and immediately posts the button message to the selected channel."
        actions={
          <Button asChild variant="secondary">
            <Link to="/dashboard/tickets">
              <ArrowLeft /> Back to tickets
            </Link>
          </Button>
        }
      />

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_440px]">
        <Card>
          <CardHeader>
            <CardTitle>Panel configuration</CardTitle>
            <CardDescription>Optional fields fall back to the defaults shown in their hints.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-5">
            {channelsQuery.isPending ? <Skeleton className="h-9 w-full" /> : null}

            <h3 className="text-2xs font-semibold tracking-[0.06em] text-subtle-foreground uppercase">Where</h3>
            <Field label="Channel" required hint="Where the Open Ticket button message is posted.">
              {(p) => (
                <Select {...p} value={channelId} onChange={(e) => setChannelId(e.target.value)}>
                  <option value="">Select a channel…</option>
                  {channelOptions}
                </Select>
              )}
            </Field>
            <Field label="Open category" hint="Optional. Blank puts tickets at the top level.">
              {(p) => (
                <Select {...p} value={openCategoryId} onChange={(e) => setOpenCategoryId(e.target.value)}>
                  <option value="">No category</option>
                  {categoryOptions}
                </Select>
              )}
            </Field>
            <Field label="Transcript channel" hint="Optional. Blank means no transcripts are posted.">
              {(p) => (
                <Select {...p} value={transcriptChannelId} onChange={(e) => setTranscriptChannelId(e.target.value)}>
                  <option value="">No transcripts</option>
                  {channelOptions}
                </Select>
              )}
            </Field>

            <h3 className="border-t border-border pt-5 text-2xs font-semibold tracking-[0.06em] text-subtle-foreground uppercase">Behaviour</h3>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Max open tickets per user" hint="Defaults to 1.">
                {(p) => <Input {...p} type="number" min={1} value={maxOpenPerUser} onChange={(e) => setMaxOpenPerUser(e.target.value)} />}
              </Field>
              <Field label="Purpose">
                {(p) => (
                  <Select {...p} value={purpose} onChange={(e) => setPurpose(e.target.value)}>
                    <option value="support">Support</option>
                    <option value="verify">Verify</option>
                  </Select>
                )}
              </Field>
            </div>
            <Field
              label="Naming pattern"
              hint={
                <>
                  Placeholders: {"{number}"} (ticket number), {"{username}"}. Example: <span className="font-mono">{example}</span>
                </>
              }
            >
              {(p) => <Input {...p} className="font-mono" value={namingPattern} onChange={(e) => setNamingPattern(e.target.value)} />}
            </Field>

            <h3 className="border-t border-border pt-5 text-2xs font-semibold tracking-[0.06em] text-subtle-foreground uppercase">Button</h3>
            <Field
              label="Template"
              hint={
                templateName && selectedTemplate
                  ? "The preview shows the first embed only. The posted message renders every embed and any plain text."
                  : "Optional. Blank posts a plain panel message with just the button."
              }
            >
              {(p) => (
                <Select {...p} value={templateName} onChange={(e) => setTemplateName(e.target.value)}>
                  <option value="">No template (blank panel message)</option>
                  {(templatesQuery.data ?? []).map((t) => (
                    <option key={t.name} value={t.name}>
                      {t.name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Button label" hint="Defaults to Open Ticket.">
                {(p) => <Input {...p} value={buttonLabel} onChange={(e) => setButtonLabel(e.target.value)} />}
              </Field>
              <Field label="Button emoji" hint="Optional.">
                {(p) => <Input {...p} value={buttonEmoji} onChange={(e) => setButtonEmoji(e.target.value)} />}
              </Field>
            </div>

            {result && !result.message_id ? (
              <Alert variant="danger" title={`Panel created (ID ${result.panel_id}), but it did NOT post to Discord.`}>
                {result.post_errors.length > 0 ? (
                  <ul className="list-disc pl-5">
                    {result.post_errors.map((err, i) => (
                      <li key={i}>{err}</li>
                    ))}
                  </ul>
                ) : null}
              </Alert>
            ) : null}
          </CardContent>
        </Card>

        <Card className="lg:sticky lg:top-20">
          <CardHeader>
            <CardTitle>Message preview</CardTitle>
            <CardDescription>What the panel message will look like.</CardDescription>
          </CardHeader>
          <CardContent>
            <DiscordMessage>
              {selectedTemplate ? <TemplatePreview templateId={selectedTemplate.id} /> : null}
              <DiscordButton label={buttonLabel || "Open Ticket"} emoji={buttonEmoji || null} />
            </DiscordMessage>
          </CardContent>
        </Card>
      </div>

      <div className="sticky bottom-20 z-20 flex flex-wrap items-center justify-end gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-md md:bottom-4">
        {!channelId ? <span className="mr-auto text-sm text-muted-foreground">Choose a channel to enable Create panel.</span> : null}
        <Button asChild variant="ghost">
          <Link to="/dashboard/tickets">Cancel</Link>
        </Button>
        <Button
          type="button"
          onClick={handleSubmit}
          disabled={!channelId || bot.offline || createPanel.isPending}
          loading={createPanel.isPending}
          title={bot.reason}
        >
          {createPanel.isPending ? "Creating" : "Create panel"}
        </Button>
      </div>

      <ConfirmDialog
        open={confirmRepost !== null}
        onOpenChange={(o) => !o && setConfirmRepost(null)}
        title="Post this panel again?"
        description={confirmRepost ?? ""}
        confirmLabel="Post another panel"
        destructive={false}
        onConfirm={submit}
      />
    </PageContainer>
  )
}

function TemplatePreview({ templateId }: { templateId: number }) {
  const query = useTemplate(templateId)

  if (query.isPending) return <Skeleton className="h-32 w-full" />
  if (query.isError || !query.data) return <p className="text-sm text-muted-foreground">Preview unavailable.</p>

  const embed = query.data.embeds[0]
  if (!embed) return <p className="text-sm text-muted-foreground">This template has no embeds (plain text only).</p>

  return (
    <EmbedPreview
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

/** The untouched form (channel blank), to tell "edited" from "pristine". */
const DEFAULT_SIGNATURE = JSON.stringify({
  channel_id: "",
  template_name: null,
  open_category_id: null,
  closed_category_id: null,
  transcript_channel_id: null,
  max_open_per_user: 1,
  naming_pattern: "ticket-{number}",
  button_label: "Open Ticket",
  button_emoji: null,
  purpose: "support",
} satisfies CreateTicketPanelRequest)
