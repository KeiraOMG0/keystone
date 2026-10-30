import { createContext, useContext, useEffect, useState, type ReactNode } from "react"

const STORAGE_KEY = "dashboard:developer-mode"

function readStored(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === "true"
  } catch {
    return false
  }
}

interface DeveloperModeState {
  enabled: boolean
  toggle: () => void
}

const DeveloperModeContext = createContext<DeveloperModeState | null>(null)

export function DeveloperModeProvider({ children }: { children: ReactNode }) {
  const [enabled, setEnabled] = useState(readStored)

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, String(enabled))
    } catch {
      // private browsing / storage disabled — dev mode just won't persist
    }
  }, [enabled])

  return (
    <DeveloperModeContext.Provider value={{ enabled, toggle: () => setEnabled((v) => !v) }}>
      {children}
    </DeveloperModeContext.Provider>
  )
}

export function useDeveloperMode(): DeveloperModeState {
  const ctx = useContext(DeveloperModeContext)
  if (!ctx) throw new Error("useDeveloperMode must be used within a DeveloperModeProvider")
  return ctx
}
