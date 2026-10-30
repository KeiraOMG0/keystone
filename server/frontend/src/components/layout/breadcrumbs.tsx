import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react"
import { Link, useLocation } from "react-router-dom"
import { ChevronRight } from "lucide-react"
import { navItemForPath } from "@/components/layout/navLinks"
import { useAuth } from "@/hooks/useAuth"
import { OWNER_ONLY_PERMISSION } from "@/lib/owner"

const CrumbContext = createContext<{ label: string | null; setLabel: (l: string | null) => void }>({
  label: null,
  setLabel: () => {},
})

export function BreadcrumbProvider({ children }: { children: ReactNode }) {
  const [label, setLabel] = useState<string | null>(null)
  const value = useMemo(() => ({ label, setLabel }), [label])
  return <CrumbContext.Provider value={value}>{children}</CrumbContext.Provider>
}

/**
 * Detail pages call `useBreadcrumb("#0141")` (or the template name) to name the
 * last crumb. Pass null/undefined while loading. List pages need nothing: the
 * section crumb comes from NAV_LINKS.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function useBreadcrumb(label: string | null | undefined): void {
  const { setLabel } = useContext(CrumbContext)
  useEffect(() => {
    setLabel(label ?? null)
    return () => setLabel(null)
  }, [label, setLabel])
}

/** Current crumb trail: Dashboard / Section / Page. */
// eslint-disable-next-line react-refresh/only-export-components
export function useCrumbs(): { label: string; to?: string }[] {
  const { pathname } = useLocation()
  const { label } = useContext(CrumbContext)
  const { isOwner } = useAuth()
  const match = navItemForPath(pathname)
  // A hidden owner-only page must not name itself to anyone else: its crumb
  // trail is then identical to an unknown path ("Dashboard / Not found").
  const item = match && match.requiredPermission === OWNER_ONLY_PERMISSION && !isOwner ? undefined : match
  const crumbs: { label: string; to?: string }[] = [{ label: "Dashboard", to: "/dashboard" }]
  if (item && item.to !== "/dashboard") crumbs.push({ label: item.label, to: item.to })
  else if (item) crumbs.push({ label: item.label })
  if (label) crumbs.push({ label })
  // last crumb is the current page: no link
  const last = crumbs[crumbs.length - 1]
  if (last) delete last.to
  return crumbs
}

export function Breadcrumbs() {
  const crumbs = useCrumbs()
  return (
    <nav aria-label="Breadcrumb" className="min-w-0">
      <ol className="flex items-center gap-1.5 text-sm text-muted-foreground">
        {crumbs.map((c, i) => (
          <li key={i} className="flex min-w-0 items-center gap-1.5">
            {i > 0 ? <ChevronRight className="size-3.5 shrink-0 text-subtle-foreground" aria-hidden /> : null}
            {c.to ? (
              <Link to={c.to} className="truncate rounded-sm hover:text-foreground hover:underline">
                {c.label}
              </Link>
            ) : (
              <span aria-current="page" className="truncate font-medium text-foreground">
                {c.label}
              </span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  )
}
