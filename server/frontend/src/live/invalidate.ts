import type { QueryClient, QueryKey } from "@tanstack/react-query"

/**
 * Coalesced invalidation for live-event handlers.
 *
 * Several components can subscribe to the same topic at once (the sidebar
 * badge AND the page itself both listen to `tickets` / `honeypot`), and a
 * burst of events can arrive within a few milliseconds. Calling
 * `invalidateQueries` from every handler made react-query cancel and restart
 * the same fetch, i.e. duplicate requests. Handlers call `invalidateSoon`
 * instead: keys are collected for a short window, de-duplicated (a key that
 * another collected key is a prefix of is dropped, because invalidation is
 * prefix-matching), and each remaining key is invalidated ONCE.
 *
 * The window is short (50 ms), so a page still updates "live"; every event in
 * the window is covered because the single refetch starts after all of them.
 */
const WINDOW_MS = 50

const pending = new Map<QueryClient, Map<string, QueryKey>>()

function isPrefix(prefix: QueryKey, key: QueryKey): boolean {
  if (prefix.length > key.length) return false
  return prefix.every((part, i) => JSON.stringify(part) === JSON.stringify(key[i]))
}

function flush(qc: QueryClient): void {
  const keys = pending.get(qc)
  pending.delete(qc)
  if (!keys) return
  const list = [...keys.values()]
  const roots = list.filter((key) => !list.some((other) => other !== key && other.length < key.length && isPrefix(other, key)))
  for (const queryKey of roots) void qc.invalidateQueries({ queryKey })
}

export function invalidateSoon(qc: QueryClient, ...queryKeys: QueryKey[]): void {
  let keys = pending.get(qc)
  if (!keys) {
    keys = new Map()
    pending.set(qc, keys)
    setTimeout(() => flush(qc), WINDOW_MS)
  }
  for (const key of queryKeys) keys.set(JSON.stringify(key), key)
}
