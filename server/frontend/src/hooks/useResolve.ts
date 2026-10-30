import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query"
import { resolveIds } from "@/api/resolve"
import type { ResolvedChannel, ResolvedRole, ResolvedUser } from "@/types/api"
import type { QueryClient } from "@tanstack/react-query"

interface UseResolvedEntitiesInput {
  userIds?: string[]
  roleIds?: string[]
  channelIds?: string[]
}

interface ResolvedEntities {
  users: Map<string, ResolvedUser | null>
  roles: Map<string, ResolvedRole | null>
  channels: Map<string, ResolvedChannel | null>
  isLoading: boolean
}

function sortedUnique(ids: string[] | undefined): string[] {
  if (!ids || ids.length === 0) return []
  return Array.from(new Set(ids)).sort()
}

/**
 * Shared resolver hook — pass every id a page/component tree needs resolved
 * in ONE call (e.g. all moderator_ids + user_ids collected from a whole list
 * of rows) so they land in a single query key and a single network request.
 * This is the low-level primitive; prefer the single-id convenience hooks
 * below for individual `<ResolvedUser>`-style components, which batch
 * themselves via a microtask scheduler rather than requiring callers to
 * pre-collect ids.
 */
export function useResolvedEntities(input: UseResolvedEntitiesInput): ResolvedEntities {
  const userIds = sortedUnique(input.userIds)
  const roleIds = sortedUnique(input.roleIds)
  const channelIds = sortedUnique(input.channelIds)

  const enabled = userIds.length > 0 || roleIds.length > 0 || channelIds.length > 0

  const query = useQuery({
    queryKey: ["resolve", userIds, roleIds, channelIds] as const,
    queryFn: () => resolveIds({ userIds, roleIds, channelIds }),
    enabled,
    staleTime: 5 * 60 * 1000,
  })

  const data = query.data
  return {
    users: new Map(data ? Object.entries(data.users) : []),
    roles: new Map(data ? Object.entries(data.roles) : []),
    channels: new Map(data ? Object.entries(data.channels) : []),
    isLoading: enabled && query.isLoading,
  }
}

interface SingleResult<T> {
  data: T | null | undefined
  isLoading: boolean
}

type EntityKind = "users" | "roles" | "channels"

/**
 * Microtask batcher: every mounted <ResolvedUser>/<ResolvedRole>/
 * <ResolvedChannel> calls useResolvedSingle, which enqueues its id into a
 * per-kind pending set instead of firing its own fetch. A single
 * queueMicrotask callback (scheduled once per kind per tick, regardless of
 * how many components enqueue) does the actual network call after every
 * component from this render pass has had a chance to enqueue — React
 * effects (where react-query's observers subscribe/fetch) run synchronously
 * in a batch after render but BEFORE any queued microtask runs, so by the
 * time the flush fires, the pending set already contains every id from this
 * commit. Each waiting component gets its value pushed into it directly via
 * a resolver map rather than re-deriving from react-query cache timing.
 */
const pendingIds: Record<EntityKind, Set<string>> = {
  users: new Set(),
  roles: new Set(),
  channels: new Set(),
}
const flushScheduled: Record<EntityKind, boolean> = {
  users: false,
  roles: false,
  channels: false,
}
interface Waiter {
  resolve: (values: Record<string, unknown>) => void
  reject: (error: unknown) => void
}
const waiters: Record<EntityKind, Waiter[]> = {
  users: [],
  roles: [],
  channels: [],
}

/** A resolve id the server did not answer for at all (neither a value nor null). */
class ResolveMissingError extends Error {
  constructor(id: string) {
    super(`No resolve result for ${id}`)
    this.name = "ResolveMissingError"
  }
}

