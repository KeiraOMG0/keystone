import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { AlertCircle, AlertTriangle, CheckCircle2, Info } from "lucide-react"

import { cn } from "@/lib/utils"

const alertVariants = cva("flex items-start gap-2.5 rounded-[10px] px-4 py-3 text-sm leading-5", {
  variants: {
    variant: {
      info: "bg-info-soft text-info",
      success: "bg-success-soft text-success",
      warning: "bg-warning-soft text-warning",
      danger: "bg-danger-soft text-danger",
    },
  },
  defaultVariants: { variant: "info" },
})

const ICONS = { info: Info, success: CheckCircle2, warning: AlertTriangle, danger: AlertCircle }

/**
 * Banner / notice. `danger` and `warning` use role="alert"; info/success
 * use role="status". `action` renders at the right (e.g. a Retry button).
 */
function Alert({
  className,
  variant = "info",
  title,
  action,
  children,
  ...props
}: Omit<React.ComponentProps<"div">, "title"> &
  VariantProps<typeof alertVariants> & { title?: React.ReactNode; action?: React.ReactNode }) {
  const Icon = ICONS[variant ?? "info"]
  return (
    <div
      data-slot="alert"
      role={variant === "danger" || variant === "warning" ? "alert" : "status"}
      className={cn(alertVariants({ variant }), className)}
      {...props}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children ? <div className={cn(title && "mt-0.5")}>{children}</div> : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  )
}

export { Alert }
