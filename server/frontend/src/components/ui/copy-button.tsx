import { useState } from "react"
import { Check, Copy } from "lucide-react"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

export function CopyButton({ value, className, label = "Copy ID" }: { value: string; className?: string; label?: string }) {
  const [copied, setCopied] = useState(false)

  function handleCopy() {
    void navigator.clipboard?.writeText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        aria-label={label}
        onClick={handleCopy}
        className={cn(className)}
      >
        {copied ? <Check className="size-3 text-success" /> : <Copy className="size-3" />}
      </Button>
      <span className="sr-only" aria-live="polite">
        {copied ? "Copied" : ""}
      </span>
    </>
  )
}
