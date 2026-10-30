import { useEffect } from "react"
import { useLocation } from "react-router-dom"
import { Link2 } from "lucide-react"
import { PublicShell } from "@/components/layout/PublicShell"
import { PRIVACY_INTRO, PRIVACY_LAST_UPDATED, PRIVACY_SECTIONS } from "@/pages/privacy/privacyContent"

function formatDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number)
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  })
}

function TableOfContents({ className }: { className?: string }) {
  return (
    <nav aria-label="Table of contents" className={className}>
      <p className="mb-2 text-xs font-medium tracking-wide text-subtle-foreground uppercase">On this page</p>
      <ol className="flex flex-col gap-0.5 text-sm">
        {PRIVACY_SECTIONS.map((s, i) => (
          <li key={s.id}>
            <a
              href={`#${s.id}`}
              className="flex gap-2 rounded-md px-2 py-1.5 text-muted-foreground hover:bg-hover hover:text-foreground"
            >
              <span className="w-5 shrink-0 text-right tabular-nums text-subtle-foreground">{i + 1}.</span>
              <span>{s.title}</span>
            </a>
          </li>
        ))}
      </ol>
    </nav>
  )
}

/**
 * PUBLIC Privacy Policy page (/privacy). No auth, no shell, no API calls of
 * its own; the words live in privacyContent.tsx. This is the URL registered
 * as the bot's Privacy Policy URL in the Discord Developer Portal. Same frame
 * and layout as the Terms page (PublicShell, sticky table of contents on
 * large screens, collapsible one on phones).
 */
export default function PrivacyPage() {
  const { hash } = useLocation()

  useEffect(() => {
    const previous = document.title
    document.title = "Privacy Policy · Gamer Girls HQ"
    return () => {
      document.title = previous
    }
  }, [])

  // The page is a lazy chunk, so the browser's own jump to #anchor on load
  // happens before the section exists; redo it once rendered.
  useEffect(() => {
    if (!hash) return
    const el = document.getElementById(decodeURIComponent(hash.slice(1)))
    el?.scrollIntoView()
  }, [hash])

  return (
    <PublicShell label="Privacy Policy" backTo="/hub" backLabel="Member hub">
      <div className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">Privacy Policy</h1>
        <p className="text-sm text-muted-foreground">
          Last updated <time dateTime={PRIVACY_LAST_UPDATED}>{formatDate(PRIVACY_LAST_UPDATED)}</time>
        </p>
        <p className="mt-2 max-w-prose text-md leading-7 text-muted-foreground">{PRIVACY_INTRO}</p>
      </div>

      <div className="grid gap-8 lg:grid-cols-[240px_minmax(0,1fr)]">
        <aside className="lg:sticky lg:top-6 lg:self-start">
          <details className="rounded-lg border border-border bg-card p-3 lg:hidden">
            <summary className="cursor-pointer text-sm font-medium text-foreground">Table of contents</summary>
            <TableOfContents className="mt-3" />
          </details>
          <TableOfContents className="hidden lg:block" />
        </aside>

        <article className="flex min-w-0 flex-col gap-10">
          {PRIVACY_SECTIONS.map((s, i) => (
            <section key={s.id} id={s.id} aria-labelledby={`${s.id}-title`} className="scroll-mt-6">
              <h2
                id={`${s.id}-title`}
                className="group mb-3 flex items-baseline gap-2 text-xl font-semibold tracking-tight text-foreground"
              >
                <span className="tabular-nums text-subtle-foreground">{i + 1}.</span>
                <span>{s.title}</span>
                <a
                  href={`#${s.id}`}
                  aria-label={`Link to section: ${s.title}`}
                  className="rounded-sm text-subtle-foreground opacity-0 transition-opacity group-hover:opacity-100 hover:text-foreground focus-visible:opacity-100"
                >
                  <Link2 className="size-4" aria-hidden />
                </a>
              </h2>
              <div className="flex max-w-prose flex-col gap-3 text-base leading-7 text-muted-foreground [&_strong]:font-semibold [&_strong]:text-foreground">
                {s.body}
              </div>
            </section>
          ))}
        </article>
      </div>
    </PublicShell>
  )
}
