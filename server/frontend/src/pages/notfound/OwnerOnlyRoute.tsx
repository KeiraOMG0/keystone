import type { ReactNode } from "react"
import { useAuth } from "@/hooks/useAuth"
import NotFound404 from "@/pages/notfound/NotFound404"

/**
 * Route-level gate for hidden owner-only pages (/dashboard/debug). Non-owners
 * get exactly the catch-all NotFound404 an unknown /dashboard/* path renders:
 * the page's lazy chunk is never loaded and none of its API calls are made,
 * so nothing distinguishes it from a path that does not exist. The server
 * still enforces it (require_owner answers 404).
 */
export function OwnerOnlyRoute({ children }: { children: ReactNode }) {
  const { isOwner } = useAuth()
  return isOwner ? <>{children}</> : <NotFound404 />
}
