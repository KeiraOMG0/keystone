import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react"
import { NavLink, useLocation } from "react-router-dom"
import { useQueryClient } from "@tanstack/react-query"
import { Ellipsis, Menu, Search } from "lucide-react"
import { useAuth } from "@/hooks/useAuth"
import { NAV_LINKS, navItemForPath } from "@/components/layout/navLinks"
import { CommandPalette } from "@/components/layout/CommandPalette"
import { ProfileMenu } from "@/components/layout/ProfileMenu"
import { SidebarContent } from "@/components/layout/Sidebar"
import { ShortcutsDialog } from "@/components/layout/ShortcutsDialog"
import { UnsavedChangesDialog } from "@/components/layout/UnsavedChangesDialog"
import { Breadcrumbs, BreadcrumbProvider, useCrumbs } from "@/components/layout/breadcrumbs"
import { useHotkeys } from "@/components/layout/useHotkeys"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Kbd } from "@/components/ui/kbd"
import { Sheet, SheetContent } from "@/components/ui/sheet"
import { useLive } from "@/live/useLive"
import { useMediaQuery } from "@/hooks/useMediaQuery"
import { usePrefs } from "@/context/PrefsContext"
import { pushRecent } from "@/lib/storage"
import { cn } from "@/lib/utils"
import { preloadDashboardPages } from "@/app/pageLoaders"

function formatClock(d: Date): string {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
}

/**
 * Connection banners at the top of the content area, all fed by useLive():
 *   bot offline  -> danger banner (actions that need Discord are disabled per page)
 *   offline      -> warning banner "Showing data from HH:MM. Retrying." + Retry now
 *   reconnecting -> warning banner only after 10 s
 *   recovered    -> green "Back online" for 3 s
 */
function LiveBanners() {
  const { state, bot } = useLive()
  const qc = useQueryClient()
  const lastLive = useRef<Date>(new Date())
  const [reconnectingLong, setReconnectingLong] = useState(false)
  const [backOnline, setBackOnline] = useState(false)
  const wasDown = useRef(false)

  useEffect(() => {
    if (state === "live") {
      lastLive.current = new Date()
      if (wasDown.current) {
        wasDown.current = false
        setBackOnline(true)
        const h = window.setTimeout(() => setBackOnline(false), 3000)
        return () => window.clearTimeout(h)
      }
    } else if (state === "offline" || state === "reconnecting") {
      wasDown.current = true
    }
  }, [state])

  useEffect(() => {
    if (state !== "reconnecting") {
      setReconnectingLong(false)
      return
    }
    const h = window.setTimeout(() => setReconnectingLong(true), 10_000)
    return () => window.clearTimeout(h)
  }, [state])

  return (
    <div className="flex flex-col gap-2 empty:hidden">
      {state === "live" && bot.online === false ? (
        <Alert variant="danger" title="The bot is offline">
          Actions that need Discord are paused until it is back. Read-only data keeps showing.
        </Alert>
      ) : null}
      {state === "offline" ? (
        <Alert
          variant="warning"
          title="You are offline"
          action={
            <Button variant="secondary" size="sm" onClick={() => void qc.refetchQueries({ type: "active" })}>
              Retry now
            </Button>
          }
        >
          Showing data from {formatClock(lastLive.current)}. Retrying.
        </Alert>
      ) : null}
      {state === "reconnecting" && reconnectingLong ? (
        <Alert variant="warning" title="Reconnecting">
          Live updates dropped. Showing data from {formatClock(lastLive.current)} while we retry.
        </Alert>
      ) : null}
      {backOnline ? <Alert variant="success">Back online. Live updates resumed.</Alert> : null}
    </div>
  )
}

function PhoneTitle() {
  const crumbs = useCrumbs()
  const current = crumbs[crumbs.length - 1]?.label ?? "Dashboard"
  return <span className="min-w-0 flex-1 truncate text-md font-semibold text-foreground">{current}</span>
}

