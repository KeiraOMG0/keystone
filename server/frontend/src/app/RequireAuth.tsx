import type { ReactNode } from "react"
import { Navigate } from "react-router-dom"
import { useAuth } from "@/hooks/useAuth"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { Skeleton } from "@/components/ui/skeleton"

/**
 * Route guard wrapping protected routes (e.g. /dashboard). Consults the
 * single AuthContext state produced from ONE GET /api/me call:
 *
 *   loading         -> render a loading skeleton, not the page
 *   unauthenticated  -> redirect to "/" (the homepage/login)
 *   not_a_member     -> render <Forbidden403> IN PLACE of the page
 *                       (not a redirect — the user should see a real page
 *                       explaining they're not in the server)
 *   disconnected     -> render the page anyway (stale/cached data if any)
 *                       with a reconnecting banner on top — NOT a redirect.
 *                       This is a network/backend-outage state, not a real
 *                       auth failure, so kicking the user to the login
 *                       screen here would be wrong (see AuthContext.tsx).
 *   authenticated    -> render the actual page content (children)
 *
 * This step only checks auth-LEVEL state (any authenticated guild member).
 * Future permission-gated pages (e.g. Moderation, Tickets) would compose a
 * <RequireRole permission="manage_tickets"> wrapper INSIDE this one — i.e.
 * <RequireAuth><RequireRole permission="..."><Page /></RequireRole></RequireAuth> —
 * so auth-level and permission-level checks stay separate concerns instead
 * of being collapsed into one big guard.
 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { state, reconnecting } = useAuth()

  if (state === "loading") {
    return (
      <div className="mx-auto flex min-h-svh max-w-[1200px] flex-col gap-4 px-4 py-8 sm:px-8" aria-busy="true" role="status" aria-label="Loading">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    )
  }

  if (state === "unauthenticated") {
    return <Navigate to="/" replace />
  }

  if (state === "not_a_member") {
    return <Forbidden403 reason="not_a_member" />
  }

  if (state === "disconnected") {
    return (
      <>
        <div role="status" className="sticky top-0 z-50 w-full border-b border-border bg-warning-soft px-4 py-2 text-center text-sm font-medium text-warning">
          Can't reach the server right now — reconnecting…
        </div>
        {children}
      </>
    )
  }

  if (reconnecting) {
    // Signed in, but /api/me is failing transiently (site/bot/tunnel outage).
    // Keep the page, show the banner, keep retrying in the background.
    return (
      <>
        <div role="status" className="sticky top-0 z-50 w-full border-b border-border bg-warning-soft px-4 py-2 text-center text-sm font-medium text-warning">
          Can't reach the server right now — reconnecting…
        </div>
        {children}
      </>
    )
  }

  return <>{children}</>
}
