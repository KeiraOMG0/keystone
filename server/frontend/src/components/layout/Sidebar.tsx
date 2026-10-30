import { NavLink } from "react-router-dom"
import { useQueryClient } from "@tanstack/react-query"
import { PanelLeftClose, PanelLeftOpen } from "lucide-react"
import { NAV_GROUPS, type NavItem } from "@/components/layout/navLinks"
import { SidebarLiveRows } from "@/components/patterns/LivePill"
import { CountBadge } from "@/components/ui/badge"
import { Tip } from "@/components/ui/tooltip"
import { Kbd } from "@/components/ui/kbd"
import { useTickets } from "@/api/tickets"
import { useHoneypotAppeals } from "@/api/honeypot"
import { useLiveTopic } from "@/live/useLive"
import { invalidateSoon } from "@/live/invalidate"
import { cn } from "@/lib/utils"

/** Open-ticket count (data Tickets already fetches; only mounted when the user has manage_tickets). */
function useTicketCount(): number | null {
  const qc = useQueryClient()
  // Keep the count fresh even when the Tickets page is not mounted. Same socket,
  // reference-counted topic subscription; the handler just re-fetches the lists
  // (coalesced with the Tickets page's own handler: one refetch per event).
  useLiveTopic("tickets", () => {
    invalidateSoon(qc, ["tickets"])
  })
  const { data } = useTickets("open")
  return data ? data.length : null
}

/** Pending-appeal count (only mounted when the user has moderate_members). */
function useAppealCount(): number | null {
  const qc = useQueryClient()
  useLiveTopic("honeypot", (msg) => {
    const kind = (msg.data as { kind?: string }).kind
    if (kind === "appeal_submitted" || kind === "appeal_approved" || kind === "appeal_denied") {
      invalidateSoon(qc, ["honeypot", "appeals"])
    }
  })
  const { data } = useHoneypotAppeals("pending")
  return data ? data.appeals.length : null
}

function TicketsBadge({ collapsed }: { collapsed: boolean }) {
  return <Badge count={useTicketCount()} collapsed={collapsed} label="open tickets" />
}
function AppealsBadge({ collapsed }: { collapsed: boolean }) {
  return <Badge count={useAppealCount()} collapsed={collapsed} label="pending appeals" />
}

function Badge({ count, collapsed, label }: { count: number | null; collapsed: boolean; label: string }) {
  if (!count) return null
  if (collapsed) {
    return <span aria-label={`${count} ${label}`} className="absolute top-1 right-1 size-2 rounded-full bg-primary" />
  }
  return (
    <>
      <CountBadge count={count} className="ml-auto" />
      <span className="sr-only">{label}</span>
    </>
  )
}

function NavRow({ link, collapsed, onNavigate }: { link: NavItem; collapsed: boolean; onNavigate?: () => void }) {
  const row = (
    <NavLink
      to={link.to}
      end={link.end}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          "relative flex h-9 items-center gap-2.5 rounded-lg px-2.5 text-base font-medium transition-colors duration-[120ms] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-sidebar-ring max-md:h-11",
          collapsed && "justify-center px-0",
          isActive
            ? "bg-sidebar-accent text-sidebar-accent-foreground"
            : "text-sidebar-foreground hover:bg-hover hover:text-foreground"
        )
      }
    >
      <link.icon className="size-4 shrink-0" aria-hidden />
      {collapsed ? <span className="sr-only">{link.label}</span> : <span className="truncate">{link.label}</span>}
      {link.badge === "tickets" ? <TicketsBadge collapsed={collapsed} /> : null}
      {link.badge === "appeals" ? <AppealsBadge collapsed={collapsed} /> : null}
    </NavLink>
  )
  return collapsed ? (
    <Tip label={link.label} side="right">
      {row}
    </Tip>
  ) : (
    row
  )
}

/**
 * Sidebar content (used by the desktop sidebar, the collapsed rail and the
 * phone drawer): brand block, four permission-filtered groups, live/bot footer.
 */
export function SidebarContent({
  items,
  collapsed = false,
  onNavigate,
  onToggleCollapsed,
}: {
  items: NavItem[]
  collapsed?: boolean
  onNavigate?: () => void
  /** Renders the collapse/expand button when provided (desktop only). */
  onToggleCollapsed?: () => void
}) {
  return (
    <div className="flex h-full flex-col">
      <div className={cn("flex h-14 shrink-0 items-center gap-2.5 border-b border-sidebar-border px-4", collapsed && "justify-center px-0")}>
        {onToggleCollapsed && collapsed ? (
          // The collapsed rail swaps the logo for the expand button so the toggle stays where the collapse button was.
          <Tip label={<span className="flex items-center gap-1.5">Expand sidebar <Kbd className="border-transparent bg-transparent text-background">[</Kbd></span>} side="right">
            <button
              type="button"
              aria-label="Expand sidebar"
              onClick={onToggleCollapsed}
              className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-sidebar-border text-muted-foreground transition-colors hover:bg-hover hover:text-foreground focus-visible:outline-2 focus-visible:outline-sidebar-ring"
            >
              <PanelLeftOpen className="size-4" />
            </button>
          </Tip>
        ) : (
          <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-sm font-semibold text-primary-foreground" aria-hidden>
            K
          </div>
        )}
        {!collapsed ? (
          <div className="min-w-0 flex-1 leading-tight">
            <p className="truncate text-md font-semibold text-foreground">Kick-Bot</p>
            <p className="truncate text-xs text-subtle-foreground">Moderator dashboard</p>
          </div>
        ) : null}
        {onToggleCollapsed && !collapsed ? (
          <Tip label={<span className="flex items-center gap-1.5">Collapse sidebar <Kbd className="border-transparent bg-transparent text-background">[</Kbd></span>} side="right">
            <button
              type="button"
              aria-label="Collapse sidebar"
              onClick={onToggleCollapsed}
              className="rounded-md p-1.5 text-subtle-foreground transition-colors hover:bg-hover hover:text-foreground"
            >
              <PanelLeftClose className="size-4" />
            </button>
          </Tip>
        ) : null}
      </div>

      <nav aria-label="Main" className="flex flex-1 flex-col gap-4 overflow-y-auto p-3">
        {NAV_GROUPS.map((g) => {
          const groupItems = items.filter((i) => i.group === g.key)
          if (groupItems.length === 0) return null
          return (
            <div key={g.key} className="flex flex-col gap-0.5">
              {g.title && !collapsed ? (
                <span className="px-2.5 pb-1 text-2xs font-semibold tracking-[0.06em] text-subtle-foreground uppercase">
                  {g.title}
                </span>
              ) : null}
              {g.title && collapsed ? <hr className="mx-2 mb-1 border-sidebar-border" /> : null}
              {groupItems.map((link) => (
                <NavRow key={link.to} link={link} collapsed={collapsed} onNavigate={onNavigate} />
              ))}
            </div>
          )
        })}
      </nav>

      <div className="shrink-0 border-t border-sidebar-border p-3">
        <SidebarLiveRows collapsed={collapsed} />
      </div>
    </div>
  )
}
