import type { ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"

/**
 * Sticky save bar for edit forms (Settings, Honeypot settings, QOTD schedule,
 * Template editor): "N unsaved changes", Discard, Save. Sticks to the bottom of
 * the content area (above the phone bottom nav) and only demands attention
 * when dirty. Save is disabled while clean, or while `blockedReason` is set
 * (shown as the button title and beside the buttons).
 *
 * Every SaveBar also guards its page while dirty (useUnsavedChangesGuard:
 * links, palette, hotkeys, Back/Forward, reload and tab close ask first), so a
 * form that uses SaveBar needs no guard of its own. `guard={false}` opts out.
 */
export function SaveBar({
  changes,
  onDiscard,
  saving,
  saveLabel = "Save changes",
  blockedReason,
  disabled,
  className,
  extra,
  formId,
  onSave,
  guard = true,
  guardMessage,
}: {
  changes: number
  onDiscard: () => void
  saving?: boolean
  saveLabel?: string
  blockedReason?: string
  disabled?: boolean
  className?: string
  extra?: ReactNode
  /** If set, Save is type="submit" for that form; otherwise call onSave. */
  formId?: string
  onSave?: () => void
  /** Guard the page against leaving with unsaved changes (default true). */
  guard?: boolean
  guardMessage?: string
}) {
  const dirty = changes > 0
  useUnsavedChangesGuard(guard && dirty, guardMessage)
  return (
    <div
      role="region"
      aria-label="Unsaved changes"
      className={cn(
        "sticky bottom-20 z-20 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card px-4 py-3 shadow-md md:bottom-4",
        className
      )}
    >
      <p className="text-sm text-muted-foreground" aria-live="polite">
        {dirty ? (
          <span className="font-medium text-foreground">
            {changes} unsaved {changes === 1 ? "change" : "changes"}
          </span>
        ) : (
          "No unsaved changes"
        )}
        {blockedReason ? <span className="ml-2 text-warning">{blockedReason}</span> : null}
      </p>
      <div className="flex items-center gap-2">
        {extra}
        <Button type="button" variant="ghost" onClick={onDiscard} disabled={!dirty || saving}>
          Discard
        </Button>
        <Button
          type={formId ? "submit" : "button"}
          form={formId}
          onClick={formId ? undefined : onSave}
          disabled={!dirty || disabled || Boolean(blockedReason)}
          loading={saving}
          title={blockedReason}
        >
          {saving ? "Saving" : saveLabel}
        </Button>
      </div>
    </div>
  )
}
