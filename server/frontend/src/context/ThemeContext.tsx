import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react"
import { readStorage, writeStorage } from "@/lib/storage"

export type ThemePreference = "light" | "dark" | "system"
export type ResolvedTheme = "light" | "dark"

const STORAGE_KEY = "kb-theme"

interface ThemeState {
  theme: ThemePreference
  resolved: ResolvedTheme
  setTheme: (t: ThemePreference) => void
}

const ThemeContext = createContext<ThemeState | null>(null)

function systemPrefersDark(): boolean {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
  } catch {
    return false
  }
}

function readPreference(): ThemePreference {
  const raw = readStorage(STORAGE_KEY)
  return raw === "light" || raw === "dark" || raw === "system" ? raw : "system"
}

/**
 * Theme preference (light | dark | system), persisted in localStorage["kb-theme"].
 * The `dark` class is first applied before paint by the inline script in
 * index.html; this provider keeps it in sync afterwards and follows OS changes
 * while the preference is "system".
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemePreference>(readPreference)
  const [systemDark, setSystemDark] = useState(systemPrefersDark)

  useEffect(() => {
    let mq: MediaQueryList
    try {
      mq = window.matchMedia("(prefers-color-scheme: dark)")
    } catch {
      return
    }
    const onChange = () => setSystemDark(mq.matches)
    mq.addEventListener("change", onChange)
    return () => mq.removeEventListener("change", onChange)
  }, [])

  const resolved: ResolvedTheme = theme === "system" ? (systemDark ? "dark" : "light") : theme

  useEffect(() => {
    document.documentElement.classList.toggle("dark", resolved === "dark")
  }, [resolved])

  const setTheme = useCallback((t: ThemePreference) => {
    setThemeState(t)
    writeStorage(STORAGE_KEY, t)
  }, [])

  const value = useMemo(() => ({ theme, resolved, setTheme }), [theme, resolved, setTheme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useTheme(): ThemeState {
  const ctx = useContext(ThemeContext)
  if (!ctx) throw new Error("useTheme must be used within a ThemeProvider")
  return ctx
}