/**
 * App shell around every protected page: skip link, sidebar (248px, collapsible
 * 64px rail, phone drawer), 56px header (breadcrumbs, Ctrl+K palette trigger,
 * live pill, profile menu), connection banners, and the phone bottom nav.
 * Nothing here changes routing, permissions or the live socket: nav items are
 * still filtered by NAV_LINKS[].requiredPermission (pure UX).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const { user, permissions } = useAuth()
  const { pathname } = useLocation()
  const { sidebarCollapsed, setSidebarCollapsed } = usePrefs()
  const isTablet = useMediaQuery("(min-width: 768px) and (max-width: 1023px)")
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [shortcutsOpen, setShortcutsOpen] = useState(false)

  const collapsed = sidebarCollapsed ?? isTablet

  const visibleNavLinks = useMemo(
    () => NAV_LINKS.filter((link) => !link.requiredPermission || permissions[link.requiredPermission]),
    [permissions]
  )

  const toggleSidebar = useCallback(() => setSidebarCollapsed(!collapsed), [collapsed, setSidebarCollapsed])
  const showShortcuts = useCallback(() => setShortcutsOpen(true), [])
  useHotkeys({ onToggleSidebar: toggleSidebar, onShowShortcuts: showShortcuts })

  // Signed in: fetch every page's code in the background so page switches are instant.
  useEffect(() => {
    if (user) preloadDashboardPages()
  }, [user])

  // Browser tab title per page ("Moderation · Keystone"), so open tabs and
  // history entries are distinguishable.
  useEffect(() => {
    const item = navItemForPath(pathname)
    document.title = item ? `${item.label} · Keystone` : "Keystone"
  }, [pathname])

  // Close the phone drawer on navigation; remember visited pages for the palette.
  useEffect(() => {
    setDrawerOpen(false)
    if (navItemForPath(pathname)?.to === pathname) pushRecent(pathname)
  }, [pathname])

  if (!user) {
    return <main className="flex-1">{children}</main>
  }

  const isMac = typeof navigator !== "undefined" && /mac/i.test(navigator.platform)
  const bottomItems = visibleNavLinks.filter((l) => ["/dashboard", "/dashboard/moderation", "/dashboard/honeypot", "/dashboard/tickets"].includes(l.to))

  return (
    <BreadcrumbProvider>
      <a href="#content" className="kb-skip">
        Skip to content
      </a>
      <div className="flex min-h-svh">
        <CommandPalette items={visibleNavLinks} open={paletteOpen} onOpenChange={setPaletteOpen} onShowShortcuts={showShortcuts} />
        <ShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
        {/* The one "Leave without saving?" dialog for useUnsavedChangesGuard. */}
        <UnsavedChangesDialog />

        {/* Desktop / tablet sidebar */}
        <aside
          className={cn(
            "sticky top-0 hidden h-svh shrink-0 border-r border-sidebar-border bg-sidebar transition-[width] duration-200 md:block",
            collapsed ? "w-16" : "w-[248px]"
          )}
        >
          <SidebarContent items={visibleNavLinks} collapsed={collapsed} onToggleCollapsed={toggleSidebar} />
        </aside>

        {/* Phone drawer */}
        <Sheet open={drawerOpen} onOpenChange={setDrawerOpen}>
          <SheetContent title="Navigation" className="md:hidden">
            <SidebarContent items={visibleNavLinks} onNavigate={() => setDrawerOpen(false)} />
          </SheetContent>
        </Sheet>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-border bg-card px-4 md:px-6">
            <button
              type="button"
              aria-label="Open navigation menu"
              onClick={() => setDrawerOpen(true)}
              className="-ml-2 flex size-11 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground md:hidden"
            >
              <Menu className="size-5" />
            </button>
            <div className="hidden min-w-0 flex-1 md:block">
              <Breadcrumbs />
            </div>
            <div className="min-w-0 flex-1 md:hidden">
              <PhoneTitle />
            </div>
            <button
              type="button"
              onClick={() => setPaletteOpen(true)}
              className="hidden h-9 w-[260px] items-center gap-2 rounded-lg border border-input bg-card px-3 text-sm text-muted-foreground transition-colors hover:bg-hover hover:text-foreground sm:flex"
            >
              <Search className="size-3.5" aria-hidden />
              <span className="flex-1 text-left">Search or jump to…</span>
              <Kbd>{isMac ? "⌘ K" : "Ctrl K"}</Kbd>
            </button>
            <button
              type="button"
              aria-label="Search"
              onClick={() => setPaletteOpen(true)}
              className="flex size-11 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground sm:hidden"
            >
              <Search className="size-5" />
            </button>
            <ProfileMenu onShowShortcuts={showShortcuts} />
          </header>

          <main id="content" tabIndex={-1} className="flex-1 px-4 pt-6 pb-24 outline-none md:px-8 md:pt-7 md:pb-10">
            <div className="mx-auto mb-4 w-full max-w-[1320px] empty:hidden">
              <LiveBanners />
            </div>
            {children}
          </main>
        </div>

        {/* Phone bottom nav */}
        <nav
          aria-label="Quick navigation"
          className="pb-safe fixed inset-x-0 bottom-0 z-30 flex h-16 items-stretch border-t border-border bg-card md:hidden"
        >
          {bottomItems.map((l) => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.end}
              className={({ isActive }) =>
                cn(
                  "flex min-w-0 flex-1 flex-col items-center justify-center gap-1 text-2xs font-medium transition-colors focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring",
                  isActive ? "text-sidebar-accent-foreground" : "text-muted-foreground"
                )
              }
            >
              <l.icon className="size-5" aria-hidden />
              {l.label}
            </NavLink>
          ))}
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            className="flex min-w-0 flex-1 flex-col items-center justify-center gap-1 text-2xs font-medium text-muted-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
          >
            <Ellipsis className="size-5" aria-hidden />
            More
          </button>
        </nav>
      </div>
    </BreadcrumbProvider>
  )
}
