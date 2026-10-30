import { useEffect, type ComponentType, type SVGProps } from "react"
import { Link } from "react-router-dom"
import { Activity, ArrowRight, FileText, Gavel, LifeBuoy, ShieldCheck } from "lucide-react"
import { PublicShell } from "@/components/layout/PublicShell"

interface HubLink {
  to: string
  title: string
  description: string
  icon: ComponentType<SVGProps<SVGSVGElement>>
}

const HUB_LINKS: HubLink[] = [
  {
    to: "/appeal",
    title: "Appeal a ban",
    description: "Removed by the trap channel? Ask staff to review it, check your appeal's outcome and rejoin if it's approved.",
    icon: Gavel,
  },
  {
    to: "/terms",
    title: "Terms of Service",
    description: "The rules for using the bot and this website, including how automated moderation works.",
    icon: FileText,
  },
  {
    to: "/privacy",
    title: "Privacy Policy",
    description: "What the bot and website store about you, why, and for how long.",
    icon: ShieldCheck,
  },
  {
    to: "/status",
    title: "Bot status",
    description: "Check whether the bot and website are online right now, and what error codes mean.",
    icon: Activity,
  },
]

/**
 * PUBLIC member hub (/hub): one place for members who are not staff (and
 * banned people) to find the appeal page, terms, privacy policy and status.
 * Linked from the landing page and from the dashboard's 403 page. No auth,
 * no API calls.
 */
export default function HubPage() {
  useEffect(() => {
    const previous = document.title
    document.title = "Member hub · Gamer Girls HQ"
    return () => {
      document.title = previous
    }
  }, [])

  return (
    <PublicShell label="Member hub">
      <div className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">Member hub</h1>
        <p className="max-w-prose text-md leading-6 text-muted-foreground">
          Everything for members of Gamer Girls HQ in one place. The dashboard itself is for staff; you don&apos;t need to
          sign in to use anything on this page.
        </p>
      </div>

      <ul className="grid gap-4 sm:grid-cols-2">
        {HUB_LINKS.map(({ to, title, description, icon: Icon }) => (
          <li key={to}>
            <Link
              to={to}
              className="group flex h-full flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-sm transition-colors duration-[120ms] hover:border-input hover:bg-hover"
            >
              <div className="flex items-center justify-between gap-3">
                <div className="flex size-10 items-center justify-center rounded-lg bg-primary-soft text-primary-soft-foreground">
                  <Icon className="size-5" aria-hidden />
                </div>
                <ArrowRight
                  className="size-4 text-subtle-foreground transition-transform duration-[120ms] group-hover:translate-x-0.5 group-hover:text-foreground"
                  aria-hidden
                />
              </div>
              <div className="flex flex-col gap-1">
                <h2 className="text-lg font-semibold text-foreground">{title}</h2>
                <p className="text-sm leading-6 text-muted-foreground">{description}</p>
              </div>
            </Link>
          </li>
        ))}
      </ul>

      <section aria-labelledby="hub-help" className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-sm sm:flex-row sm:gap-4">
        <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
          <LifeBuoy className="size-5" aria-hidden />
        </div>
        <div className="flex flex-col gap-1.5">
          <h2 id="hub-help" className="text-lg font-semibold text-foreground">
            How to get help
          </h2>
          <p className="text-sm leading-6 text-muted-foreground">
            Open a ticket from a ticket panel in the Gamer Girls HQ Discord server. A ticket is a private channel between
            you and staff, good for questions, reports and verification. You can also message the server owner on Discord.
            If you were banned by the trap channel, use{" "}
            <Link to="/appeal" className="rounded-sm font-medium text-primary underline underline-offset-4 hover:text-primary-hover">
              Appeal a ban
            </Link>{" "}
            instead.
          </p>
        </div>
      </section>

      <p className="text-sm text-muted-foreground">
        Staff member?{" "}
        <Link to="/" className="rounded-sm font-medium text-primary underline underline-offset-4 hover:text-primary-hover">
          Sign in to the dashboard
        </Link>
        .
      </p>
    </PublicShell>
  )
}
