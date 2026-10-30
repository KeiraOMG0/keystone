import { useEffect, useId, useSyncExternalStore } from "react"

/**
 * ONE shared unsaved-changes guard for the whole dashboard.
 *
 * The app uses <BrowserRouter> (declarative mode), which has no route
 * blockers (`useBlocker` needs a data router), so this module guards every
 * way of leaving a page itself:
 *
 *   tab close / reload / external URL    `beforeunload` (browser's own prompt)
 *   in-app links (NavLink, Link, <a>)    capture-phase document click listener
 *   command palette, `g` hotkeys         they call `guardedNavigate(to, go)`
 *   browser Back / Forward               capture-phase `popstate` listener that
 *                                        runs BEFORE React Router's own one, undoes
 *                                        the history move and asks first
 *
 * In-app cases show ONE styled confirm dialog (`<UnsavedChangesDialog>`,
 * mounted once in AppShell); without that host mounted they fall back to
 * window.confirm. Only a change of PATHNAME is guarded: a ?tab= or #hash
 * change on the same page keeps the page (and its draft) mounted.
 *
 * Usage (one line per form):  useUnsavedChangesGuard(dirty)
 * SaveBar calls it for every form that uses it.
 */

export const DEFAULT_UNSAVED_MESSAGE = "You have unsaved changes on this page. If you leave now they will be lost."

/** What the host must do when the user confirms leaving. */
export type LeaveAction =
  | { kind: "navigate"; to: string } // react-router navigate(to)
  | { kind: "document"; href: string } // full page load (non-SPA URL such as /api/...)
  | { kind: "history"; delta: number } // replay a Back/Forward move
  | { kind: "run"; run: () => void } // caller-supplied navigation (palette, hotkeys)

export interface PendingLeave {
  message: string
  action: LeaveAction
}

const guards = new Map<string, string>() // guard id -> message (only dirty guards are registered)
let pending: PendingLeave | null = null
const listeners = new Set<() => void>()
let hostCount = 0
/** Set while a confirmed navigation is under way, so our own listeners let it through. */
let bypass = false
/** The history index / pathname React Router last committed (tracked by the host). */
let currentIdx: number | null = null
let currentPath = typeof window !== "undefined" ? window.location.pathname : "/"
/** Number of our own corrective history.go() calls whose popstate must be swallowed. */
let swallowPops = 0

function emit() {
  for (const l of listeners) l()
}

function activeMessage(): string | null {
  if (bypass) return null
  for (const msg of guards.values()) return msg
  return null
}

/** True while at least one mounted form has unsaved changes. */
export function hasUnsavedChanges(): boolean {
  return activeMessage() !== null
}

function pathOf(to: string): string {
  try {
    return new URL(to, window.location.href).pathname
  } catch {
    return to
  }
}

function request(action: LeaveAction, message: string): void {
  if (hostCount > 0) {
    pending = { message, action }
    emit()
    return
  }
  // No dialog host mounted (should not happen inside the shell): native confirm.
  if (window.confirm(message)) performLeave(action, null)
}

/**
 * Run a confirmed navigation with the guards disabled until it has happened.
 * `navigate` is react-router's navigate (from the host); null only in the
 * window.confirm fallback, where "navigate" actions use history directly.
 */
export function performLeave(action: LeaveAction, navigate: ((to: string) => void) | null): void {
  bypass = true
  const release = () => {
    bypass = false
  }
  switch (action.kind) {
    case "navigate":
      if (navigate) navigate(action.to)
      else window.location.assign(action.to)
      window.setTimeout(release, 0)
      return
    case "document":
      // beforeunload checks `bypass`; keep it set while the page unloads.
      window.location.assign(action.href)
      window.setTimeout(release, 3000)
      return
    case "history":
      window.history.go(action.delta) // the popstate handler sees `bypass` and releases it
      window.setTimeout(release, 1000) // safety net if the move was a no-op
      return
    case "run":
      action.run()
      window.setTimeout(release, 0)
      return
  }
}

/**
 * For programmatic in-app navigation from global UI (command palette, `g`
 * hotkeys): runs `go()` now, or asks first when a dirty form would be left.
 */
