import { useEffect, type ReactNode } from "react"
import { Link } from "react-router-dom"
import { useQueryClient } from "@tanstack/react-query"
import { Ban, CalendarDays, Gavel, ShieldCheck, Tags, Ticket, UserX, type LucideIcon } from "lucide-react"
import { PublicShell } from "@/components/layout/PublicShell"
import { DiscordIcon } from "@/components/DiscordIcon"
import { ErrorState } from "@/components/patterns/states"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useLogout } from "@/api/auth"
import { HUB_QUERY_KEY, isSignedOut, useHub, type HubResponse } from "@/api/hub"
import { describeApiError } from "@/lib/apiError"
import { SOURCE_CODE_URL } from "@/lib/project"
import {
  Birthdays,
  Events,
  GettingStarted,
  HubHero,
  NowPlaying,
  Profile,
  Qotd,
  SelfRoles,
  Standing,
  Tickets,
  UsefulLinks,
} from "./HubSections"
import { formatDay, initials } from "./hubFormat"

const SERVER_NAME = "Gamer Girls HQ"
/** Same OAuth flow as the dashboard, returning here (server/auth/oauth.py LOGIN_NEXT_PATHS). */
const LOGIN_URL = "/api/auth/login?next=/hub"

/**
 * Member hub (/hub): the home page for regular members, modelled on Discord's
 * own member surfaces (Server Guide's welcome + to-dos, Channels & Roles, and
 * Account Standing).
 *
 * Sign-in is REQUIRED: signed out, the page is a "Continue with Discord" gate
 * (the public pages it links to, /terms /privacy /status /appeal, stay public).
 * Signed in, GET /api/hub answers with the person's OWN view; the bot decides
 * membership live, so someone who isn't in the server sees why: removed by
 * the trap channel (their appeal status + the appeal page), banned, or not
 * joined. Data: services/member_hub_service.py.
 */
export default function HubPage() {
  useEffect(() => {
    const previous = document.title
    document.title = "Member hub · Gamer Girls HQ"
    return () => {
      document.title = previous
    }
  }, [])

  const hub = useHub()
  const queryClient = useQueryClient()
  const logout = useLogout()
  const signOut = () =>
    logout.mutate(undefined, {
      onSettled: () => queryClient.removeQueries({ queryKey: HUB_QUERY_KEY }),
    })

  const data = hub.data
  const signedOut = hub.isError && isSignedOut(hub.error) && !data

  const headerActions = data ? (
    <div className="flex items-center gap-2">
      <Avatar className="size-7">
        <AvatarImage src={data.user.avatar_url ?? undefined} alt="" />
        <AvatarFallback className="text-2xs">{initials(data.user.display_name ?? data.user.username)}</AvatarFallback>
      </Avatar>
      <Button variant="ghost" size="sm" onClick={signOut} disabled={logout.isPending}>
        {logout.isPending ? "Signing out…" : "Sign out"}
      </Button>
    </div>
  ) : signedOut ? (
    <Button asChild variant="ghost" size="sm">
      <Link to="/">Staff dashboard</Link>
    </Button>
  ) : (
    <span />
  )

  let body: ReactNode
  if (data) {
    body = data.status === "member" ? <MemberView data={data} /> : <OutsiderView data={data} onSignOut={signOut} />
  } else if (signedOut) {
    body = <SignInGate />
  } else if (hub.isError) {
    body = (
      <div className="rounded-xl border border-border bg-card shadow-sm">
        <ErrorState
          title="Couldn't load the member hub"
          message={describeApiError(hub.error)}
          onRetry={() => void hub.refetch()}
          retrying={hub.isFetching}
          showStatusLink
        />
      </div>
    )
  } else {
    body = <HubSkeleton />
  }

  return (
    <PublicShell label="Member hub" headerActions={headerActions} mainClassName="max-w-[1100px]">
      {body}
    </PublicShell>
  )
}

/* ------------------------------------------------------------------ */

