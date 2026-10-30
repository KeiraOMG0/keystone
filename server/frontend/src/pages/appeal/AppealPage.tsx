import { useEffect, useState, type FormEvent, type ReactNode } from "react"
import { useParams, useSearchParams } from "react-router-dom"
import { LogIn } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Skeleton } from "@/components/ui/skeleton"
import { Alert } from "@/components/ui/alert"
import { Field, CharCounter } from "@/components/ui/field"
import { PublicFooter } from "@/components/layout/PublicShell"
import {
  appealErrorCode,
  appealLoginUrl,
  appealSignOut,
  fetchAppealMe,
  lookupAppealToken,
  submitSignedInAppeal,
} from "@/api/appeals"
import type { PublicAppealErrorCode, PublicAppealLookup, PublicAppealMe, PublicAppealOutcome } from "@/types/api"

type LinkState =
  | { kind: "none" }
  | { kind: "loading" }
  | { kind: "invalid" }
  | { kind: "error"; code: PublicAppealErrorCode }
  | { kind: "ready"; info: PublicAppealLookup }

const NAME_MAX = 100
const MESSAGE_MAX = 1500

const SUBMIT_ERRORS: Record<PublicAppealErrorCode, string> = {
  not_eligible: "We couldn't find a removal by the trap channel for this Discord account, so there is nothing to appeal.",
  already_submitted: "An appeal has already been submitted for this account. A moderator will review it.",
  rate_limited: "Too many attempts. Please wait a minute and try again.",
  unavailable: "The appeal system is temporarily unavailable. Please try again in a little while.",
  invalid_request: "Please check the form. Name is required.",
  not_signed_in: "Your sign-in expired. Sign in with Discord again.",
  signin_required: "Appeals need a Discord sign-in. Sign in with Discord and try again.",
}

/** ?rejoin= values set by the sign-in callback (server/routes/appeal_auth.py). */
const REJOIN_RESULT: Record<string, { title: string; text: string; tone: "success" | "default" | "error" }> = {
  joined: {
    title: "You're back in the server",
    text: "Welcome back. Please make sure your account is secured (new password, 2FA on), and follow the server's verification steps again.",
    tone: "success",
  },
  already_member: { title: "You're already in the server", text: "Nothing else to do.", tone: "default" },
  not_approved: { title: "Not approved yet", text: "Your appeal has not been approved, so you can't rejoin yet.", tone: "error" },
  still_banned: {
    title: "You're still banned",
    text: "You could not be added because a ban is in place. Please contact the server staff.",
    tone: "error",
  },
  too_many_guilds: {
    title: "You're in too many servers",
    text: "Discord limits how many servers one account can join. Leave one and try again, or use the invite link below.",
    tone: "error",
  },
  login_expired: { title: "The Discord login expired", text: "Please try Rejoin server again.", tone: "error" },
  failed: {
    title: "Couldn't add you right now",
    text: "Please try again in a moment, or use the invite link if there is one.",
    tone: "error",
  },
}

const SIGNIN_RESULT: Record<string, { title: string; text: string }> = {
  failed: { title: "Sign-in didn't work", text: "Please try signing in with Discord again." },
  cancelled: { title: "Sign-in cancelled", text: "You need to sign in with Discord to appeal. You can do it whenever you're ready." },
}

