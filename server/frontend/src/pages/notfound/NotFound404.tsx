import { Link } from "react-router-dom"
import { Compass } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { useBreadcrumb } from "@/components/layout/breadcrumbs"

/**
 * "Page not found". Rendered by the catch-all routes in app/routes.tsx:
 *   - in-shell (default) for any unknown /dashboard/* path, AND for the hidden
 *     owner-only /dashboard/debug when the viewer is not the owner
 *     (OwnerOnlyRoute), so the two are indistinguishable: same page, same
 *     "Dashboard / Not found" breadcrumb (breadcrumbs.tsx hides owner-only
 *     nav items from non-owners), no page chunk loaded, no API call made;
 *   - `standalone` (no shell, links home) for any other unknown URL.
 */
export default function NotFound404({ standalone = false }: { standalone?: boolean }) {
  useBreadcrumb("Not found")
  return (
    <div
      className={
        standalone
          ? "flex min-h-svh flex-col items-center justify-center gap-6 bg-background px-4 text-center"
          : "flex min-h-[60vh] flex-col items-center justify-center gap-6 px-4 text-center"
      }
    >
      <div className="flex max-w-md flex-col items-center gap-3">
        <div className="flex size-[52px] items-center justify-center rounded-2xl bg-muted text-muted-foreground">
          <Compass className="size-6" aria-hidden />
        </div>
        <Badge variant="outline">404, Not found</Badge>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">This page does not exist</h1>
        <p className="text-base text-muted-foreground">
          {standalone ? "Check the address, or head back to the start page." : "Check the address, or head back to the overview."}
        </p>
      </div>
      <Button asChild variant="secondary">
        {standalone ? <Link to="/">Back to home</Link> : <Link to="/dashboard">Back to Overview</Link>}
      </Button>
    </div>
  )
}
