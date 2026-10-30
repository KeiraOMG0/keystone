import * as React from "react"

import { cn } from "@/lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "h-9 rounded-lg border border-input bg-card px-3 text-base text-foreground outline-none transition-[color,box-shadow,border-color] duration-[120ms] placeholder:text-subtle-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/25 disabled:cursor-not-allowed disabled:bg-muted disabled:text-subtle-foreground read-only:bg-muted aria-invalid:border-danger aria-invalid:ring-3 aria-invalid:ring-danger/20 max-md:h-11",
        className
      )}
      {...props}
    />
  )
}

export { Input }
