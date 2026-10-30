import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react"
import { readStorage, writeStorage } from "@/lib/storage"

export type Density = "comfortable" | "compact"

interface PrefsState {
  density: Density
  setDensity: (d: Density) => void
  /** null = no saved choice (tablet widths default to the rail) */
  sidebarCollapsed: boolean | null
  setSidebarCollapsed: (c: boolean) => void
}

const PrefsContext = createContext<PrefsState | null>(null)

/**
 * Per-browser UI preferences (all in localStorage, all try/catch-wrapped):
 * table density (`kb-density`, applied app-wide via <html data-density>) and
 * the collapsed sidebar rail (`kb-sidebar`).
 */
export function PrefsProvider({ children }: { children: ReactNode }) {
  const [density, setDensityState] = useState<Density>(() =>
    readStorage("kb-density") === "compact" ? "compact" : "comfortable"
  )
  const [sidebarCollapsed, setCollapsed] = useState<boolean | null>(() => {
    const raw = readStorage("kb-sidebar")
    return raw === "collapsed" ? true : raw === "expanded" ? false : null
  })

  useEffect(() => {
    if (density === "compact") document.documentElement.setAttribute("data-density", "compact")
    else document.documentElement.removeAttribute("data-density")
  }, [density])

  const setDensity = useCallback((d: Density) => {
    setDensityState(d)
    writeStorage("kb-density", d)
  }, [])

  const setSidebarCollapsed = useCallback((c: boolean) => {
    setCollapsed(c)
    writeStorage("kb-sidebar", c ? "collapsed" : "expanded")
  }, [])

  const value = useMemo(
    () => ({ density, setDensity, sidebarCollapsed, setSidebarCollapsed }),
    [density, setDensity, sidebarCollapsed, setSidebarCollapsed]
  )
  return <PrefsContext.Provider value={value}>{children}</PrefsContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function usePrefs(): PrefsState {
  const ctx = useContext(PrefsContext)
  if (!ctx) throw new Error("usePrefs must be used within a PrefsProvider")
  return ctx
}