function formatWhen(iso: string | null): string | null {
  if (!iso) return null
  const d = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`)
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}

/** The person's own appeal outcome, with the rejoin options when approved. */
function OutcomeView({ outcome }: { outcome: PublicAppealOutcome }) {
  const submitted = formatWhen(outcome.submitted_at)
  const reviewed = formatWhen(outcome.reviewed_at)
  if (outcome.status === "pending") {
    return (
      <Notice title="Your appeal is waiting for review">
        A moderator will look at it soon.{submitted ? ` Submitted ${submitted}.` : ""} Come back to this page any time to
        check; you do not need to submit again.
      </Notice>
    )
  }
  if (outcome.status === "denied") {
    return (
      <Notice title="Your appeal was not approved" tone="error">
        A moderator reviewed it{reviewed ? ` on ${reviewed}` : ""} and the ban stays in place.
      </Notice>
    )
  }
  if (outcome.status !== "approved") return null
  return (
    <div className="flex flex-col gap-4">
      <Notice title="Your appeal was approved" tone="success">
        You can rejoin the server{reviewed ? ` (approved ${reviewed})` : ""}. Please secure your account first: new
        password, two-factor authentication on, and remove any authorized apps you don't recognise.
      </Notice>
      <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-6 shadow-sm max-md:p-4">
        <Button asChild size="lg" className="self-start max-sm:w-full">
          <a href={appealLoginUrl("rejoin")}>
            <LogIn aria-hidden />
            {outcome.rejoined ? "Rejoin server again" : "Rejoin server"}
          </a>
        </Button>
        <p className="text-sm text-muted-foreground">
          Discord will ask you to let the bot add you to the server. That permission is used once, right away, and is
          then cancelled; nothing is kept.
        </p>
        {outcome.rejoin_invite_url ? (
          <p className="text-sm text-muted-foreground">
            Or use this invite:{" "}
            <a
              href={outcome.rejoin_invite_url}
              className="break-all text-primary underline underline-offset-4"
              rel="noreferrer noopener"
              target="_blank"
            >
              {outcome.rejoin_invite_url}
            </a>
          </p>
        ) : null}
      </div>
    </div>
  )
}

function SignInCard({ unavailable = false }: { unavailable?: boolean }) {
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-6 shadow-sm max-md:p-4">
      <h2 className="text-lg font-semibold">Sign in with Discord to appeal</h2>
      <p className="text-sm text-muted-foreground">
        Appeals need a Discord sign-in so we know the appeal really comes from the banned account, and so you can see the
        result here later. We only read your username and ID; nothing is stored from your Discord login.
      </p>
      {unavailable ? (
        <p className="text-sm text-danger">Sign-in is unavailable right now. Please try again in a little while.</p>
      ) : null}
      <Button asChild size="lg" className="self-start max-sm:w-full">
        <a href={appealLoginUrl("appeal")}>
          <LogIn aria-hidden />
          Sign in with Discord
        </a>
      </Button>
    </div>
  )
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-svh flex-col bg-background text-foreground">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-card px-4 md:px-8">
        <div className="flex items-center gap-2.5">
          <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-sm font-semibold text-primary-foreground" aria-hidden>
            K
          </div>
          <span className="text-md font-semibold">Gamer Girls HQ</span>
        </div>
        <span className="text-sm font-medium text-muted-foreground">Ban appeal</span>
      </header>
      <main className="mx-auto flex w-full max-w-[560px] flex-1 flex-col gap-6 px-4 py-8 md:py-12">
        <div className="flex flex-col gap-2">
          <h1 className="text-3xl font-semibold tracking-tight">Ban appeal</h1>
          <p className="text-md leading-6 text-muted-foreground">
            This server removes accounts that post in the trap channel automatically, because hacked accounts and
            spam bots do that. If that was your real account, tell us what happened and a moderator will review it.
          </p>
        </div>
        {children}
      </main>
      <PublicFooter />
    </div>
  )
}

function Notice({ title, children, tone = "default" }: { title: string; children: ReactNode; tone?: "default" | "success" | "error" }) {
  return (
    <Alert variant={tone === "success" ? "success" : tone === "error" ? "danger" : "info"} title={title}>
      {children}
    </Alert>
  )
}

type MeState = { kind: "loading" } | { kind: "error" } | { kind: "ready"; me: PublicAppealMe }

/**
 * PUBLIC. Submitting an appeal REQUIRES Discord sign-in (server/routes/appeal_auth.py);
 * there is no typed-User-ID form any more (2026-10-29).
 *  - signed in: the form, or their outcome, or "no ban found".
 *  - /appeal/:token (the link from the ban DM): read-only; shows their outcome once they
 *    have appealed, otherwise "appealing as <name>" plus the sign-in card.
 *  - not signed in and no outcome to show: the sign-in card only.
 * Approved -> "Rejoin server" (Discord login with guilds.join, used once) and the invite link.
 */
export default function AppealPage() {
  const { token } = useParams<{ token?: string }>()
  const [params, setParams] = useSearchParams()
  const [link, setLink] = useState<LinkState>(token ? { kind: "loading" } : { kind: "none" })
  const [me, setMe] = useState<MeState>({ kind: "loading" })
  const [name, setName] = useState("")
  const [message, setMessage] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<PublicAppealErrorCode | null>(null)
  const [done, setDone] = useState(false)
  // Result of the Discord sign-in round trip, read once from the URL and then removed from it.
  const [flash] = useState(() => ({ signin: params.get("signin"), rejoin: params.get("rejoin") }))

  useEffect(() => {
    if (params.has("signin") || params.has("rejoin")) setParams({}, { replace: true })
  }, [params, setParams])

  useEffect(() => {
    if (!token) return
    let cancelled = false
    lookupAppealToken(token)
      .then((info) => {
        if (cancelled) return
        setLink({ kind: "ready", info })
      })
      .catch((err) => {
        if (cancelled) return
        const code = appealErrorCode(err)
        setLink(code === "not_eligible" || code === "invalid_request" ? { kind: "invalid" } : { kind: "error", code })
      })
    return () => {
      cancelled = true
    }
  }, [token])

  function loadMe() {
    setMe({ kind: "loading" })
    fetchAppealMe()
      .then((data) => setMe({ kind: "ready", me: data }))
      .catch(() => setMe({ kind: "error" }))
  }

  useEffect(() => {
    let cancelled = false
    fetchAppealMe()
      .then((data) => !cancelled && setMe({ kind: "ready", me: data }))
      .catch(() => !cancelled && setMe({ kind: "error" }))
    return () => {
      cancelled = true
    }
  }, [])

  const signedIn = me.kind === "ready" && me.me.signed_in ? me.me : null
  const canSubmit = name.trim().length > 0 && !!signedIn && !submitting

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setSubmitError(null)
    try {
      await submitSignedInAppeal({ name: name.trim(), message: message.trim() })
      setDone(true)
    } catch (err) {
      setSubmitError(appealErrorCode(err))
    } finally {
      setSubmitting(false)
    }
  }

  async function signOut() {
    try {
      await appealSignOut()
    } finally {
      loadMe()
    }
  }

  const rejoinNote = flash.rejoin ? REJOIN_RESULT[flash.rejoin] ?? REJOIN_RESULT.failed : null
  const signinNote = flash.signin && flash.signin !== "ok" ? SIGNIN_RESULT[flash.signin] ?? SIGNIN_RESULT.failed : null
  const banners = (
    <>
      {rejoinNote ? (
        <Notice title={rejoinNote.title} tone={rejoinNote.tone}>
          {rejoinNote.text}
        </Notice>
      ) : null}
      {signinNote ? (
        <Notice title={signinNote.title} tone="error">
          {signinNote.text}
        </Notice>
      ) : null}
    </>
  )

  if ((token && link.kind === "loading") || me.kind === "loading") {
    return (
      <Shell>
        <div className="flex flex-col gap-3" aria-busy="true" aria-label="Loading your appeal">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-32 w-full" />
        </div>
      </Shell>
    )
  }

  if (link.kind === "invalid") {
    return (
      <Shell>
        <Notice title="This link is invalid or has expired" tone="error">
          Double-check the link from your DM, or sign in with Discord on the{" "}
          <a href="/appeal" className="text-primary underline underline-offset-4">
            appeal page
          </a>
          .
        </Notice>
      </Shell>
    )
  }

  if (link.kind === "error") {
    return (
      <Shell>
        <Notice title={link.code === "rate_limited" ? "Too many requests" : "Something went wrong"} tone="error">
          {SUBMIT_ERRORS[link.code]}
        </Notice>
      </Shell>
    )
  }

  if (done) {
    return (
      <Shell>
        <Notice title="Appeal submitted" tone="success">
          A moderator will review it. Come back to this page and sign in with Discord to see the result.
        </Notice>
      </Shell>
    )
  }

  // Someone whose identity is known (DM link or Discord sign-in) and who already appealed: show the outcome.
  // Signed in wins (their own live state); otherwise the DM link can still show the outcome.
  const outcome: PublicAppealOutcome | null = signedIn
    ? signedIn.eligible
      ? null
      : signedIn.appeal
    : link.kind === "ready" && link.info.status !== "invited"
      ? (link.info as PublicAppealLookup)
      : null

  const signedInHeader = signedIn ? (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border bg-card px-4 py-3 text-sm">
      <span>
        Signed in as <span className="font-medium text-foreground">{signedIn.user.username || signedIn.user.id}</span>
      </span>
      <button type="button" onClick={() => void signOut()} className="rounded-sm text-primary hover:underline">
        Sign out
      </button>
    </div>
  ) : null

  if (outcome) {
    return (
      <Shell>
        {banners}
        {signedInHeader}
        <OutcomeView outcome={outcome} />
      </Shell>
    )
  }

  if (signedIn && !signedIn.eligible) {
    return (
      <Shell>
        {banners}
        {signedInHeader}
        <Notice title="No honeypot ban found for this account">
          We couldn't find a removal by the trap channel for this Discord account, so there is nothing to appeal here. If
          you were banned some other way, please contact the server staff.
        </Notice>
      </Shell>
    )
  }

  const form = (
    <form onSubmit={onSubmit} className="flex flex-col gap-5 rounded-xl border border-border bg-card p-6 shadow-sm max-md:p-4" noValidate>
      <Field label="Name" required hint="What should we call you?">
        {(p) => (
          <Input
            {...p}
            autoComplete="name"
            maxLength={NAME_MAX}
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="max-md:h-11"
          />
        )}
      </Field>

      <Field label="What happened?" labelAction={<CharCounter value={message.length} max={MESSAGE_MAX} />}>
        {(p) => (
          <Textarea
            {...p}
            rows={6}
            maxLength={MESSAGE_MAX}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Was your account hacked? Did you secure it? Anything else the moderators should know."
          />
        )}
      </Field>

      {submitError ? (
        <Notice title={submitError === "rate_limited" ? "Slow down" : "Could not submit"} tone="error">
          {SUBMIT_ERRORS[submitError]}
        </Notice>
      ) : null}

      <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:items-center">
        <Button type="submit" size="lg" disabled={!canSubmit} loading={submitting} className="max-sm:w-full">
          {submitting ? "Submitting" : "Submit appeal"}
        </Button>
        <p className="text-sm text-muted-foreground">A moderator reviews every appeal.</p>
      </div>
    </form>
  )

  if (signedIn) {
    return (
      <Shell>
        {banners}
        {signedInHeader}
        {form}
      </Shell>
    )
  }

  return (
    <Shell>
      {banners}
      {link.kind === "ready" ? (
        <Notice title={`Appealing as ${link.info.username ?? "your account"}`}>
          Sign in with that Discord account to continue.
        </Notice>
      ) : null}
      <SignInCard unavailable={me.kind === "error"} />
    </Shell>
  )
}
