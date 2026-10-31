import type { ReactNode } from "react"
import { Link } from "react-router-dom"
import {
  CakeSlice,
  CalendarDays,
  Check,
  CheckCircle2,
  Circle,
  ExternalLink,
  Gem,
  MessageCircleQuestion,
  Music2,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Sparkles,
  Tags,
  Ticket,
  Users,
  type LucideIcon,
} from "lucide-react"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"
import { describeApiError } from "@/lib/apiError"
import { cn } from "@/lib/utils"
import { useToggleSelfRole, type HubPanel, type HubResponse, type HubSelfRole } from "@/api/hub"
import { birthdayOffset, displayEmoji, durationSince, formatDay, formatWhen, initials, isDividerRole, toDate } from "./hubFormat"

/* ------------------------------------------------------------------ */
/* Building blocks                                                     */
/* ------------------------------------------------------------------ */

export function HubCard({
  icon: Icon,
  title,
  action,
  children,
  className,
  id,
}: {
  icon: LucideIcon
  title: string
  action?: ReactNode
  children: ReactNode
  className?: string
  id?: string
}) {
  const headingId = id ? `${id}-title` : undefined
  return (
    <section
      id={id}
      aria-labelledby={headingId}
      className={cn("flex flex-col gap-4 rounded-xl border border-border bg-card p-5 shadow-sm", className)}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary-soft text-primary-soft-foreground">
            <Icon className="size-4" aria-hidden />
          </div>
          <h2 id={headingId} className="truncate text-md font-semibold text-foreground">
            {title}
          </h2>
        </div>
        {action}
      </div>
      {children}
    </section>
  )
}

function Muted({ children }: { children: ReactNode }) {
  return <p className="text-sm leading-6 text-muted-foreground">{children}</p>
}

function ExternalButton({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Button asChild variant="ghost" size="sm">
      <a href={href} target="_blank" rel="noopener noreferrer">
        {children}
        <ExternalLink aria-hidden />
        <span className="sr-only"> (opens in a new tab)</span>
      </a>
    </Button>
  )
}

function RoleDot({ color }: { color: string | null }) {
  return (
    <span
      aria-hidden
      className="size-2.5 shrink-0 rounded-full bg-subtle-foreground"
      // Role colours are Discord data, not theme colours.
      style={color ? { backgroundColor: color } : undefined}
    />
  )
}

/* ------------------------------------------------------------------ */
/* Hero                                                                */
/* ------------------------------------------------------------------ */

export function HubHero({ data }: { data: HubResponse }) {
  const { server, user, profile } = data
  const name = profile?.display_name ?? user.display_name ?? user.username ?? "there"
  const since = durationSince(profile?.joined_at)
  return (
    <section
      aria-label="Welcome"
      className="overflow-hidden rounded-xl border border-border bg-card shadow-sm"
    >
      <div className="h-20 bg-primary-soft sm:h-24" aria-hidden />
      <div className="flex flex-col gap-4 px-5 pb-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-4">
          {/* Only the avatar overlaps the banner; the text sits below it. */}
          <Avatar className="-mt-10 size-20 shrink-0 border-4 border-card bg-card">
            <AvatarImage src={user.avatar_url ?? undefined} alt="" />
            <AvatarFallback className="text-xl">{initials(name)}</AvatarFallback>
          </Avatar>
          <div className="flex min-w-0 flex-col gap-0.5 pt-3">
            <h1 className="truncate text-2xl font-semibold tracking-tight text-foreground">Welcome back, {name}</h1>
            <p className="truncate text-sm text-muted-foreground">
              {user.username ? `@${user.username}` : null}
              {since ? ` · in ${server.name} for ${since}` : null}
            </p>
          </div>
        </div>
        <Button asChild variant="secondary" size="sm" className="self-start sm:mt-3">
          <a href={`https://discord.com/channels/${server.id}`} target="_blank" rel="noopener noreferrer">
            Open in Discord
            <ExternalLink aria-hidden />
          </a>
        </Button>
      </div>
      <dl className="grid grid-cols-3 border-t border-border">
        <HeroStat icon={Users} label="Members" value={server.member_count.toLocaleString()} />
        <HeroStat
          icon={Circle}
          iconClassName="fill-success text-success"
          label="Online now"
          value={server.online_count === null ? "–" : server.online_count.toLocaleString()}
        />
        <HeroStat
          icon={Gem}
          label="Boosts"
          value={`${server.boost_count}${server.boost_tier ? ` · Level ${server.boost_tier}` : ""}`}
        />
      </dl>
    </section>
  )
}

