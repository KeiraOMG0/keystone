import { useEffect, useRef } from "react"
import { useNavigate } from "react-router-dom"
import { NAV_LINKS } from "@/components/layout/navLinks"
import { useAuth } from "@/hooks/useAuth"
import { guardedNavigate } from "@/hooks/useUnsavedChangesGuard"

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  const tag = target.tagName
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable
}

/**
 * One global hotkey hook, mounted in AppShell. Ignored while typing in a
 * field, while a dialog is open, and when a modifier key is held (Ctrl/Cmd+K
 * is owned by the command palette). Shortcuts never do anything destructive.
 *
 *   ?          shortcuts dialog        /   focus the page search ([data-page-search])
 *   [          collapse/expand sidebar g then o/m/h/t   go to a page
 */
export function useHotkeys({
  onToggleSidebar,
  onShowShortcuts,
}: {
  onToggleSidebar: () => void
  onShowShortcuts: () => void
}) {
  const navigate = useNavigate()
  const { permissions } = useAuth()
  const gPending = useRef<number | null>(null)
  const latest = useRef({ onToggleSidebar, onShowShortcuts, permissions, navigate })
  useEffect(() => {
    latest.current = { onToggleSidebar, onShowShortcuts, permissions, navigate }
  })

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.ctrlKey || e.metaKey || e.altKey) return
      if (isTyping(e.target)) return
      if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return
      const { onToggleSidebar, onShowShortcuts, permissions, navigate } = latest.current

      if (gPending.current !== null) {
        window.clearTimeout(gPending.current)
        gPending.current = null
        const link = NAV_LINKS.find((l) => l.hotkey === e.key.toLowerCase())
        if (link && (!link.requiredPermission || permissions[link.requiredPermission])) {
          e.preventDefault()
          // Asks first when the current page has unsaved changes.
          guardedNavigate(link.to, () => navigate(link.to))
        }
        return
      }
      if (e.key === "g") {
        gPending.current = window.setTimeout(() => (gPending.current = null), 1200)
        return
      }
      if (e.key === "?") {
        e.preventDefault()
        onShowShortcuts()
      } else if (e.key === "[") {
        e.preventDefault()
        onToggleSidebar()
      } else if (e.key === "/") {
        const el = document.querySelector<HTMLElement>("[data-page-search]")
        if (el) {
          e.preventDefault()
          el.focus()
        }
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [])
}
