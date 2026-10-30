import * as React from "react"
import { Tooltip as TooltipPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

const TooltipProvider = ({ children }: { children: React.ReactNode }) => (
  <TooltipPrimitive.Provider delayDuration={500} skipDelayDuration={200}>
    {children}
  </TooltipPrimitive.Provider>
)

/** Convenience wrapper: <Tip label="Copy ID"><Button .../></Tip>. Not for essential info. */
function Tip({
  label,
  side = "top",
  children,
}: {
  label: React.ReactNode
  side?: "top" | "right" | "bottom" | "left"
  children: React.ReactElement
}) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className={cn(
            "z-[70] rounded-md bg-foreground px-2 py-1 text-xs font-medium text-background shadow-md data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in-0"
          )}
        >
          {label}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  )
}

export { TooltipProvider, Tip }
