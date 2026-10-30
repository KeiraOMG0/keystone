/**
 * Code-split dashboard pages: one dynamic import per page. app/routes.tsx wraps
 * them in React.lazy; AppShell calls preloadDashboardPages() once the shell is
 * idle after sign-in, so every page's chunk is already cached (the assets are
 * immutable, see server/app.py) when the user clicks it. The owner-only Debug
 * page is not in this list (it stays loaded only on demand).
 */
export const pageLoaders = {
  DashboardPage: () => import("@/pages/dashboard/DashboardPage"),
  ModerationPage: () => import("@/pages/moderation/ModerationPage"),
  TicketsPage: () => import("@/pages/tickets/TicketsPage"),
  TicketDetailPage: () => import("@/pages/tickets/TicketDetailPage"),
  TicketPanelBuilderPage: () => import("@/pages/tickets/TicketPanelBuilderPage"),
  PermissionsPage: () => import("@/pages/permissions/PermissionsPage"),
  SelfRolesPage: () => import("@/pages/selfroles/SelfRolesPage"),
  TemplatesPage: () => import("@/pages/templates/TemplatesPage"),
  TemplateEditorPage: () => import("@/pages/templates/TemplateEditorPage"),
  QotdPage: () => import("@/pages/qotd/QotdPage"),
  SettingsPage: () => import("@/pages/settings/SettingsPage"),
  HoneypotPage: () => import("@/pages/honeypot/HoneypotPage"),
  DiagnosticsPage: () => import("@/pages/diagnostics/DiagnosticsPage"),
  RolesPage: () => import("@/pages/roles/RolesPage"),
  FortniteShopPage: () => import("@/pages/fortnite/FortniteShopPage"),
  BirthdaysPage: () => import("@/pages/birthdays/BirthdaysPage"),
  AutomationPage: () => import("@/pages/automation/AutomationPage"),
  AuditLogPage: () => import("@/pages/audit/AuditLogPage"),
  MusicPage: () => import("@/pages/music/MusicPage"),
  MembersPage: () => import("@/pages/members/MembersPage"),
  MemberProfilePage: () => import("@/pages/members/MemberProfilePage"),
  ModToolsPage: () => import("@/pages/modtools/ModToolsPage"),
} as const

let preloaded = false

/** Fetch every dashboard page chunk in the background, once per page load.
 *  Skipped when the browser asks to save data. Failures are ignored (the
 *  page then simply loads on click, as before). */
export function preloadDashboardPages(): void {
  if (preloaded) return
  preloaded = true
  const conn = (navigator as Navigator & { connection?: { saveData?: boolean } }).connection
  if (conn?.saveData) return
  const run = () => {
    for (const load of Object.values(pageLoaders)) load().catch(() => {})
  }
  const w = window as Window & { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number }
  if (w.requestIdleCallback) w.requestIdleCallback(run, { timeout: 4000 })
  else window.setTimeout(run, 2000)
}
