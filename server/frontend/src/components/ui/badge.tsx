import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex h-[22px] w-fit shrink-0 items-center gap-1 rounded-full px-2 text-xs font-medium whitespace-nowrap [&_svg]:size-3 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "bg-muted text-foreground",
        success: "bg-success-soft text-success",
        warning: "bg-warning-soft text-warning",
        destructive: "bg-danger-soft text-danger",
        info: "bg-info-soft text-info",
        accent: "bg-primary-soft text-primary-soft-foreground",
        outline: "border border-border bg-transparent text-muted-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

function Badge({
  className,
  variant = "default",
  dot = false,
  children,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants> & { dot?: boolean }) {
  return (
    <span
      data-slot="badge"
      data-variant={variant}
      className={cn(badgeVariants({ variant, className }))}
      {...props}
    >
      {dot ? <span aria-hidden className="size-2 shrink-0 rounded-full bg-current" /> : null}
      {children}
    </span>
  )
}

/** Small numeric pill (nav badges, tab counts). Caps at 99+. */
function CountBadge({ count, className }: { count: number; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-primary-soft px-1.5 text-xs font-semibold text-primary-soft-foreground tabular-nums",
        className
      )}
    >
      {count > 99 ? "99+" : count}
    </span>
  )
}

export { Badge, CountBadge, badgeVariants }
