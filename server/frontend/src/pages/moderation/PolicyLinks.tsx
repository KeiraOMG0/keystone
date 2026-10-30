import { FileText, ShieldCheck } from "lucide-react"
import { Button } from "@/components/ui/button"

/**
 * Moderation page header actions: open the public Terms of Service and
 * Privacy Policy in a new tab (handy to quote or link to a member). Plain
 * links, so they work while the bot is offline.
 */
export function PolicyLinks() {
  return (
    <>
      <Button asChild variant="secondary" size="sm">
        <a href="/terms" target="_blank" rel="noreferrer">
          <FileText aria-hidden />
          Terms of Service
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
      </Button>
      <Button asChild variant="secondary" size="sm">
        <a href="/privacy" target="_blank" rel="noreferrer">
          <ShieldCheck aria-hidden />
          Privacy Policy
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
      </Button>
    </>
  )
}
