import { Badge } from "@/components/ui/badge"
import { Tip } from "@/components/ui/tooltip"
import type { DepartedStatus, ResolvedUser } from "@/types/api"

const LABEL: Record<DepartedStatus, string> = {
  left: "Left",
  kicked: "Kicked",
  banned: "Banned",
  unbanned: "Unbanned",
}

const VARIANT: Record<DepartedStatus, "outline" | "warning" | "destructive" | "info"> = {
  left: "outline",
  kicked: "warning",
  banned: "destructive",
  unbanned: "info",
}

function formatDate(iso: string | null | undefined): string | null {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
}

/** Sentence for the tooltip / screen readers, e.g. "Kicked by a moderator on 28 Oct 2026, 14:02". */
export function departedDescription(user: ResolvedUser): string {
  const status = user.status ?? "left"
  if (status === "unbanned") {
    const when = formatDate(user.status_changed_at)
    const banned = formatDate(user.departed_at)
    return `Not in the server. Unbanned${when ? ` on ${when}` : ""}${banned ? ` (banned ${banned})` : ""}`
  }
  const when = formatDate(user.departed_at)
  const by =
    user.departure_source === "bot" ? " by the bot" : user.departure_source === "moderator" ? " by a moderator" : ""
  const verb = status === "left" ? "Left the server" : `${LABEL[status]}${by}`
  if (!when) return `${verb} (date unknown)`
  if (user.departure_source === "detected_at_startup") return `${verb}, noticed ${when} (while the bot was offline)`
  return `${verb} on ${when}`
}

/** Small status pill for a user who is not in the server any more (resolve `departed: true`). */
export function DepartedBadge({ user }: { user: ResolvedUser }) {
  if (!user.departed) return null
  const status = user.status ?? "left"
  const description = departedDescription(user)
  return (
    <Tip label={description}>
      <Badge variant={VARIANT[status] ?? "outline"} tabIndex={0} aria-label={description} className="h-5 px-1.5">
        {LABEL[status] ?? "Left"}
      </Badge>
    </Tip>
  )
}
