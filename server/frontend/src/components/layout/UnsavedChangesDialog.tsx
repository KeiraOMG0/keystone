import { useEffect } from "react"
import { useLocation, useNavigate } from "react-router-dom"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import {
  performLeave,
  registerLeaveHost,
  resolvePendingLeave,
  trackCommittedLocation,
  usePendingLeave,
} from "@/hooks/useUnsavedChangesGuard"

/**
 * The single "Leave without saving?" dialog for useUnsavedChangesGuard,
 * mounted once in AppShell. It also tells the guard which location React
 * Router has committed (needed to undo a guarded Back/Forward move).
 */
export function UnsavedChangesDialog() {
  const pending = usePendingLeave()
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => registerLeaveHost(), [])
  useEffect(() => {
    trackCommittedLocation(location.pathname)
  }, [location.key, location.pathname])

  return (
    <ConfirmDialog
      open={pending !== null}
      onOpenChange={(open) => {
        if (!open) resolvePendingLeave()
      }}
      title="Leave without saving?"
      description={pending?.message ?? ""}
      confirmLabel="Leave page"
      onConfirm={() => {
        const p = resolvePendingLeave()
        // Let the dialog close first, then navigate (Radix restores focus on close).
        if (p) window.setTimeout(() => performLeave(p.action, navigate), 0)
      }}
    />
  )
}
