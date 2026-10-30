import { useState } from "react"
import { Eye, ExternalLink, MessageSquare, Send } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Select } from "@/components/ui/select"
import { Field } from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { useBotOffline } from "@/hooks/useBotOffline"
import { describeApiError } from "@/lib/apiError"
import { useLabPreview, type LabOptions, type LabPreviewKind, type LabPreviewResult } from "@/api/debugLab"
import { MemberPicker, type PickedMember } from "./MemberPicker"
import { MessagePreview } from "./EmbedPreview"

type Mode = "welcome" | "boost" | "event" | "template"

const MODE_LABELS: Record<Mode, string> = {
  welcome: "Welcome message",
  boost: "Boost thank-you",
  event: "Event binding",
  template: "Template render",
}

/**
 * Lab: render the welcome / boost / event-binding / template output for any
 * member and show it here; "Send copy" posts the same render to the Debug
 * page's target channel through SafeChannel ([TEST] prefix, only the owner can
 * be pinged; the chosen member is never pinged or DMed).
 */
export function PreviewCard({
  modes,
  options,
  channelId,
  channelName,
}: {
  modes: Mode[]
  options: LabOptions | undefined
  channelId: string
  channelName?: string
}) {
  const preview = useLabPreview()
  const [mode, setModeState] = useState<Mode>(modes[0])
  const setMode = (m: Mode) => {
    setModeState(m)
    setResult(null)
    preview.reset()
  }
  const { offline, reason } = useBotOffline()
  const [member, setMember] = useState<PickedMember | null>(null)
  const [eventType, setEventType] = useState("member_join")
  const [roleId, setRoleId] = useState("")
  const [template, setTemplate] = useState("")
  const [result, setResult] = useState<LabPreviewResult | null>(null)
  const [showPlaceholders, setShowPlaceholders] = useState(false)

  const needsRole = mode === "template" || (mode === "event" && (eventType === "role_add" || eventType === "role_remove"))
  const binding = options?.bindings.find((b) => b.event_type === eventType)
  const missing = !member
    ? "Pick a member"
    : mode === "template" && !template
      ? "Pick a template"
      : mode === "event" && !binding
        ? `Nothing is bound to ${eventType}`
        : undefined

  const run = (send: boolean) => {
    if (!member) return
    preview.mutate(
      {
        kind: mode as LabPreviewKind,
        user_id: member.id,
        event_type: mode === "event" ? eventType : null,
        role_id: needsRole && roleId ? roleId : null,
        template: mode === "template" ? template : null,
        send_channel_id: send ? channelId : null,
      },
      { onSuccess: (r) => setResult(r) }
    )
  }

  const title = modes.length > 1 ? "Event message preview" : MODE_LABELS[mode]
  const description =
    modes.length > 1
      ? "The welcome and boost messages and the /event bindings (member_join, member_leave, boost, role_add, role_remove), rendered for any member."
      : "Any saved template with a chosen member's placeholders (and every placeholder's value)."

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <MessageSquare className="size-4" aria-hidden /> {title}
        </CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {modes.length > 1 ? (
          <Field label="Message">
            {(p) => (
              <Select {...p} value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
                {modes.map((m) => (
                  <option key={m} value={m}>
                    {MODE_LABELS[m]}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        ) : null}
        <MemberPicker value={member} onChange={setMember} />
        {mode === "event" ? (
          <Field
            label="Event"
            hint={
              binding
                ? `Posts to #${binding.channel_name ?? binding.channel_id ?? "?"} using ${binding.templates.join(", ") || "no template"}${binding.enabled ? "" : " (disabled)"}${binding.send_dm ? ", also DMs the member" : ""}.`
                : "No binding for this event."
            }
          >
            {(p) => (
              <Select {...p} value={eventType} onChange={(e) => setEventType(e.target.value)}>
                {(options?.event_types ?? ["member_join", "member_leave", "boost", "role_add", "role_remove"]).map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        ) : null}
        {mode === "template" ? (
          <Field label="Template" required>
            {(p) => (
              <Select {...p} value={template} onChange={(e) => setTemplate(e.target.value)}>
                <option value="">{options?.templates.length ? "Choose…" : "No templates"}</option>
                {(options?.templates ?? []).map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        ) : null}
        {needsRole ? (
          <Field label="Role for {role}" hint="Optional.">
            {(p) => (
              <Select {...p} value={roleId} onChange={(e) => setRoleId(e.target.value)}>
                <option value="">None</option>
                {(options?.roles ?? []).map((r) => (
                  <option key={r.id} value={r.id}>
                    @{r.name}
                  </option>
                ))}
              </Select>
            )}
          </Field>
        ) : null}
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="secondary" onClick={() => run(false)} disabled={Boolean(missing) || preview.isPending || offline} title={missing ?? reason}>
            <Eye /> Preview here
          </Button>
          <Button
            size="sm"
            onClick={() => run(true)}
            disabled={Boolean(missing) || !channelId || preview.isPending || offline}
            title={missing ?? (offline ? reason : !channelId ? "Pick a target channel on the Actions tab" : undefined)}
          >
            <Send /> {channelName ? `Send copy to #${channelName}` : "Send copy"}
          </Button>
          {missing ? <span className="text-xs text-muted-foreground">{missing}</span> : null}
        </div>
        {preview.isError ? <Alert variant="danger" title="Preview failed">{describeApiError(preview.error)}</Alert> : null}
        {preview.isPending ? <Skeleton className="h-24 w-full" /> : null}
        {result && !preview.isPending ? (
          <div className="flex flex-col gap-2">
            {result.sent ? (
              <Alert
                variant="success"
                title="Sent a [TEST] copy"
                action={
                  result.sent.jump_url ? (
                    <Button asChild size="sm" variant="secondary">
                      <a href={result.sent.jump_url} target="_blank" rel="noreferrer">
                        <ExternalLink /> Open
                      </a>
                    </Button>
                  ) : undefined
                }
              >
                {result.sent.message_ids.length} message{result.sent.message_ids.length === 1 ? "" : "s"}.
              </Alert>
            ) : null}
            <div className="flex flex-wrap gap-2 text-xs">
              <Badge variant="outline">For {result.member.display_name}</Badge>
              {result.channel_name ? <Badge variant="outline">Context #{result.channel_name}</Badge> : null}
            </div>
            {result.notes.map((n) => (
              <p key={n} className="text-xs text-warning">
                {n}
              </p>
            ))}
            {result.messages.map((m, i) => (
              <div key={i} className="flex flex-col gap-1">
                {m.template ? <p className="text-xs text-muted-foreground">Template {m.template}</p> : null}
                <MessagePreview content={m.content} embeds={m.embeds} />
              </div>
            ))}
            {mode === "template" || mode === "event" ? (
              <div>
                <Button size="xs" variant="link" onClick={() => setShowPlaceholders((v) => !v)}>
                  {showPlaceholders ? "Hide" : "Show"} placeholder values
                </Button>
                {showPlaceholders ? (
                  <dl className="mt-1 grid grid-cols-[max-content_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
                    {Object.entries(result.placeholders).map(([k, v]) => (
                      <div key={k} className="contents">
                        <dt className="font-mono text-muted-foreground">{`{${k}}`}</dt>
                        <dd className="truncate font-mono text-foreground">{v || "(empty)"}</dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}
