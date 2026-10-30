import { Link } from "react-router-dom"
import { Lock } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { CopyButton } from "@/components/ui/copy-button"
import { cn } from "@/lib/utils"

export type ForbiddenReason = "not_a_member" | "forbidden"

interface Forbidden403Props {
  reason: ForbiddenReason
  /** Only meaningful for reason="forbidden": the permission string that was required. */
  requiredPermission?: string
}

const COPY: Record<ForbiddenReason, { title: string; description: string }> = {
  not_a_member: {
    title: "You're not in the server",
    description:
      "This dashboard is only available to members of the Discord server it manages. Join the server with the Discord account you signed in with, then reload this page.",
  },
  forbidden: {
    title: "You don't have permission",
    description:
      "You're signed in and a member of the server, but you don't have the permission needed to view this page. Ask a server admin if you believe this is a mistake.",
  },
}

/**
 * SHARED 403 page. Distinguishes "not a member of the server" (full page, no
 * shell: rendered by RequireAuth) from "member, but lacking a specific
 * permission" (rendered INSIDE the app shell by the page itself, so the nav
 * stays visible). Different problems, different fixes, different copy.
 */
export default function Forbidden403({ reason, requiredPermission }: Forbidden403Props) {
  const copy = COPY[reason]
  const inShell = reason === "forbidden"

  return (
    <div className={cn("flex flex-col items-center justify-center gap-6 px-4 text-center", inShell ? "min-h-[60vh]" : "min-h-svh bg-background")}>
      <div className="flex max-w-md flex-col items-center gap-3">
        <div className="flex size-[52px] items-center justify-center rounded-2xl bg-danger-soft text-danger">
          <Lock className="size-6" aria-hidden />
        </div>
        <Badge variant="destructive">403, Access denied</Badge>
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">{copy.title}</h1>
        <p className="text-base text-muted-foreground">{copy.description}</p>
        {reason === "forbidden" && requiredPermission ? (
          <p className="mt-1 flex items-center gap-1 rounded-lg bg-muted py-1 pr-1 pl-3 text-xs text-muted-foreground">
            Required permission: <span className="font-mono text-foreground">{requiredPermission}</span>
            <CopyButton value={requiredPermission} label="Copy permission name" />
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center justify-center gap-2">
        <Button asChild variant="secondary">
          <Link to={inShell ? "/dashboard" : "/"}>{inShell ? "Back to Overview" : "Back to home"}</Link>
        </Button>
        {/* Members without staff permissions still have somewhere useful to go. */}
        <Button asChild variant="secondary">
          <Link to="/hub">Member hub</Link>
        </Button>
        <Button asChild variant="ghost">
          <a href="/api/auth/login">Sign in with another account</a>
        </Button>
      </div>
    </div>
  )
}
