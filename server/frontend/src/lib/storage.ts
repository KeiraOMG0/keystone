import { useCallback, useState } from "react"

/** localStorage read that never throws (private windows, blocked storage). */
export function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // not persisted: the preference just lasts for this page view
  }
}

/**
 * useState backed by localStorage for small per-browser preferences (sidebar
 * collapsed, last tab...). `allowed` guards against stale/garbage values.
 */
export function usePersistentString<T extends string>(
  key: string,
  fallback: T,
  allowed?: readonly T[]
): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => {
    const raw = readStorage(key)
    if (raw === null) return fallback
    if (allowed && !(allowed as readonly string[]).includes(raw)) return fallback
    return raw as T
  })
  const set = useCallback(
    (v: T) => {
      setValue(v)
      writeStorage(key, v)
    },
    [key]
  )
  return [value, set]
}

/** Last 5 visited dashboard pages, for the command palette "Recent" group. */
const RECENT_KEY = "kb-recent"
export function readRecent(): string[] {
  try {
    const parsed = JSON.parse(readStorage(RECENT_KEY) ?? "[]")
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string").slice(0, 5) : []
  } catch {
    return []
  }
}
export function pushRecent(path: string): void {
  const next = [path, ...readRecent().filter((p) => p !== path)].slice(0, 5)
  writeStorage(RECENT_KEY, JSON.stringify(next))
}
