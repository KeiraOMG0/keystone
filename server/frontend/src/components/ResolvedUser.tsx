import { useContext } from "react"
import { Link } from "react-router-dom"
import { DepartedBadge } from "@/components/DepartedBadge"
import { AuthContext } from "@/context/AuthContext"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { CopyButton } from "@/components/ui/copy-button"
import { useDeveloperMode } from "@/context/DeveloperModeContext"
import { useResolvedUser } from "@/hooks/useResolve"
import { cn } from "@/lib/utils"

function initials(name: string): string {
  return name.slice(0, 2).toUpperCase()
}

/**
 * Inline avatar + username for a Discord user id. Falls back to a muted raw-
 * ID chip while loading or if the id didn't resolve at all — per the design
 * review, raw snowflakes stay tooltip-only metadata by default, but Developer
 * Mode surfaces them as visible trailing text with a copy button so mods can
 * grab an id without hovering.
 *
 * Users who are no longer in the server come back from the bot's user cache
 * (`departed: true`): they render with their last known name + avatar (muted)
 * and a Left / Kicked / Banned / Unbanned badge whose tooltip gives the date.
 */
export function ResolvedUser({ userId, link = false }: { userId: string; link?: boolean }) {
  const { data, isLoading } = useResolvedUser(userId)
  // `link`: the name opens the member profile (/dashboard/members/:id) for
  // viewers with staff_base. Opt-in: never pass it inside another link/button.
  const auth = useContext(AuthContext)
  const asLink = link && Boolean(auth?.permissions.staff_base)
  const { enabled: devMode } = useDeveloperMode()

  if (isLoading || data === undefined) {
    return (
      <span className="inline-flex items-center gap-2" title={userId}>
        <Avatar size="sm">
          <AvatarFallback className="text-xs">?</AvatarFallback>
        </Avatar>
        <code className="text-xs text-muted-foreground">{userId}</code>
      </span>
    )
  }

  if (data === null) {
    return (
      <span className="inline-flex items-center gap-2" title={userId}>
        <Avatar size="sm">
          <AvatarFallback className="text-xs">?</AvatarFallback>
        </Avatar>
        <code className="text-xs text-muted-foreground">{userId} (unknown user)</code>
      </span>
    )
  }

  const departed = data.departed === true
  return (
    <span className="inline-flex items-center gap-2" title={userId}>
      <Avatar size="sm" className={cn(departed && "opacity-70 grayscale")}>
        <AvatarImage src={data.avatar_url} alt={data.display_name} />
        <AvatarFallback className="text-xs">{initials(data.display_name)}</AvatarFallback>
      </Avatar>
      {asLink ? (
        <Link to={`/dashboard/members/${userId}`} className={cn("hover:underline", departed && "text-muted-foreground")}>
          {data.display_name}
        </Link>
      ) : (
        <span className={cn(departed && "text-muted-foreground")}>{data.display_name}</span>
      )}
      {departed ? <DepartedBadge user={data} /> : null}
      {devMode ? (
        <>
          <code className="text-2xs text-muted-foreground">{userId}</code>
          <CopyButton value={userId} />
        </>
      ) : null}
    </span>
  )
}