function enqueueAndFlush(
  kind: EntityKind,
  id: string,
  queryClient: QueryClient
): Promise<unknown> {
  pendingIds[kind].add(id)

  const promise = new Promise<Record<string, unknown>>((resolve, reject) => {
    waiters[kind].push({ resolve, reject })
  })

  if (!flushScheduled[kind]) {
    flushScheduled[kind] = true
    queueMicrotask(() => {
      flushScheduled[kind] = false
      const batch = Array.from(pendingIds[kind])
      pendingIds[kind].clear()
      const resolvers = waiters[kind]
      waiters[kind] = []

      resolveIds({
        userIds: kind === "users" ? batch : [],
        roleIds: kind === "roles" ? batch : [],
        channelIds: kind === "channels" ? batch : [],
      })
        .then((result) => {
          const values = result[kind]
          for (const [batchId, value] of Object.entries(values)) {
            queryClient.setQueryData(["resolve-single", kind, batchId], value ?? null)
          }
          resolvers.forEach((w) => w.resolve(values))
        })
        .catch((error: unknown) => {
          // Transient failure (bot restarting, 502, network): REJECT so nothing
          // is cached as "not found". The query retries with backoff and the
          // components show the raw id meanwhile, never "(user left)".
          resolvers.forEach((w) => w.reject(error))
        })
    })
  }

  return promise.then((values) => {
    // Only an explicit null from the server is a definitive "not found".
    if (!(id in values)) throw new ResolveMissingError(id)
    return values[id] ?? null
  })
}

/** Definitive "not found" (null) is re-checked after a minute; a real entity is cached for 5. */
const FOUND_STALE_MS = 5 * 60 * 1000
const NOT_FOUND_STALE_MS = 60 * 1000

function resolveSingleOptions(kind: EntityKind, id: string, queryClient: QueryClient) {
  return {
    queryKey: ["resolve-single", kind, id] as const,
    queryFn: () => enqueueAndFlush(kind, id, queryClient),
    staleTime: (query: { state: { data: unknown } }) => (query.state.data === null ? NOT_FOUND_STALE_MS : FOUND_STALE_MS),
    // Failures are transient by construction (see enqueueAndFlush): retry a few
    // times with backoff, then show the raw id until the next mount/refetch.
    retry: 3,
    retryDelay: (attempt: number) => Math.min(2_000 * 2 ** attempt, 20_000),
  }
}

function useResolvedSingle<T>(kind: EntityKind, id: string | null | undefined): SingleResult<T> {
  const queryClient = useQueryClient()

  const query = useQuery({
    ...resolveSingleOptions(kind, id ?? "", queryClient),
    enabled: Boolean(id),
  })

  if (!id) return { data: undefined, isLoading: false }
  return { data: query.data as T | null | undefined, isLoading: query.isFetching && query.data === undefined }
}

/** undefined = still loading, nothing requested yet, OR the lookup failed
 *  transiently (show the raw id); null = the server definitively answered
 *  "not found" (e.g. the user left the guild). */
export function useResolvedUser(id: string | null | undefined): SingleResult<ResolvedUser> {
  return useResolvedSingle<ResolvedUser>("users", id)
}

export function useResolvedRole(id: string | null | undefined): SingleResult<ResolvedRole> {
  return useResolvedSingle<ResolvedRole>("roles", id)
}

export function useResolvedChannel(id: string | null | undefined): SingleResult<ResolvedChannel> {
  return useResolvedSingle<ResolvedChannel>("channels", id)
}

/**
 * Many user ids at once, sharing the SAME cache entries (and batched requests)
 * as <ResolvedUser>, so a list that already renders the users costs no extra
 * request. Values: ResolvedUser, null (definitively not found) or undefined
 * (loading / transient failure). Used for client-side name search.
 */
export function useResolvedUsersMap(ids: string[]): Map<string, ResolvedUser | null | undefined> {
  const queryClient = useQueryClient()
  const unique = sortedUnique(ids)
  return useQueries({
    queries: unique.map((id) => resolveSingleOptions("users", id, queryClient)),
    combine: (results) => {
      const map = new Map<string, ResolvedUser | null | undefined>()
      results.forEach((r, i) => map.set(unique[i], r.data as ResolvedUser | null | undefined))
      return map
    },
  })
}

/** Case-insensitive match of a lower-cased query against a resolved user's display name and username. */
export function userMatches(user: ResolvedUser | null | undefined, lowerQuery: string): boolean {
  if (!user) return false
  return user.display_name.toLowerCase().includes(lowerQuery) || user.username.toLowerCase().includes(lowerQuery)
}
