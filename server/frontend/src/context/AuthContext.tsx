import { createContext, useMemo, type ReactNode } from "react"
import { classifyMeError, useMe } from "@/api/me"
import type { MeResponse } from "@/types/api"
import { AUTOMATION_ANY_PERMISSION, AUTOMATION_PERMISSIONS, OWNER_ONLY_PERMISSION, STAFF_ONLY_PERMISSION, MODERATION_ANY_PERMISSION, MODERATION_PERMISSIONS } from "@/lib/owner"

/**
 * The single source of truth for the auth states the whole app's route
 * guards consult. Derived from ONE call to GET /api/me on app mount (via
 * useMe()).
 *
 * Mapping (per server/routes/me.py / server/permissions/guards.py's actual
 * response shapes):
 *   - query in flight                                    -> "loading"
 *   - 401 {"error":"unauthenticated"}                     -> "unauthenticated"
 *   - 403 {"error":"not_a_member"}                        -> "not_a_member"
 *   - anything else (network error, 502/503/504, Cloudflare 52x HTML pages,
 *     502 {"error":"internal_api_unavailable"}, non-JSON bodies) is TRANSIENT:
 *     retried forever, never logs the user out. Before the first successful
 *     load -> "disconnected"; after it, the last-known user stays
 *     "authenticated" and `reconnecting` is true.
 *   - 200 with user data                                  -> "authenticated"
 *
 * "disconnected" is distinct from "unauthenticated": it means the browser
 * couldn't reach the backend at all, or the backend couldn't reach the bot
 * process — not that the session is actually invalid. Previously this was
 * collapsed into "unauthenticated", which bounced a logged-in user to the
 * login screen any time their internet blipped or the bot process
 * restarted. Route guards should keep showing the last-known page (or a
 * "reconnecting" notice) for "disconnected" rather than redirecting.
 */
export type AuthState = "loading" | "unauthenticated" | "not_a_member" | "disconnected" | "authenticated"

interface AuthContextValue {
  state: AuthState
  user: MeResponse["user"] | null
  permissions: Record<string, boolean>
  roleIds: string[]
  guildId: string | null
  /** GET /api/me `is_staff` (owner or STAFF_ROLE_IDS; missing = false). Same as permissions[STAFF_ONLY_PERMISSION]. */
  isStaff: boolean
  /** GET /api/me `is_owner`. Same as permissions[OWNER_ONLY_PERMISSION]. */
  isOwner: boolean
  /**
   * True while /api/me is failing transiently and being retried (site/bot/
   * tunnel outage). The session is NOT known to be invalid; RequireAuth shows
   * the "reconnecting..." banner but keeps the last-known page.
   */
  reconnecting: boolean
}

export const AuthContext = createContext<AuthContextValue | undefined>(undefined)

const EMPTY: Pick<AuthContextValue, "user" | "permissions" | "roleIds" | "guildId" | "isStaff" | "isOwner"> = {
  user: null,
  permissions: {},
  roleIds: [],
  guildId: null,
  isStaff: false,
  isOwner: false,
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const meQuery = useMe()

  const value = useMemo<AuthContextValue>(() => {
    const reconnecting = meQuery.failureCount > 0

    if (meQuery.isError) {
      // Only reached for non-transient failures (transient ones retry forever).
      const verdict = classifyMeError(meQuery.error)
      if (verdict === "not_a_member") return { state: "not_a_member", ...EMPTY, reconnecting: false }
      if (verdict === "unauthenticated") return { state: "unauthenticated", ...EMPTY, reconnecting: false }
    }

    const data = meQuery.data
    if (data) {
      // Keep the last-known identity through any transient refetch failure.
      const isOwner = data.is_owner === true
      const isStaff = data.is_staff === true
      return {
        state: "authenticated",
        user: data.user,
        // Pseudo-permissions (lib/owner.ts): owner-only for the hidden Debug nav
        // item, staff-only for the require_staff pages (Diagnostics, Roles,
        // Permissions, Settings). Never real bot permission strings.
        permissions: {
          ...data.permissions,
          ...(isOwner ? { [OWNER_ONLY_PERMISSION]: true } : {}),
          ...(isStaff ? { [STAFF_ONLY_PERMISSION]: true } : {}),
          // Automation nav item: any of its tab permissions (lib/owner.ts).
          ...(isStaff || AUTOMATION_PERMISSIONS.some((p) => data.permissions?.[p]) ? { [AUTOMATION_ANY_PERMISSION]: true } : {}),
          // Moderation nav item: any of moderate/kick/ban (lib/owner.ts).
          ...(isStaff || MODERATION_PERMISSIONS.some((p) => data.permissions?.[p]) ? { [MODERATION_ANY_PERMISSION]: true } : {}),
        },
        isStaff,
        isOwner,
        roleIds: data.role_ids,
        guildId: data.guild_id,
        reconnecting,
      }
    }

    // Never loaded yet: still loading, or failing transiently (outage before
    // the first successful load): "disconnected", never "unauthenticated".
    if (reconnecting || meQuery.isError) return { state: "disconnected", ...EMPTY, reconnecting: true }
    return { state: "loading", ...EMPTY, reconnecting: false }
  }, [meQuery.isError, meQuery.error, meQuery.data, meQuery.failureCount])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