export function guardedNavigate(to: string, go: () => void): void {
  const message = activeMessage()
  if (!message || pathOf(to) === currentPath) {
    go()
    return
  }
  request({ kind: "run", run: go }, message)
}

// ---------------------------------------------------------------------------
// Global listeners (installed once, cheap no-ops while nothing is dirty)
// ---------------------------------------------------------------------------

function onBeforeUnload(e: BeforeUnloadEvent) {
  if (!activeMessage()) return
  e.preventDefault()
  e.returnValue = "" // legacy browsers need returnValue set
}

function onClickCapture(e: MouseEvent) {
  const message = activeMessage()
  if (!message) return
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
  const a = (e.target as Element | null)?.closest?.("a[href]") as HTMLAnchorElement | null
  if (!a) return
  if ((a.target && a.target !== "_self") || a.hasAttribute("download")) return
  if (a.origin !== window.location.origin) return // leaves the site: beforeunload handles it
  if (a.pathname === window.location.pathname) return // same page (?tab= / #hash): nothing is lost
  // Stop the click before React Router's <Link> handler (React listens on the
  // root container, below document) and before the browser follows the link.
  e.preventDefault()
  e.stopPropagation()
  const to = a.pathname + a.search + a.hash
  request(a.pathname.startsWith("/api/") ? { kind: "document", href: a.href } : { kind: "navigate", to }, message)
}

function historyIdx(): number | null {
  const state = window.history.state as { idx?: unknown } | null
  return state && typeof state.idx === "number" ? state.idx : null
}

/**
 * Registered with `capture: true` on window: for an event targeted at window,
 * capturing listeners run before non-capturing ones (DOM spec), so this runs
 * before React Router's popstate handler and can stop it.
 */
function onPopStateCapture(e: PopStateEvent) {
  if (swallowPops > 0) {
    // Our own corrective history.go(): the router never saw the move, hide this too.
    swallowPops -= 1
    e.stopImmediatePropagation()
    return
  }
  if (bypass) {
    bypass = false // the confirmed Back/Forward move: let the router handle it
    return
  }
  const message = activeMessage()
  if (!message) return
  if (window.location.pathname === currentPath) return // only the query/hash changed
  const nextIdx = historyIdx()
  if (currentIdx === null || nextIdx === null || nextIdx === currentIdx) return // cannot undo reliably: allow
  const delta = nextIdx - currentIdx
  e.stopImmediatePropagation()
  swallowPops += 1
  window.history.go(-delta) // put the URL back where the router thinks it is
  request({ kind: "history", delta }, message)
}

let installed = false
function install() {
  if (installed || typeof window === "undefined") return
  installed = true
  window.addEventListener("beforeunload", onBeforeUnload)
  document.addEventListener("click", onClickCapture, true)
  window.addEventListener("popstate", onPopStateCapture, true)
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

/**
 * Guard the current page while `dirty`: leaving it by link, palette, hotkey,
 * Back/Forward, reload or tab close asks for confirmation first. Call it once
 * per form; several guarded forms on a page share one prompt.
 */
export function useUnsavedChangesGuard(dirty: boolean, message: string = DEFAULT_UNSAVED_MESSAGE): void {
  const id = useId()
  useEffect(() => {
    install()
    if (!dirty) return
    guards.set(id, message)
    return () => {
      guards.delete(id)
    }
  }, [id, dirty, message])
}

function subscribe(l: () => void) {
  listeners.add(l)
  return () => {
    listeners.delete(l)
  }
}

/** For <UnsavedChangesDialog> only: the pending leave request, if any. */
export function usePendingLeave(): PendingLeave | null {
  return useSyncExternalStore(subscribe, () => pending, () => null)
}

/** For <UnsavedChangesDialog> only: registers the dialog host. */
export function registerLeaveHost(): () => void {
  install()
  hostCount += 1
  return () => {
    hostCount -= 1
  }
}

/** For <UnsavedChangesDialog> only: record the location React Router committed. */
export function trackCommittedLocation(pathname: string): void {
  currentPath = pathname
  currentIdx = historyIdx()
}

/** For <UnsavedChangesDialog> only: close the dialog (confirmed = leave). */
export function resolvePendingLeave(): PendingLeave | null {
  const p = pending
  pending = null
  emit()
  return p
}
