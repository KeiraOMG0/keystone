import type { AdminRole, RoleAdminOverview } from "@/types/api"

export type RoleFilter = "all" | "manageable" | "locked" | "internal" | "hoisted" | "staff"

export const ROLE_FILTERS: { value: RoleFilter; label: string }[] = [
  { value: "all", label: "All roles" },
  { value: "manageable", label: "Manageable by the bot" },
  { value: "locked", label: "Read-only" },
  { value: "internal", label: "Has bot permissions" },
  { value: "hoisted", label: "Shown separately" },
  { value: "staff", label: "Default staff" },
]

/** Discord's preset role colours (the swatches in its role editor). */
export const PRESET_COLORS = [
  "#1abc9c", "#2ecc71", "#3498db", "#9b59b6", "#e91e63", "#f1c40f", "#e67e22", "#e74c3c", "#95a5a6", "#607d8b",
  "#11806a", "#1f8b4c", "#206694", "#71368a", "#ad1457", "#c27c0e", "#a84300", "#992d22", "#979c9f", "#546e7a",
]

export const HEX_RE = /^#[0-9a-fA-F]{6}$/

export function matchesFilter(role: AdminRole, filter: RoleFilter): boolean {
  switch (filter) {
    case "manageable":
      return role.manageable
    case "locked":
      return !role.manageable
    case "internal":
      return role.internal_permissions.length > 0
    case "hoisted":
      return role.hoist
    case "staff":
      return role.is_default_staff
    default:
      return true
  }
}

export function matchesSearch(role: AdminRole, q: string): boolean {
  const needle = q.trim().toLowerCase()
  if (!needle) return true
  return role.name.toLowerCase().includes(needle) || role.id === needle
}

/**
 * The role that caps where things may go: the bot's top role, or the viewer's
 * own top role when that is lower and the viewer is not the owner (Discord's
 * hierarchy applies to them too; the server enforces the same rule).
 */
export function ceilingRole(data: RoleAdminOverview): { position: number; name: string; isActor: boolean } {
  const a = data.actor
  if (a && !a.is_owner && a.top_position < data.bot.top_position) {
    const name = data.roles.find((r) => r.id === a.top_role_id)?.name ?? "your highest role"
    return { position: a.top_position, name, isActor: true }
  }
  return { position: data.bot.top_position, name: data.bot.top_role_name, isActor: false }
}

/** Highest position a manageable role may take (directly under the ceiling role). */
export function maxManagedPosition(data: RoleAdminOverview): number {
  return Math.max(1, ceilingRole(data).position - 1)
}

/**
 * Discord permission flags the viewer may add/remove on a role: held by the
 * bot AND grantable by the viewer (owner = everything the bot holds).
 */
export function grantablePermissions(data: RoleAdminOverview): Set<string> {
  const bot = new Set(data.bot.permissions)
  if (!data.actor) return bot
  const mine = new Set(data.actor.permissions)
  return new Set([...bot].filter((p) => mine.has(p)))
}

/**
 * Position choices for a role in the bot's band, highest first. `label` names
 * the role currently in that slot so "move to" reads naturally.
 */
export function positionOptions(data: RoleAdminOverview, excludeRoleId?: string): { value: number; label: string }[] {
  const ceiling = ceilingRole(data)
  const max = maxManagedPosition(data)
  const band = data.roles.filter((r) => !r.is_default && r.position < ceiling.position && r.id !== excludeRoleId)
  const out: { value: number; label: string }[] = []
  for (let pos = max; pos >= 1; pos--) {
    const occupant = band.find((r) => r.position === pos)
    const where = pos === max ? `directly below ${ceiling.name}` : occupant ? `where ${occupant.name} is now` : ""
    out.push({ value: pos, label: `Position ${pos}${where ? ` (${where})` : ""}${pos === 1 ? " - bottom" : ""}` })
  }
  return out
}

export function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error ?? new Error("Could not read the file"))
    reader.readAsDataURL(file)
  })
}

export function sameSet(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false
  const s = new Set(a)
  return b.every((x) => s.has(x))
}

export const INTERNAL_PERMISSION_HELP: Record<string, string> = {
  staff_base: "Basic staff access (dashboard overview, warnings)",
  moderate_members: "Moderation page, timeouts, warnings, honeypot view",
  kick_members: "Kick members",
  ban_members: "Ban/unban, honeypot settings and appeals",
  manage_messages_bulk: "Bulk purge",
  verify_members: "Verify members",
  manage_tickets: "Tickets and ticket panels",
  manage_templates: "Message templates",
  manage_events: "Event bindings",
  manage_schedules: "Scheduled sends",
  manage_autorole: "Autorole rules",
  manage_reaction_roles: "Self-role panels",
  manage_qotd: "Question of the day",
  view_audit: "Audit log",
}
