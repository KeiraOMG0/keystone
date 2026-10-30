import type { ReactNode } from "react"
import { Link } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * Links shown in the footer of the public, shell-less pages (/hub, /terms,
 * /privacy). Plain client-side links: none of these pages needs a session.
 */
const PUBLIC_FOOTER_LINKS: { to: string; label: string }[] = [
  { to: "/hub", label: "Member hub" },
  { to: "/terms", label: "Terms" },
  { to: "/privacy", label: "Privacy" },
  { to: "/status", label: "Status" },
  { to: "/appeal", label: "Appeal a ban" },
]

/** Small footer row with the public links. Usable on any public page. */
export function PublicFooter({ className }: { className?: string }) {
  return (
    <footer className={cn("border-t border-border bg-card", className)}>
      <div className="mx-auto flex max-w-[1000px] flex-col items-center gap-3 px-4 py-6 text-center sm:flex-row sm:justify-between sm:text-left">
        <p className="text-xs text-muted-foreground">Gamer Girls HQ bot and dashboard</p>
        <nav aria-label="Site links">
          <ul className="flex flex-wrap items-center justify-center gap-x-4 gap-y-2">
            {PUBLIC_FOOTER_LINKS.map((link) => (
              <li key={link.to}>
                <Link
                  to={link.to}
                  className="rounded-sm text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
                >
                  {link.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      </div>
    </footer>
  )
}

/**
 * Frame for the public, shell-less pages: the same 56px header as /status and
 * /appeal (K mark + page name + a way back), a centred main column and the
 * public footer. No auth, no API calls.
 */
export function PublicShell({
  label,
  children,
  backTo = "/",
  backLabel = "Back to home",
  mainClassName,
}: {
  /** Short name shown next to the K mark in the header. */
  label: string
  children: ReactNode
  backTo?: string
  backLabel?: string
  mainClassName?: string
}) {
  return (
    <div className="flex min-h-svh flex-col bg-background text-foreground">
      <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-border bg-card px-4 md:px-8">
        <Link to="/hub" className="flex min-w-0 items-center gap-2.5 rounded-md">
          <div
            className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-sm font-semibold text-primary-foreground"
            aria-hidden
          >
            K
          </div>
          <span className="truncate text-md font-semibold">{label}</span>
        </Link>
        <Button asChild variant="ghost" size="sm">
          <Link to={backTo}>{backLabel}</Link>
        </Button>
      </header>
      <main className={cn("mx-auto flex w-full max-w-[1000px] flex-1 flex-col gap-6 px-4 py-8 md:py-12", mainClassName)}>
        {children}
      </main>
      <PublicFooter />
    </div>
  )
}
