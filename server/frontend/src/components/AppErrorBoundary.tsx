import { Component, type ErrorInfo, type ReactNode } from "react"
import { isChunkLoadError, reportClientError } from "@/lib/errorReport"

/**
 * Last-resort error screen for the whole app (mounted in main.tsx, outside every
 * provider). Without it any render error or failed lazy-chunk import unmounts the
 * tree and leaves a blank page with no clue (reported 2026-10-29 from an iPhone in
 * Discord's in-app browser). It never reloads by itself: it reports the error to
 * the backend (src/lib/errorReport.ts -> owner-only Debug > Errors) and shows the
 * error text, with a Reload button the person can press.
 *
 * Deliberately self-contained: plain elements + theme tokens, no providers, no
 * shared UI components, since any of those could be what threw.
 */

type State = { error: Error | null; reported: boolean }

export class AppErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null, reported: false }

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[app] render crashed", error, info.componentStack)
    const reported = reportClientError(isChunkLoadError(error) ? "chunk" : "render", error, {
      componentStack: info.componentStack,
    })
    if (reported) this.setState({ reported: true })
  }

  render() {
    const { error, reported } = this.state
    if (!error) return this.props.children
    const stale = isChunkLoadError(error)
    const stack = (error.stack || "").split("\n").slice(0, 6).join("\n")
    return (
      <div className="flex min-h-svh items-center justify-center bg-background p-4 text-foreground">
        <div className="flex w-full max-w-[560px] flex-col gap-4 rounded-xl border border-border bg-card p-6 shadow-sm">
          <h1 className="text-xl font-semibold">The dashboard couldn&apos;t load</h1>
          <p className="text-sm text-muted-foreground">
            {stale
              ? "The dashboard was updated while this page was open. Press Reload to get the new version."
              : "Try reloading. If it keeps happening, take a screenshot of this box and send it to the bot owner."}
            {reported ? " This error was reported automatically." : null}
          </p>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-3 font-mono text-xs">
            {`${error.name}: ${error.message}\n${stack}\n\nPage: ${window.location.pathname}\nBrowser: ${navigator.userAgent}`}
          </pre>
          <div className="flex flex-wrap gap-3">
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="h-10 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground"
            >
              Reload
            </button>
            <a href="/status" className="flex h-10 items-center rounded-md border border-border px-4 text-sm font-medium">
              Bot status
            </a>
          </div>
        </div>
      </div>
    )
  }
}