function HeroStat({
  icon: Icon,
  label,
  value,
  iconClassName,
}: {
  icon: LucideIcon
  label: string
  value: string
  iconClassName?: string
}) {
  return (
    <div className="flex flex-col gap-0.5 border-border px-4 py-3 not-last:border-r sm:px-5">
      <dt className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <Icon className={cn("size-3", iconClassName)} aria-hidden />
        {label}
      </dt>
      <dd className="text-lg font-semibold text-foreground tabular-nums">{value}</dd>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* Getting started (Discord's "Server Guide" to-dos)                   */
/* ------------------------------------------------------------------ */

export function GettingStarted({ data }: { data: HubResponse }) {
  const steps: { label: string; detail: string; done: boolean; href?: string }[] = []
  if (data.profile?.verified !== null && data.profile?.verified !== undefined) {
    steps.push({
      label: "Get verified",
      detail: data.profile.verified ? "You're verified." : "Open a verification ticket in Discord so staff can let you in.",
      done: data.profile.verified,
    })
  }
  if (data.self_roles && data.self_roles.length > 0) {
    const picked = data.self_roles.some((p) => p.roles.some((r) => r.has))
    steps.push({
      label: "Pick your roles",
      detail: picked ? "You can change them any time below." : "Choose colours, pings and interests below.",
      done: picked,
      href: "#hub-roles",
    })
  }
  if (data.birthday !== undefined && data.birthday !== null) {
    steps.push({
      label: "Add your birthday",
      detail: data.birthday.mine
        ? `Saved as ${data.birthday.mine.label}.`
        : "Use /birthday set date in Discord and the server will celebrate with you.",
      done: data.birthday.mine !== null,
    })
  }
  if (steps.length === 0) return null
  const done = steps.filter((s) => s.done).length
  const complete = done === steps.length

  return (
    <HubCard
      icon={Sparkles}
      title="Getting started"
      id="hub-start"
      action={<Badge variant={complete ? "success" : "accent"}>{complete ? "All done" : `${done} of ${steps.length}`}</Badge>}
    >
      <div className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden>
        <div
          className="h-full rounded-full bg-primary transition-[width] duration-300"
          style={{ width: `${(done / steps.length) * 100}%` }}
        />
      </div>
      <ol className="flex flex-col gap-1">
        {steps.map((step) => {
          const Icon = step.done ? CheckCircle2 : Circle
          const body = (
            <>
              <Icon className={cn("mt-0.5 size-5 shrink-0", step.done ? "text-success" : "text-subtle-foreground")} aria-hidden />
              <span className="flex min-w-0 flex-col">
                <span className={cn("text-base font-medium", step.done ? "text-muted-foreground line-through" : "text-foreground")}>
                  {step.label}
                  <span className="sr-only">{step.done ? " (done)" : " (to do)"}</span>
                </span>
                <span className="text-sm text-muted-foreground">{step.detail}</span>
              </span>
            </>
          )
          return (
            <li key={step.label}>
              {step.href && !step.done ? (
                <a href={step.href} className="flex gap-3 rounded-lg p-2 transition-colors hover:bg-hover">
                  {body}
                </a>
              ) : (
                <div className="flex gap-3 p-2">{body}</div>
              )}
            </li>
          )
        })}
      </ol>
    </HubCard>
  )
}

/* ------------------------------------------------------------------ */
/* Self-roles (Discord's "Channels & Roles")                           */
/* ------------------------------------------------------------------ */

export function SelfRoles({ panels }: { panels: HubPanel[] }) {
  const toggle = useToggleSelfRole()
  const { toast } = useToast()

  const onToggle = (panel: HubPanel, role: HubSelfRole) => {
    const assign = !role.has
    toggle.mutate(
      { panel, role, assign },
      {
        onSuccess: (res) => {
          if (res.result === "unchanged") return
          toast({
            title: assign ? `Added ${role.name}` : `Removed ${role.name}`,
            description:
              assign && res.removed_role_ids.length > 0 ? "Your other role from this panel was removed." : undefined,
            variant: "success",
          })
        },
        onError: (error) =>
          toast({ title: "Couldn't change that role", description: describeApiError(error), variant: "destructive" }),
      },
    )
  }

  return (
    <HubCard icon={Tags} title="Your roles" id="hub-roles">
      {panels.length === 0 ? (
        <Muted>There are no self-assignable roles yet.</Muted>
      ) : (
        <div className="flex flex-col gap-5">
          {panels.map((panel) => (
            <div key={panel.panel_id} className="flex flex-col gap-2.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <h3 className="text-base font-semibold text-foreground">{panel.title}</h3>
                  {panel.exclusive ? <Badge variant="outline">Pick one</Badge> : null}
                </div>
                {panel.jump_url ? <ExternalButton href={panel.jump_url}>View in Discord</ExternalButton> : null}
              </div>
              <ul className="flex flex-wrap gap-2">
                {panel.roles.map((role) => {
                  const pending =
                    toggle.isPending && toggle.variables?.panel.panel_id === panel.panel_id && toggle.variables.role.id === role.id
                  const emoji = displayEmoji(role.emoji)
                  return (
                    <li key={role.id}>
                      <button
                        type="button"
                        aria-pressed={role.has}
                        aria-busy={pending || undefined}
                        disabled={pending}
                        onClick={() => onToggle(panel, role)}
                        title={role.label ?? undefined}
                        className={cn(
                          "inline-flex h-9 items-center gap-2 rounded-full border px-3.5 text-sm font-medium transition-colors duration-[120ms] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60",
                          role.has
                            ? "border-primary bg-primary-soft text-primary-soft-foreground"
                            : "border-border bg-card text-foreground hover:border-input hover:bg-hover",
                        )}
                      >
                        {role.has ? <Check className="size-3.5" aria-hidden /> : <RoleDot color={role.color} />}
                        {emoji ? <span aria-hidden>{emoji}</span> : null}
                        {role.name}
                      </button>
                    </li>
                  )
                })}
              </ul>
            </div>
          ))}
        </div>
      )}
    </HubCard>
  )
}

/* ------------------------------------------------------------------ */
/* Events, QOTD, music                                                 */
/* ------------------------------------------------------------------ */

export function Events({ events }: { events: NonNullable<HubResponse["events"]> }) {
  return (
    <HubCard icon={CalendarDays} title="Upcoming events" id="hub-events">
      {events.length === 0 ? (
        <Muted>No events are scheduled right now. Staff post them in Discord&apos;s Events tab.</Muted>
      ) : (
        <ul className="flex flex-col gap-2">
          {events.map((event) => {
            const start = toDate(event.start_time)
            return (
              <li key={event.id}>
                <a
                  href={event.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex gap-3.5 rounded-lg border border-border p-3 transition-colors hover:border-input hover:bg-hover"
                >
                  <div className="flex w-12 shrink-0 flex-col items-center justify-center rounded-lg bg-primary-soft py-1.5 text-primary-soft-foreground">
                    <span className="text-2xs font-semibold uppercase">
                      {start ? start.toLocaleDateString([], { month: "short" }) : "–"}
                    </span>
                    <span className="text-lg leading-tight font-semibold">{start ? start.getDate() : "–"}</span>
                  </div>
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <p className="flex flex-wrap items-center gap-2 text-base font-medium text-foreground">
                      {event.name}
                      {event.active ? <Badge variant="success">Happening now</Badge> : null}
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {formatWhen(event.start_time)}
                      {event.location ? ` · ${event.location}` : ""}
                      {event.interested ? ` · ${event.interested} interested` : ""}
                    </p>
                    {event.description ? (
                      <p className="line-clamp-2 text-sm text-muted-foreground">{event.description}</p>
                    ) : null}
                  </div>
                </a>
              </li>
            )
          })}
        </ul>
      )}
    </HubCard>
  )
}

export function Qotd({ qotd }: { qotd: NonNullable<HubResponse["qotd"]> }) {
  return (
    <HubCard
      icon={MessageCircleQuestion}
      title="Question of the day"
      id="hub-qotd"
      action={qotd.jump_url ? <ExternalButton href={qotd.jump_url}>Answer</ExternalButton> : null}
    >
      <blockquote className="border-l-2 border-primary pl-4 text-lg leading-7 text-foreground">{qotd.question}</blockquote>
      {qotd.posted_at ? <p className="text-xs text-subtle-foreground">Posted {formatDay(qotd.posted_at)}</p> : null}
    </HubCard>
  )
}

export function NowPlaying({ music }: { music: NonNullable<HubResponse["music"]> }) {
  return (
    <HubCard icon={Music2} title={music.paused ? "Paused" : "Now playing"} id="hub-music">
      <div className="flex flex-col gap-1">
        <p className="line-clamp-2 text-base font-medium text-foreground">{music.title}</p>
        <p className="text-sm text-muted-foreground">
          {music.voice_channel ? `In ${music.voice_channel}` : "In voice"}
          {music.listeners ? ` · ${music.listeners} listening` : ""}
          {music.queue_total ? ` · ${music.queue_total} queued` : ""}
        </p>
      </div>
      <Muted>Join the voice channel and use !play in Discord to add a song.</Muted>
    </HubCard>
  )
}

/* ------------------------------------------------------------------ */
/* Sidebar: standing, profile, birthday, tickets                       */
/* ------------------------------------------------------------------ */

const STANDING: Record<
  "good" | "warned" | "timed_out",
  { title: string; icon: LucideIcon; tone: string; step: number }
> = {
  good: { title: "You're in good standing", icon: ShieldCheck, tone: "text-success", step: 0 },
  warned: { title: "You have warnings on record", icon: ShieldAlert, tone: "text-warning", step: 1 },
  timed_out: { title: "You're timed out", icon: ShieldX, tone: "text-danger", step: 2 },
}

export function Standing({ standing }: { standing: NonNullable<HubResponse["standing"]> }) {
  const s = STANDING[standing.level]
  const segments = ["bg-success", "bg-warning", "bg-danger"]
  return (
    <HubCard icon={ShieldCheck} title="Your standing" id="hub-standing">
      <div className="flex items-start gap-3">
        <s.icon className={cn("mt-0.5 size-6 shrink-0", s.tone)} aria-hidden />
        <div className="flex flex-col gap-1">
          <p className="text-base font-semibold text-foreground">{s.title}</p>
          <p className="text-sm text-muted-foreground">
            {standing.level === "timed_out"
              ? `You can't chat until ${formatWhen(standing.timed_out_until)}.`
              : standing.warning_count === 0
                ? "No warnings. Thanks for keeping the server friendly."
                : `${standing.warning_count} warning${standing.warning_count === 1 ? "" : "s"}, most recent ${formatDay(standing.warnings[0]?.created_at)}.`}
          </p>
        </div>
      </div>
      <div className="flex gap-1" role="img" aria-label={`Standing: ${s.title}`}>
        {segments.map((tone, i) => (
          <div key={tone} className={cn("h-1.5 flex-1 rounded-full", i <= s.step ? tone : "bg-muted")} />
        ))}
      </div>
      {standing.level !== "good" ? (
        <Muted>Questions about a warning or timeout? Open a ticket in Discord and staff will explain.</Muted>
      ) : null}
    </HubCard>
  )
}

export function Profile({ data }: { data: HubResponse }) {
  const profile = data.profile
  if (!profile) return null
  const rows: [string, string][] = [
    ["Joined the server", formatDay(profile.joined_at)],
    ["Discord account created", formatDay(profile.created_at)],
  ]
  if (profile.booster_since) rows.push(["Boosting since", formatDay(profile.booster_since)])
  const roles = profile.roles.filter((r) => !isDividerRole(r.name))
  return (
    <HubCard icon={Users} title="Your profile" id="hub-profile">
      <dl className="flex flex-col gap-2">
        {rows.map(([label, value]) => (
          <div key={label} className="flex items-center justify-between gap-3 text-sm">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="text-right font-medium text-foreground">{value}</dd>
          </div>
        ))}
      </dl>
      {roles.length > 0 ? (
        <div className="flex flex-col gap-2">
          <p className="text-xs font-medium text-muted-foreground">Roles ({roles.length})</p>
          <ul className="flex flex-wrap gap-1.5">
            {roles.map((role) => (
              <li
                key={role.id}
                className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-muted px-2 text-xs font-medium text-foreground"
              >
                <RoleDot color={role.color} />
                {role.name}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </HubCard>
  )
}

export function Birthdays({ birthday }: { birthday: NonNullable<HubResponse["birthday"]> }) {
  return (
    <HubCard icon={CakeSlice} title="Birthdays" id="hub-birthdays">
      <div className="rounded-lg bg-muted px-3 py-2.5 text-sm">
        {birthday.mine ? (
          <p className="text-foreground">
            Your birthday: <span className="font-semibold">{birthday.mine.label}</span>
            {birthday.mine.timezone ? <span className="text-muted-foreground"> · {birthday.mine.timezone}</span> : null}
          </p>
        ) : (
          <p className="text-muted-foreground">
            Not set yet. Use <span className="font-mono text-foreground">/birthday set date</span> in Discord.
          </p>
        )}
      </div>
      {birthday.nearest.length === 0 ? (
        <Muted>No birthdays in the last week or the next two weeks.</Muted>
      ) : (
        <ul className="flex flex-col gap-2.5">
          {birthday.nearest.flatMap((group) =>
            group.people.map((person) => (
              <li key={`${group.label}-${person.id}`} className="flex items-center gap-2.5">
                <Avatar className="size-8">
                  <AvatarImage src={person.avatar_url ?? undefined} alt="" />
                  <AvatarFallback className="text-2xs">{initials(person.display_name)}</AvatarFallback>
                </Avatar>
                <div className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-sm font-medium text-foreground">{person.display_name}</span>
                  <span className="text-xs text-muted-foreground">{group.label}</span>
                </div>
                <Badge variant={group.offset === 0 ? "accent" : "outline"}>{birthdayOffset(group.offset)}</Badge>
              </li>
            )),
          )}
        </ul>
      )}
    </HubCard>
  )
}

export function Tickets({ tickets }: { tickets: NonNullable<HubResponse["tickets"]> }) {
  return (
    <HubCard icon={Ticket} title="Your tickets" id="hub-tickets">
      {tickets.length === 0 ? (
        <Muted>
          No open tickets. Need staff? Open one from a ticket panel in Discord: it&apos;s a private channel between you and
          staff.
        </Muted>
      ) : (
        <ul className="flex flex-col gap-2">
          {tickets.map((t) => (
            <li key={t.id}>
              <a
                href={t.jump_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center justify-between gap-3 rounded-lg border border-border px-3 py-2.5 text-sm transition-colors hover:border-input hover:bg-hover"
              >
                <span className="flex min-w-0 flex-col">
                  <span className="truncate font-medium text-foreground">#{t.channel_name ?? `ticket-${t.number ?? t.id}`}</span>
                  <span className="text-xs text-muted-foreground">Opened {formatDay(t.opened_at)}</span>
                </span>
                <Badge variant="success">Open</Badge>
              </a>
            </li>
          ))}
        </ul>
      )}
    </HubCard>
  )
}

/* ------------------------------------------------------------------ */
/* Public links                                                        */
/* ------------------------------------------------------------------ */

export function UsefulLinks({ sourceUrl }: { sourceUrl: string }) {
  const links: { to: string; label: string; external?: boolean }[] = [
    { to: "/terms", label: "Terms of Service" },
    { to: "/privacy", label: "Privacy Policy" },
    { to: "/status", label: "Bot status" },
    { to: "/appeal", label: "Appeal a trap-channel ban" },
    { to: sourceUrl, label: "Bot source code", external: true },
  ]
  return (
    <nav aria-label="Useful links" className="rounded-xl border border-border bg-card p-2 shadow-sm">
      <ul className="flex flex-col">
        {links.map((l) => (
          <li key={l.to}>
            {l.external ? (
              <a
                href={l.to}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center justify-between rounded-lg px-3 py-2 text-sm text-foreground transition-colors hover:bg-hover"
              >
                {l.label}
                <ExternalLink className="size-3.5 text-subtle-foreground" aria-hidden />
              </a>
            ) : (
              <Link to={l.to} className="flex rounded-lg px-3 py-2 text-sm text-foreground transition-colors hover:bg-hover">
                {l.label}
              </Link>
            )}
          </li>
        ))}
      </ul>
    </nav>
  )
}
