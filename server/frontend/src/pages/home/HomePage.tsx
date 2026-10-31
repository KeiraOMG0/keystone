import type { SVGProps } from "react"
import { Navigate, Link, useSearchParams } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Alert } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { useAuth } from "@/hooks/useAuth"
import { DiscordIcon } from "@/components/DiscordIcon"

/**
 * PUBLIC homepage. Branding + "Continue with Discord".
 *
 * The login link is a real <a href> top-level navigation, NOT a fetch/XHR:
 * GET /api/auth/login (server/auth/oauth.py) issues a 302 redirect to
 * Discord's own authorize page, which only works as a full browser
 * navigation (a fetch would just follow the redirect in the background and
 * throw away the result).
 */
export default function HomePage() {
  const { state, permissions } = useAuth()
  const [params] = useSearchParams()
  const signInError = params.get("error")

  // Already signed in: staff (any bot permission) go to the dashboard; members
  // without one, and people who aren't in the server (the hub explains why:
  // trap-channel removal with their appeal status, a ban, or not joined), go
  // to the member hub instead of a 403.
  if (state === "authenticated") {
    const hasDashboardAccess = Object.values(permissions).some(Boolean)
    return <Navigate to={hasDashboardAccess ? "/dashboard" : "/hub"} replace />
  }
  if (state === "not_a_member") {
    return <Navigate to="/hub" replace />
  }

  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-8 bg-background px-4 text-center">
      <div className="flex flex-col items-center gap-3">
        <div className="flex size-12 items-center justify-center rounded-xl bg-primary text-primary-foreground">
          <ShieldIcon className="size-6" />
        </div>
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">Keystone</h1>
        <p className="max-w-sm text-base text-muted-foreground">
          Sign in with your Discord account to view live server stats and moderation tools.
        </p>
      </div>

      <div className="flex w-full max-w-xs flex-col items-stretch gap-3">
        {signInError ? (
          <Alert variant="danger" title="Sign-in did not work" className="text-left">
            Something went wrong while signing in. Try again, or check the error codes below.
          </Alert>
        ) : null}
        {state === "loading" ? (
          // Same footprint as the button, so nothing flashes or jumps.
          <Skeleton className="h-11 w-full" />
        ) : (
          <Button
            asChild
            size="lg"
            className="w-full bg-[#5865F2] text-white hover:bg-[#4752c4]"
          >
            <a href="/api/auth/login">
              <DiscordIcon className="size-5" />
              Continue with Discord
            </a>
          </Button>
        )}
        <p className="text-xs text-muted-foreground">Staff land on the dashboard, members on the member hub.</p>
      </div>

      <Link to="/status" className="rounded-sm text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground">
        Seeing an error? View API error codes
      </Link>

      {/* Public links for members who aren't staff (member hub, terms). */}
      <nav aria-label="Member links" className="flex flex-col items-center gap-2">
        <Button asChild variant="secondary" size="sm">
          <Link to="/hub">Not staff? Visit the member hub</Link>
        </Button>
        <div className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-xs">
          <Link to="/terms" className="rounded-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
            Terms of Service
          </Link>
          <Link to="/privacy" className="rounded-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline">
            Privacy Policy
          </Link>
        </div>
      </nav>
    </div>
  )
}

function ShieldIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75} aria-hidden {...props}>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6l7-3z"
      />
    </svg>
  )
}
