import { lazy, Suspense } from "react"
import { Routes, Route, Outlet, Navigate } from "react-router-dom"
import HomePage from "@/pages/home/HomePage"
import StatusPage from "@/pages/status/StatusPage"
import { RequireAuth } from "@/app/RequireAuth"
import { AppShell } from "@/components/layout/AppShell"
import { PageTransition } from "@/components/layout/PageTransition"
import { PageSkeleton } from "@/components/patterns/states"
import NotFound404 from "@/pages/notfound/NotFound404"
import { OwnerOnlyRoute } from "@/pages/notfound/OwnerOnlyRoute"
import { pageLoaders } from "@/app/pageLoaders"

// Every protected page is its own chunk (loaders in app/pageLoaders.ts, which
// AppShell also uses to preload them in the background after sign-in, so a
// page switch never waits for its code). HomePage stays a static import since
// it's the very first thing anyone sees (public landing page).
const DashboardPage = lazy(pageLoaders.DashboardPage)
const ModerationPage = lazy(pageLoaders.ModerationPage)
const TicketsPage = lazy(pageLoaders.TicketsPage)
const TicketDetailPage = lazy(pageLoaders.TicketDetailPage)
const TicketPanelBuilderPage = lazy(pageLoaders.TicketPanelBuilderPage)
const PermissionsPage = lazy(pageLoaders.PermissionsPage)
const SelfRolesPage = lazy(pageLoaders.SelfRolesPage)
const TemplatesPage = lazy(pageLoaders.TemplatesPage)
const TemplateEditorPage = lazy(pageLoaders.TemplateEditorPage)
const QotdPage = lazy(pageLoaders.QotdPage)
const SettingsPage = lazy(pageLoaders.SettingsPage)
const HoneypotPage = lazy(pageLoaders.HoneypotPage)
const DiagnosticsPage = lazy(pageLoaders.DiagnosticsPage)
const RolesPage = lazy(pageLoaders.RolesPage)
const FortniteShopPage = lazy(pageLoaders.FortniteShopPage)
const BirthdaysPage = lazy(pageLoaders.BirthdaysPage)
const AutomationPage = lazy(pageLoaders.AutomationPage)
const AuditLogPage = lazy(pageLoaders.AuditLogPage)
const MusicPage = lazy(pageLoaders.MusicPage)
const MembersPage = lazy(pageLoaders.MembersPage)
const MemberProfilePage = lazy(pageLoaders.MemberProfilePage)
const ModToolsPage = lazy(pageLoaders.ModToolsPage)
// Hidden owner-only page: OwnerOnlyRoute renders the catch-all NotFound404 for
// everyone else (chunk not even loaded), identical to an unknown path.
const DebugPage = lazy(() => import("@/pages/debug/DebugPage"))
// Public (no auth, no AppShell): banned users cannot log in to appeal.
const AppealPage = lazy(() => import("@/pages/appeal/AppealPage"))
// Public (no auth, no AppShell): Terms of Service and the member hub.
const TermsPage = lazy(() => import("@/pages/terms/TermsPage"))
const HubPage = lazy(() => import("@/pages/hub/HubPage"))
// Public (no auth, no AppShell): Privacy Policy (Discord Developer Portal URL).
const PrivacyPage = lazy(() => import("@/pages/privacy/PrivacyPage"))

/**
 * Route tree: "/" (public homepage/login) and a single "/dashboard/*" layout
 * route wrapping every protected page in ONE <RequireAuth><AppShell> pair
 * instead of each page calling both individually — previously every page
 * hand-rolled its own <AppShell> wrapper AND its own content max-width
 * wrapper, which had already visibly drifted (max-w-3xl/4xl/5xl/6xl all
 * present with no reasoning tied to content needs). Page components now
 * render ONLY their own content; AppShell (sidebar/header/command palette)
 * and the outer <RequireAuth> auth gate live here, once.
 *
 * Permission-level gating (e.g. "moderate_members", "manage_tickets") is
 * still enforced inline per-page (each page's own forbidden-403 branch) and
 * server-side on every backend route — this layout route only handles the
 * auth-LEVEL check, matching RequireAuth's own doc comment about composing
 * permission guards INSIDE it rather than collapsing everything into one
 * check.
 */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/status" element={<StatusPage />} />
      {/* Short link to the owner-only Errors panel; everyone else ends on the same 404 as /dashboard/debug. */}
      <Route path="/admin" element={<Navigate to="/dashboard/debug?tab=errors" replace />} />
      <Route
        path="/appeal"
        element={
          <Suspense fallback={null}>
            <AppealPage />
          </Suspense>
        }
      />
      <Route
        path="/appeal/:token"
        element={
          <Suspense fallback={null}>
            <AppealPage />
          </Suspense>
        }
      />
      <Route
        path="/terms"
        element={
          <Suspense fallback={null}>
            <TermsPage />
          </Suspense>
        }
      />
      <Route
        path="/hub"
        element={
          <Suspense fallback={null}>
            <HubPage />
          </Suspense>
        }
      />
      <Route
        path="/privacy"
        element={
          <Suspense fallback={null}>
            <PrivacyPage />
          </Suspense>
        }
      />
      <Route
        path="/dashboard"
        element={
          <RequireAuth>
            <AppShell>
              <PageTransition>
                <Suspense fallback={<PageSkeleton />}>
                  <Outlet />
                </Suspense>
              </PageTransition>
            </AppShell>
          </RequireAuth>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="moderation" element={<ModerationPage />} />
        <Route path="honeypot" element={<HoneypotPage />} />
        <Route path="tickets" element={<TicketsPage />} />
        <Route path="tickets/:ticketId" element={<TicketDetailPage />} />
        <Route path="tickets/panels/new" element={<TicketPanelBuilderPage />} />
        <Route path="permissions" element={<PermissionsPage />} />
        <Route path="self-roles" element={<SelfRolesPage />} />
        <Route path="templates" element={<TemplatesPage />} />
        <Route path="templates/:templateId" element={<TemplateEditorPage />} />
        <Route path="qotd" element={<QotdPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="diagnostics" element={<DiagnosticsPage />} />
        <Route path="roles" element={<RolesPage />} />
        <Route path="fortnite" element={<FortniteShopPage />} />
        <Route path="birthdays" element={<BirthdaysPage />} />
        <Route path="automation" element={<AutomationPage />} />
        <Route path="audit" element={<AuditLogPage />} />
        <Route path="music" element={<MusicPage />} />
        <Route path="members" element={<MembersPage />} />
        <Route path="members/:userId" element={<MemberProfilePage />} />
        <Route path="message-tools" element={<ModToolsPage />} />
        <Route
          path="debug"
          element={
            <OwnerOnlyRoute>
              <DebugPage />
            </OwnerOnlyRoute>
          }
        />
        {/* Catch-all: any unknown /dashboard/* path, inside the shell. */}
        <Route path="*" element={<NotFound404 />} />
      </Route>
      {/* Catch-all for every other unknown URL (public, no shell). */}
      <Route path="*" element={<NotFound404 standalone />} />
    </Routes>
  )
}
