import type { ReactNode } from "react"
import { useLocation } from "react-router-dom"

// Wraps routed page content with a short fade (150 ms, fade only) that
// re-triggers on navigation. Keyed by pathname so React remounts it on every
// route change. Disabled under prefers-reduced-motion via motion-safe.
export function PageTransition({ children }: { children: ReactNode }) {
  const location = useLocation()
  return (
    <div key={location.pathname} className="motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-150">
      {children}
    </div>
  )
}
