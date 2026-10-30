import { CopyButton } from "@/components/ui/copy-button"
import { useDeveloperMode } from "@/context/DeveloperModeContext"
import { useResolvedChannel } from "@/hooks/useResolve"

/**
 * "#channel-name" for a Discord channel id. Falls back to a muted raw-ID
 * chip while loading or if the id didn't resolve (e.g. the channel was
 * deleted) — raw id always available via `title`, and shown inline with a
 * copy button when Developer Mode is on.
 */
export function ResolvedChannel({ channelId }: { channelId: string }) {
  const { data, isLoading } = useResolvedChannel(channelId)
  const { enabled: devMode } = useDeveloperMode()

  if (isLoading || data === undefined || data === null) {
    return (
      <code className="text-xs text-muted-foreground" title={channelId}>
        {channelId}
      </code>
    )
  }

  return (
    <span className="inline-flex items-center gap-1.5" title={channelId}>
      <span>#{data.name}</span>
      {devMode ? (
        <>
          <code className="text-2xs text-muted-foreground">{channelId}</code>
          <CopyButton value={channelId} />
        </>
      ) : null}
    </span>
  )
}
