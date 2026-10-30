import type { SVGProps } from "react"
import { Navigate, Link, useSearchParams } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Alert } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { useAuth } from "@/hooks/useAuth"

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
  const { state } = useAuth()
  const [params] = useSearchParams()
  const signInError = params.get("error")

  // Already signed in and a guild member: no reason to show the login screen.
  if (state === "authenticated") {
    return <Navigate to="/dashboard" replace />
  }

  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-8 bg-background px-4 text-center">
      <div className="flex flex-col items-center gap-3">
        <div className="flex size-12 items-center justify-center rounded-xl bg-primary text-primary-foreground">
          <ShieldIcon className="size-6" />
        </div>
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">Kick-Bot Dashboard</h1>
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
        <p className="text-xs text-muted-foreground">Only members of Gamer Girls HQ can sign in.</p>
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

function DiscordIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden {...props}>
      <path d="M20.317 4.369a19.79 19.79 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037 19.736 19.736 0 0 0-4.885 1.515.07.07 0 0 0-.032.027C.533 9.09-.32 13.68.099 18.212a.082.082 0 0 0 .031.056 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.1 13.1 0 0 1-1.872-.892.077.077 0 0 1-.008-.128c.126-.094.252-.192.372-.291a.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.099.246.198.373.292a.077.077 0 0 1-.006.127c-.598.35-1.22.645-1.873.892a.076.076 0 0 0-.04.107c.36.698.772 1.363 1.225 1.993a.076.076 0 0 0 .084.028 19.84 19.84 0 0 0 6.002-3.03.077.077 0 0 0 .032-.055c.5-5.177-.838-9.727-3.548-13.816a.06.06 0 0 0-.031-.028zM8.02 15.33c-1.183 0-2.157-1.086-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.211 0 2.176 1.096 2.157 2.42 0 1.332-.955 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.086-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.211 0 2.176 1.096 2.157 2.42 0 1.332-.946 2.418-2.157 2.418z" />
    </svg>
  )
}
