import { CopyButton } from "@/components/ui/copy-button"
import { useDeveloperMode } from "@/context/DeveloperModeContext"
import { useResolvedRole } from "@/hooks/useResolve"

/**
 * Colored dot + role name for a Discord role id. Falls back to a muted raw-
 * ID chip while loading or if the id didn't resolve (e.g. the role was
 * deleted) — raw id always available via `title`, and shown inline with a
 * copy button when Developer Mode is on.
 */
export function ResolvedRole({ roleId }: { roleId: string }) {
  const { data, isLoading } = useResolvedRole(roleId)
  const { enabled: devMode } = useDeveloperMode()

  if (isLoading || data === undefined || data === null) {
    return (
      <code className="text-xs text-muted-foreground" title={roleId}>
        {roleId}
      </code>
    )
  }

  return (
    <span className="inline-flex items-center gap-1.5" title={roleId}>
      <span
        className="size-2.5 shrink-0 rounded-full border border-border"
        style={{ backgroundColor: data.color ?? undefined }}
      />
      <span>{data.name}</span>
      {devMode ? (
        <>
          <code className="text-2xs text-muted-foreground">{roleId}</code>
          <CopyButton value={roleId} />
        </>
      ) : null}
    </span>
  )
}
