import {
  Ticket,
  FilePlus2,
  FileText,
  KeyRound,
  MessageSquareWarning,
  ShieldAlert,
  Sparkles,
  ShieldCheck,
  ScrollText,
  Music,
  Target,
  Palette,
  Keyboard,
  type LucideIcon,
} from "lucide-react"
import type { NavItem } from "@/components/layout/navLinks"
import { STAFF_ONLY_PERMISSION } from "@/lib/owner"

export type CommandGroup = "Recent" | "Pages" | "Settings" | "Actions"

export interface Command {
  id: string
  label: string
  group: CommandGroup
  icon: LucideIcon
  /** Route to open. Exactly one of `to` / `run` is set. */
  to?: string
  /** Non-navigation action (theme switch, shortcuts dialog). */
  run?: () => void
  /** Extra terms matched against the query beyond the label itself. */
  keywords?: string[]
  requiredPermission?: string
}

/**
 * Static command index for Ctrl+K. Deliberately NOT auto-derived from
 * NAV_LINKS alone: the palette also surfaces destinations without their own
 * sidebar entry (individual Settings tabs via ?tab=, quick actions) so a
 * search like "music" finds the Music settings tab. Commands whose target
 * doesn't exist as a real route are left out rather than linking somewhere
 * dead. Pages are permission-filtered by the caller (AppShell + palette).
 */
export function buildCommands(
  navLinks: NavItem[],
  actions: { cycleTheme: () => void; showShortcuts: () => void }
): Command[] {
  const pages: Command[] = navLinks.map((link) => ({
    id: `page:${link.to}`,
    label: link.label,
    group: "Pages",
    icon: link.icon,
    to: link.to,
    requiredPermission: link.requiredPermission,
    keywords: PAGE_KEYWORDS[link.to] ?? [],
  }))

  const settingsTabs: Command[] = [
    { id: "settings:verification", label: "Verification settings", to: "/dashboard/settings?tab=verification", icon: ShieldCheck, keywords: ["settings", "verify", "verification"] },
    { id: "settings:logging", label: "Logging settings", to: "/dashboard/settings?tab=logging", icon: ScrollText, keywords: ["settings", "logs", "log channel"] },
    { id: "settings:antispam", label: "Anti-spam settings", to: "/dashboard/settings?tab=antispam", icon: ShieldAlert, keywords: ["settings", "spam", "anti-spam", "flood", "raid", "mentions"] },
    { id: "settings:tickets", label: "Ticket settings", to: "/dashboard/settings?tab=tickets", icon: Ticket, keywords: ["settings", "tickets", "chat log", "archive"] },
    { id: "settings:music", label: "Music settings", to: "/dashboard/settings?tab=music", icon: Music, keywords: ["settings", "music", "queue", "controls"] },
    { id: "settings:permissions", label: "Permission settings", to: "/dashboard/settings?tab=permissions", icon: KeyRound, keywords: ["settings", "permissions", "owner"] },
  ].map((c) => ({ ...c, group: "Settings" as const, requiredPermission: STAFF_ONLY_PERMISSION }))

  const quickActions: Command[] = [
    { id: "action:warn", label: "Warn user", to: "/dashboard/moderation", icon: MessageSquareWarning, keywords: ["warn", "warning", "warning history"], requiredPermission: "moderate_members" },
    { id: "action:honeypot", label: "Honeypot appeals", to: "/dashboard/honeypot", icon: Target, keywords: ["honeypot", "appeal", "trap", "hacked", "spam", "catches"], requiredPermission: "moderate_members" },
    { id: "action:qotd", label: "Generate QOTD", to: "/dashboard/qotd", icon: Sparkles, keywords: ["qotd", "question of the day", "generate", "post announcement"], requiredPermission: "manage_qotd" },
    { id: "action:new-panel", label: "New ticket panel", to: "/dashboard/tickets/panels/new", icon: FilePlus2, keywords: ["ticket", "panel", "create"], requiredPermission: "manage_tickets" },
    { id: "action:new-template", label: "New template", to: "/dashboard/templates", icon: FileText, keywords: ["template", "create"], requiredPermission: "manage_templates" },
    { id: "action:grant", label: "Grant permission", to: "/dashboard/permissions", icon: KeyRound, keywords: ["grant", "permission", "role"], requiredPermission: STAFF_ONLY_PERMISSION },
  ].map((c) => ({ ...c, group: "Actions" as const }))

  const local: Command[] = [
    { id: "action:theme", label: "Switch theme", run: actions.cycleTheme, icon: Palette, keywords: ["theme", "dark", "light", "appearance", "mode"] },
    { id: "action:shortcuts", label: "Keyboard shortcuts", run: actions.showShortcuts, icon: Keyboard, keywords: ["keys", "hotkeys", "help"] },
  ].map((c) => ({ ...c, group: "Actions" as const }))

  return [...pages, ...settingsTabs, ...quickActions, ...local]
}

const PAGE_KEYWORDS: Record<string, string[]> = {
  "/dashboard": ["overview", "home", "stats"],
  "/dashboard/moderation": ["warn", "warning", "warning history", "kick", "ban", "timeout"],
  "/dashboard/honeypot": ["honeypot", "trap", "appeal", "hacked account", "spam bot"],
  "/dashboard/tickets": ["ticket", "open tickets", "ticket panels"],
  "/dashboard/self-roles": ["reaction roles", "self assign"],
  "/dashboard/templates": ["template", "ticket templates", "message templates"],
  "/dashboard/automation": ["event", "welcome", "boost", "schedule", "cron", "autorole", "managed messages"],
  "/dashboard/qotd": ["question of the day", "qotd"],
  "/dashboard/music": ["music", "player", "queue", "play", "skip", "volume", "playlist", "now playing"],
  "/dashboard/permissions": ["roles", "grants", "staff"],
  "/dashboard/settings": ["config", "configuration"],
  "/dashboard/diagnostics": ["diagnostics", "debug", "health", "cpu", "memory", "ram", "logs", "cookies", "pot server", "performance", "ops"],
}
