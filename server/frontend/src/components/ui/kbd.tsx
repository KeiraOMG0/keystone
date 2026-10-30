import { cn } from "@/lib/utils"

/** Keycap. */
function Kbd({ className, ...props }: React.ComponentProps<"kbd">) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-[4px] border border-b-2 border-border bg-card px-1 font-mono text-2xs text-muted-foreground",
        className
      )}
      {...props}
    />
  )
}

export { Kbd }