function MemberView({ data }: { data: HubResponse }) {
  return (
    <>
      <HubHero data={data} />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex min-w-0 flex-col gap-6">
          <GettingStarted data={data} />
          {data.self_roles !== undefined && !data.unavailable.includes("self_roles") ? (
            <SelfRoles panels={data.self_roles} />
          ) : null}
          {data.qotd ? <Qotd qotd={data.qotd} /> : null}
          {data.events !== undefined ? <Events events={data.events} /> : null}
        </div>
        <aside className="flex min-w-0 flex-col gap-6" aria-label="About you">
          {data.standing ? <Standing standing={data.standing} /> : null}
          {data.music ? <NowPlaying music={data.music} /> : null}
          <Profile data={data} />
          {data.birthday ? <Birthdays birthday={data.birthday} /> : null}
          {data.tickets !== undefined && !data.unavailable.includes("tickets") ? <Tickets tickets={data.tickets} /> : null}
          <UsefulLinks sourceUrl={SOURCE_CODE_URL} />
        </aside>
      </div>
    </>
  )
}

/* ------------------------------------------------------------------ */

const APPEAL_STATUS: Record<string, { label: string; variant: "warning" | "success" | "destructive" | "default"; text: string }> = {
  pending: { label: "Waiting for review", variant: "warning", text: "Staff haven't reviewed your appeal yet. You'll see the outcome on the appeal page." },
  approved: { label: "Approved", variant: "success", text: "Your appeal was approved. Open the appeal page to rejoin the server." },
  denied: { label: "Denied", variant: "destructive", text: "Your appeal was reviewed and denied." },
}

