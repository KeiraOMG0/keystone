import { useQueryClient, type QueryClient } from "@tanstack/react-query"
import { useLiveTopic } from "@/live/useLive"

/**
 * Polling fallback for role lists, used ONLY while the live socket is down:
 * `refetchInterval: connected ? false : ROLES_FALLBACK_POLL_MS`.
 */
export const ROLES_FALLBACK_POLL_MS = 60_000

/**
 * True for every query key that caches guild role data:
 * - the Self-roles picker ["reaction-role-roles"],
 * - the Roles page ["role-admin", ...] (overview, members, member search),
 * - the role pickers fed by the same internal GET /roles:
 *   ["permissions", "roles"], ["qotd", "roles"], ["fortnite", "roles"],
 * - the <ResolvedRole> name/colour caches ["resolve-single", "roles", id] and
 *   batched ["resolve", userIds, roleIds, channelIds] keys that carry role ids.
 *   Those resolve caches have a 5 min staleTime, so without this a renamed or
 *   recoloured role kept its old look on panel entries.
 * Used by invalidateRoleQueries AND by LiveProvider's reconnect resync.
 */
export function isRoleQueryKey(key: readonly unknown[]): boolean {
  const [root, second] = key
  if (root === "reaction-role-roles" || root === "role-admin") return true
  if (root === "automation" && (second === "autorole-roles" || second === "autoroles")) return true
  if (second === "roles" && (root === "permissions" || root === "qotd" || root === "fortnite" || root === "resolve-single")) {
    return true
  }
  return root === "resolve" && Array.isArray(key[2]) && key[2].length > 0
}

/** Re-fetch every cached copy of guild role data (see isRoleQueryKey). Idempotent. */
export function invalidateRoleQueries(qc: QueryClient): Promise<void> {
  return qc.invalidateQueries({ predicate: (q) => isRoleQueryKey(q.queryKey) })
}

/**
 * LIVE `roles` topic (requires "manage_reaction_roles"; staff bypass): the bot
 * publishes `{action, role_id, role_ids, count}` on guild role create / update
 * (incl. reorders) / delete, coalesced so a 30-role drag is one message. Any
 * message re-fetches every role query above. Mounted by the Self-roles,
 * Permissions and Roles pages (the Roles page through useRoleAdminLive in
 * api/roleAdmin.ts). The QOTD and Fortnite role pickers do NOT subscribe (their
 * viewers may lack manage_reaction_roles); their keys are only refreshed when
 * an event arrives via another page or on reconnect resync. Pass
 * `enabled: false` for users who lack the permission so the hub does not
 * answer with a `forbidden` error.
 */
export function useRolesLive(opts?: { enabled?: boolean }) {
  const qc = useQueryClient()
  useLiveTopic(
    "roles",
    () => {
      void invalidateRoleQueries(qc)
    },
    opts
  )
}
