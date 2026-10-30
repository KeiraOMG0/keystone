import {
  LayoutDashboard,
  ShieldAlert,
  Target,
  Ticket,
  Smile,
  FileText,
  MessageSquareText,
  KeyRound,
  SlidersHorizontal,
  Activity,
  Shapes,
  ShoppingBag,
  Cake,
  Music,
  Workflow,
  Bug,
  Contact,
  Eraser,
  ScrollText,
  type LucideIcon,
} from "lucide-react"
import { AUTOMATION_ANY_PERMISSION, MODERATION_ANY_PERMISSION, OWNER_ONLY_PERMISSION, STAFF_ONLY_PERMISSION } from "@/lib/owner"

export type NavGroupKey = "overview" | "moderation" | "community" | "system"

export interface NavItem {
  to: string
  label: string
  end: boolean
  icon: LucideIcon
  requiredPermission?: string
  group: NavGroupKey
  /** Numeric badge fed by data the target page already fetches (see NavBadge). */
  badge?: "tickets" | "appeals"
  /** `g` then this key jumps here (keyboard shortcut). */
  hotkey?: string
}

/** Sidebar groups in render order. The Overview group has no heading (single item). */
export const NAV_GROUPS: { key: NavGroupKey; title: string | null }[] = [
  { key: "overview", title: null },
  { key: "moderation", title: "Moderation" },
  { key: "community", title: "Community" },
  { key: "system", title: "System" },
]

/**
 * Single source of truth for every top-level destination: consumed by the
 * sidebar, the phone drawer + bottom nav, the breadcrumb, the command palette
 * (Ctrl+K) and the `g` shortcuts, so a new page only needs an entry here.
 *
 * `requiredPermission` hides an item when the logged-in user's permissions
 * map (GET /api/me via useAuth()) doesn't grant it. This is PURE UX: every
 * route is independently enforced server-side (require_permission /
 * require_staff), hiding a link only avoids showing an option that would
 * 403. Groups with no visible items are not rendered.
 */
export const NAV_LINKS: NavItem[] = [
  { to: "/dashboard", label: "Overview", end: true, icon: LayoutDashboard, group: "overview", hotkey: "o" },
  {
    to: "/dashboard/moderation",
    label: "Moderation",
    end: false,
    icon: ShieldAlert,
    // any of moderate/kick/ban (lib/owner.ts); each section/action keeps its own gate
    requiredPermission: MODERATION_ANY_PERMISSION,
    group: "moderation",
    hotkey: "m",
  },
  {
    // Member profiles (/userinfo, richer); enforced by require_permission("staff_base").
    to: "/dashboard/members",
    label: "Members",
    end: false,
    icon: Contact,
    requiredPermission: "staff_base",
    group: "moderation",
  },
  {
    // !purge (manage_messages_bulk) + !msglog DM export (staff only, inside the page).
    to: "/dashboard/message-tools",
    label: "Message tools",
    end: false,
    icon: Eraser,
    requiredPermission: "manage_messages_bulk",
    group: "moderation",
  },
  {
    to: "/dashboard/honeypot",
    label: "Honeypot",
    end: false,
    icon: Target,
    requiredPermission: "moderate_members",
    group: "moderation",
    badge: "appeals",
    hotkey: "h",
  },
  {
    to: "/dashboard/tickets",
    label: "Tickets",
    end: false,
    icon: Ticket,
    requiredPermission: "manage_tickets",
    group: "moderation",
    badge: "tickets",
    hotkey: "t",
  },
  {
    to: "/dashboard/qotd",
    label: "QOTD",
    end: false,
    icon: MessageSquareText,
    requiredPermission: "manage_qotd",
    group: "community",
  },
  {
    to: "/dashboard/self-roles",
    label: "Self-roles",
    end: false,
    icon: Smile,
    requiredPermission: "manage_reaction_roles",
    group: "community",
  },
  {
    to: "/dashboard/templates",
    label: "Templates",
    end: false,
    icon: FileText,
    requiredPermission: "manage_templates",
    group: "community",
  },
  {
    // Events / Welcome & Boost / Schedules / Autoroles / Managed messages. Shown
    // with ANY of the tab permissions (pseudo-permission set in AuthContext);
    // each tab is enforced by its own permission on /api/automation/*.
    to: "/dashboard/automation",
    label: "Automation",
    end: false,
    icon: Workflow,
    requiredPermission: AUTOMATION_ANY_PERMISSION,
    group: "community",
  },
  {
    // Fortnite item shop daily post; enforced by require_permission("manage_schedules").
    to: "/dashboard/fortnite",
    label: "Fortnite shop",
    end: false,
    icon: ShoppingBag,
    requiredPermission: "manage_schedules",
    group: "community",
  },
  {
    // Birthdays list/config/override/import-export; enforced by require_permission("manage_birthdays").
    to: "/dashboard/birthdays",
    label: "Birthdays",
    end: false,
    icon: Cake,
    requiredPermission: "manage_birthdays",
    group: "community",
  },
  {
    // Music player control; enforced by require_permission("control_music") on /api/music.
    to: "/dashboard/music",
    label: "Music",
    end: false,
    icon: Music,
    requiredPermission: "control_music",
    group: "community",
  },
  {
    // Read-only bot audit log; enforced by require_permission("view_audit") on /api/audit.
    to: "/dashboard/audit",
    label: "Audit log",
    end: false,
    icon: ScrollText,
    requiredPermission: "view_audit",
    group: "system",
  },
  {
    // Staff-only ops console: hidden unless /api/me says is_staff (pure UX;
    // the grantable staff_base is not enough), enforced by require_staff on /api/diagnostics.
    to: "/dashboard/diagnostics",
    label: "Diagnostics",
    end: false,
    icon: Activity,
    requiredPermission: STAFF_ONLY_PERMISSION,
    group: "system",
  },
  {
    // Staff-only role manager: hidden unless is_staff (pure UX), enforced
    // by require_staff on /api/roles (same gate as Permissions).
    to: "/dashboard/roles",
    label: "Roles",
    end: false,
    icon: Shapes,
    requiredPermission: STAFF_ONLY_PERMISSION,
    group: "system",
  },
  {
    to: "/dashboard/permissions",
    label: "Permissions",
    end: false,
    icon: KeyRound,
    requiredPermission: STAFF_ONLY_PERMISSION, // require_staff
    group: "system",
  },
  {
    to: "/dashboard/settings",
    label: "Settings",
    end: false,
    icon: SlidersHorizontal,
    requiredPermission: STAFF_ONLY_PERMISSION, // require_staff
    group: "system",
  },
  {
    // HIDDEN owner-only test bench: shown only when /api/me says is_owner
    // (pseudo-permission, lib/owner.ts); /api/debug/* answers 404 to anyone else.
    to: "/dashboard/debug",
    label: "Debug",
    end: false,
    icon: Bug,
    requiredPermission: OWNER_ONLY_PERMISSION,
    group: "system",
  },
]

/** The nav item whose route contains `pathname` (longest match), for breadcrumbs. */
export function navItemForPath(pathname: string): NavItem | undefined {
  return [...NAV_LINKS]
    .filter((l) => (l.end ? pathname === l.to : pathname === l.to || pathname.startsWith(l.to + "/")))
    .sort((a, b) => b.to.length - a.to.length)[0]
}