function OutsiderView({ data, onSignOut }: { data: HubResponse; onSignOut: () => void }) {
  const server = data.server.name || SERVER_NAME
  let icon: LucideIcon = UserX
  let title = `You're not in ${server}`
  let text: ReactNode = `The member hub is for members of ${server}. Join the server with this Discord account, then come back to this page.`
  let actions: ReactNode = (
    <Button asChild variant="secondary">
      <a href={LOGIN_URL}>Sign in with another account</a>
    </Button>
  )
  let extra: ReactNode = null

  if (data.status === "trap") {
    const appeal = data.trap?.appeal ?? null
    const status = appeal ? APPEAL_STATUS[appeal.status] : undefined
    icon = Gavel
    title = "You were removed by the trap channel"
    text = data.trap?.eligible
      ? `Posting in ${server}'s trap channel removes people automatically, because spam bots post there. If it was a mistake, send an appeal and staff will review it.`
      : `Posting in ${server}'s trap channel removes people automatically, because spam bots post there.`
    actions = (
      <Button asChild>
        <Link to="/appeal">{data.trap?.eligible ? "Appeal the removal" : "Open the appeal page"}</Link>
      </Button>
    )
    if (appeal) {
      extra = (
        <div className="flex w-full flex-col gap-1.5 rounded-lg border border-border bg-muted px-4 py-3 text-left">
          <div className="flex items-center justify-between gap-3">
            <span className="text-sm font-medium text-foreground">Your appeal</span>
            <Badge variant={status?.variant ?? "default"}>{status?.label ?? appeal.status}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            {status?.text ?? "Open the appeal page for details."}
            {appeal.submitted_at ? ` Sent ${formatDay(appeal.submitted_at)}.` : ""}
          </p>
        </div>
      )
    }
  } else if (data.status === "banned") {
    icon = Ban
    title = `You're banned from ${server}`
    text = "Banned accounts can't use the member hub. If you think this is a mistake, contact the server owner on Discord."
  }

  const Icon = icon
  return (
    <section className="mx-auto flex w-full max-w-lg flex-col items-center gap-5 rounded-xl border border-border bg-card p-6 text-center shadow-sm sm:p-8">
      <div className="flex size-[52px] items-center justify-center rounded-2xl bg-danger-soft text-danger">
        <Icon className="size-6" aria-hidden />
      </div>
      <div className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight text-foreground">{title}</h1>
        <p className="text-base leading-6 text-muted-foreground">{text}</p>
      </div>
      {extra}
      <div className="flex flex-wrap items-center justify-center gap-2">{actions}</div>
      <div className="flex items-center gap-2 border-t border-border pt-4 text-sm text-muted-foreground">
        <span>
          Signed in as <span className="font-medium text-foreground">@{data.user.username ?? "unknown"}</span>
        </span>
        <span aria-hidden>·</span>
        <button type="button" onClick={onSignOut} className="rounded-sm font-medium text-primary underline-offset-4 hover:underline">
          Sign out
        </button>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------------ */

const PREVIEW: { icon: LucideIcon; title: string; text: string }[] = [
  { icon: Tags, title: "Pick your roles", text: "Colours, pings and interests, one click each." },
  { icon: ShieldCheck, title: "Your standing", text: "See whether you have warnings or a timeout." },
  { icon: CalendarDays, title: "Events & birthdays", text: "What's coming up and who's celebrating." },
  { icon: Ticket, title: "Your tickets", text: "Jump straight back into an open ticket." },
]

function SignInGate() {
  return (
    <>
      <section className="flex flex-col items-center gap-6 rounded-xl border border-border bg-card px-6 py-10 text-center shadow-sm sm:py-14">
        <div className="flex flex-col items-center gap-3">
          <Badge variant="accent">For members of {SERVER_NAME}</Badge>
          <h1 className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">Your home in {SERVER_NAME}</h1>
          <p className="max-w-lg text-md leading-6 text-muted-foreground">
            Sign in with Discord to pick your roles, check your standing, and see upcoming events and birthdays.
          </p>
        </div>
        <Button asChild size="lg" className="w-full max-w-xs bg-[#5865F2] text-white hover:bg-[#4752c4]">
          <a href={LOGIN_URL}>
            <DiscordIcon className="size-5" />
            Continue with Discord
          </a>
        </Button>
        <p className="max-w-sm text-xs leading-5 text-muted-foreground">
          We only read your Discord name, avatar and your membership in {SERVER_NAME}.{" "}
          <Link to="/privacy" className="rounded-sm font-medium text-primary underline-offset-4 hover:underline">
            Privacy Policy
          </Link>
        </p>
      </section>

      <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {PREVIEW.map(({ icon: Icon, title, text }) => (
          <li key={title} className="flex flex-col gap-2 rounded-xl border border-border bg-card p-5 shadow-sm">
            <div className="flex size-9 items-center justify-center rounded-lg bg-primary-soft text-primary-soft-foreground">
              <Icon className="size-4" aria-hidden />
            </div>
            <h2 className="text-base font-semibold text-foreground">{title}</h2>
            <p className="text-sm leading-6 text-muted-foreground">{text}</p>
          </li>
        ))}
      </ul>

      <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-sm sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-col gap-1">
          <h2 className="text-base font-semibold text-foreground">Removed by the trap channel?</h2>
          <p className="text-sm text-muted-foreground">You can appeal without being in the server.</p>
        </div>
        <Button asChild variant="secondary">
          <Link to="/appeal">
            <Gavel aria-hidden />
            Appeal a ban
          </Link>
        </Button>
      </div>
    </>
  )
}

function HubSkeleton() {
  return (
    <div className="flex flex-col gap-6" aria-busy="true" aria-label="Loading the member hub">
      <Skeleton className="h-[220px] w-full rounded-xl" />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex flex-col gap-6">
          <Skeleton className="h-48 w-full rounded-xl" />
          <Skeleton className="h-64 w-full rounded-xl" />
        </div>
        <div className="flex flex-col gap-6">
          <Skeleton className="h-36 w-full rounded-xl" />
          <Skeleton className="h-48 w-full rounded-xl" />
        </div>
      </div>
    </div>
  )
}
