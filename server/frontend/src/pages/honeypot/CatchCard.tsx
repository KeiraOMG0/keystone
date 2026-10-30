import { Badge } from "@/components/ui/badge"
import { EmbedPreview, type EmbedPreviewField } from "@/components/embed/EmbedPreview"
import type { HoneypotCatch } from "@/types/api"
import { cn } from "@/lib/utils"
import { ACTION_LABEL, accountAge, formatDateTime, formatDuration, formatRelative } from "./format"

const RESULT_COLOR: Record<string, number> = {
  ok: 0xed4245,
  partial: 0xf0b232,
  failed: 0x7b8291,
}

function flagList(c: HoneypotCatch): string {
  const out: string[] = []
  if (c.flags.link) out.push("link")
  if (c.flags.invite) out.push("invite")
  if (c.flags.everyone) out.push("@everyone")
  if (c.flags.attachment) out.push("attachment")
  if (c.had_default_avatar) out.push("default avatar")
  return out.length ? out.join(", ") : "none"
}

const APPEAL_BADGE: Record<string, { label: string; variant: "info" | "warning" | "success" | "destructive" }> = {
  invited: { label: "Appeal link sent", variant: "info" },
  pending: { label: "Appeal pending", variant: "warning" },
  approved: { label: "Appeal approved", variant: "success" },
  denied: { label: "Appeal denied", variant: "destructive" },
}

/** One honeypot catch rendered as a Discord-style embed card. */
export function CatchCard({ item, isNew = false }: { item: HoneypotCatch; isNew?: boolean }) {
  const fields: EmbedPreviewField[] = [
    { name: "User", value: `${item.username}\n${item.user_id}`, inline: true },
    { name: "Action", value: `${ACTION_LABEL[item.action] ?? item.action} (${item.action_result})`, inline: true },
    { name: "DM sent", value: item.dm_sent ? "Yes" : "No", inline: true },
    { name: "Account age", value: accountAge(item.account_created_at, item.created_at), inline: true },
    {
      name: "Joined to caught",
      value: item.seconds_since_join === null ? "unknown" : formatDuration(item.seconds_since_join),
      inline: true,
    },
    { name: "Signals", value: flagList(item), inline: true },
  ]
  if (item.via_app) {
    fields.push({ name: "Posted via app", value: `${item.via_app.name ?? "unknown"}\n${item.via_app.id}`, inline: false })
  }
  if (item.errors.length > 0) {
    fields.push({ name: "Errors", value: item.errors.join("\n"), inline: false })
  }

  const appeal = item.appeal_status ? APPEAL_BADGE[item.appeal_status] : null

  return (
    <article
      className={cn(
        "flex flex-col gap-2 rounded-md",
        isNew && "kb-new outline-2 outline-offset-[6px] outline-primary"
      )}
      aria-label={`Catch: ${item.username}`}
    >
      <EmbedPreview
        className="w-full max-w-[520px]"
        color={RESULT_COLOR[item.action_result] ?? null}
        title="Honeypot catch"
        author={{ name: item.display_name || item.username, icon_url: item.avatar_url }}
        description={item.content ? item.content : "(no text content)"}
        fields={fields}
        footer={{ text: `Caught ${formatDateTime(item.created_at)}` }}
      />
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>{formatRelative(item.created_at)}</span>
        {item.via_app ? <Badge variant="warning">Via app</Badge> : null}
        {appeal ? <Badge variant={appeal.variant}>{appeal.label}</Badge> : null}
      </div>
    </article>
  )
}
