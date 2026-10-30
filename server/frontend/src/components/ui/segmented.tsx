import * as React from "react"
import { ToggleGroup as ToggleGroupPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

export interface SegmentedOption<T extends string> {
  value: T
  label: React.ReactNode
  icon?: React.ReactNode
}

/**
 * Single-select segmented control (density, theme, feed filter, refresh
 * interval). Unlike raw Radix ToggleGroup, clicking the active segment does
 * not deselect it.
 */
function Segmented<T extends string>({
  value,
  onValueChange,
  options,
  label,
  size = "default",
  className,
}: {
  value: T
  onValueChange: (v: T) => void
  options: SegmentedOption<T>[]
  /** Accessible name of the group. */
  label: string
  size?: "default" | "sm"
  className?: string
}) {
  return (
    <ToggleGroupPrimitive.Root
      type="single"
      value={value}
      onValueChange={(v) => {
        if (v) onValueChange(v as T)
      }}
      aria-label={label}
      className={cn("inline-flex items-center gap-0.5 rounded-lg bg-muted p-0.5", className)}
    >
      {options.map((o) => (
        <ToggleGroupPrimitive.Item
          key={o.value}
          value={o.value}
          className={cn(
            "inline-flex items-center justify-center gap-1.5 rounded-md px-2.5 font-medium whitespace-nowrap text-muted-foreground transition-colors duration-[120ms] outline-none hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring data-[state=on]:bg-card data-[state=on]:text-foreground data-[state=on]:shadow-sm [&_svg]:size-3.5",
            size === "sm" ? "h-7 text-xs" : "h-8 text-sm"
          )}
        >
          {o.icon}
          {o.label}
        </ToggleGroupPrimitive.Item>
      ))}
    </ToggleGroupPrimitive.Root>
  )
}

export { Segmented }
