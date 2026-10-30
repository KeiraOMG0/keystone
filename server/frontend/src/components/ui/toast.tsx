import * as React from "react"
import { AlertCircle, CheckCircle2, Info, X } from "lucide-react"

import { cn } from "@/lib/utils"

type ToastVariant = "default" | "success" | "destructive" | "info"

type ToastAction = { label: string; onClick: () => void }

type Toast = {
  id: string
  title: string
  description?: string
  variant: ToastVariant
  action?: ToastAction
}

type ToastOptions = {
  title: string
  description?: string
  variant?: ToastVariant
  /** e.g. { label: "Undo", onClick }. Toasts with an action stay 8 s instead of 4 s. */
  action?: ToastAction
}

type ToastContextValue = {
  toasts: Toast[]
  toast: (opts: ToastOptions) => string
  dismiss: (id: string) => void
}

const ToastContext = React.createContext<ToastContextValue | null>(null)

const MAX_VISIBLE = 3

function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = React.useState<Toast[]>([])

  const dismiss = React.useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const toast = React.useCallback((opts: ToastOptions) => {
    const id = crypto.randomUUID()
    setToasts((prev) =>
      [
        ...prev,
        {
          id,
          title: opts.title,
          description: opts.description,
          variant: opts.variant ?? "default",
          action: opts.action,
        },
      ].slice(-MAX_VISIBLE)
    )
    return id
  }, [])

  const value = React.useMemo(() => ({ toasts, toast, dismiss }), [toasts, toast, dismiss])

  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>
}

function useToast() {
  const ctx = React.useContext(ToastContext)
  if (!ctx) {
    throw new Error("useToast must be used within a ToastProvider")
  }
  return { toast: ctx.toast, dismiss: ctx.dismiss }
}

const VARIANT_ICON: Record<ToastVariant, { icon: React.ComponentType<{ className?: string }>; className: string }> = {
  default: { icon: Info, className: "text-primary" },
  info: { icon: Info, className: "text-info" },
  success: { icon: CheckCircle2, className: "text-success" },
  destructive: { icon: AlertCircle, className: "text-danger" },
}

function ToastItem({ t, dismiss }: { t: Toast; dismiss: (id: string) => void }) {
  const [paused, setPaused] = React.useState(false)
  const persistent = t.variant === "destructive"
  const duration = t.action ? 8000 : 4000

  // Auto-dismiss after `duration`, restarting while hovered/focused (pause).
  React.useEffect(() => {
    if (persistent || paused) return
    const h = window.setTimeout(() => dismiss(t.id), duration)
    return () => window.clearTimeout(h)
  }, [persistent, paused, duration, dismiss, t.id])

  const { icon: Icon, className: iconClass } = VARIANT_ICON[t.variant]

  return (
    <div
      data-slot="toast"
      data-variant={t.variant}
      role={persistent ? "alert" : "status"}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
      className="pointer-events-auto flex w-full gap-3 rounded-[10px] border border-border bg-card p-3 text-card-foreground shadow-md motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-2"
    >
      <Icon className={cn("mt-0.5 size-5 shrink-0", iconClass)} aria-hidden />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <p className="text-sm leading-5 font-semibold text-foreground">{t.title}</p>
        {t.description ? <p className="text-xs leading-4 text-muted-foreground">{t.description}</p> : null}
        {t.action ? (
          <button
            type="button"
            onClick={() => {
              t.action?.onClick()
              dismiss(t.id)
            }}
            className="mt-1 w-fit rounded-sm text-sm font-medium text-primary underline-offset-4 hover:underline"
          >
            {t.action.label}
          </button>
        ) : null}
      </div>
      <button
        type="button"
        aria-label="Dismiss notification"
        onClick={() => dismiss(t.id)}
        className="h-fit shrink-0 rounded-md p-0.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
      >
        <X className="size-4" />
      </button>
    </div>
  )
}

function Toaster() {
  const ctx = React.useContext(ToastContext)
  if (!ctx) {
    throw new Error("Toaster must be used within a ToastProvider")
  }
  const { toasts, dismiss } = ctx

  return (
    <div
      data-slot="toaster"
      role="region"
      aria-label="Notifications"
      className="pointer-events-none fixed right-4 bottom-20 left-4 z-[60] flex flex-col items-center gap-2 md:right-4 md:bottom-4 md:left-auto md:w-[340px] md:items-stretch"
    >
      {toasts.map((t) => (
        <ToastItem key={t.id} t={t} dismiss={dismiss} />
      ))}
    </div>
  )
}

export { ToastProvider, Toaster, useToast }
