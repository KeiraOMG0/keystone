import { Eraser } from "lucide-react"
import { useClearDiagnosticsCache } from "@/api/diagnostics"
import { Button } from "@/components/ui/button"
import { useToast } from "@/components/ui/toast"
import { useBotOffline } from "@/hooks/useBotOffline"
import { apiErrorMessage } from "@/pages/honeypot/format"
import { Sub } from "@/pages/diagnostics/widgets"

/**
 * Diagnostics > System: "Clear cached data". POST /api/diagnostics/cache/clear
 * (staff) empties the bot's diagnostics TTL cache (database, cookies, config,
 * Discord registry, children, ...) and the log-tail cache; the live hub then
 * pushes a full fresh snapshot to every open Diagnostics page, so the page
 * updates by itself. Needs the bot, so it is disabled while the bot is offline.
 */
export function ClearCacheButton() {
  const { toast } = useToast()
  const { offline, reason } = useBotOffline()
  const clear = useClearDiagnosticsCache()

  return (
    <Sub title="Cached data">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="max-w-2xl text-sm text-muted-foreground">
          Slow-changing parts of this page (database counts, cookies, config and versions, cogs and permissions, child
          processes, log tails) are cached on the bot for 10 seconds to 5 minutes. Clear the cache to recompute them now.
        </p>
        <Button
          variant="secondary"
          size="sm"
          loading={clear.isPending}
          disabled={clear.isPending || offline}
          title={reason}
          onClick={() =>
            clear.mutate(undefined, {
              onSuccess: (res) =>
                toast({
                  title: "Diagnostics cache cleared",
                  description: `${res.entries} cached ${res.entries === 1 ? "entry" : "entries"} dropped${res.log_cache_cleared ? ", log tails included" : ""}. Fresh data arrives with the next update.`,
                  variant: "success",
                }),
              onError: (err) => toast({ title: "Cache not cleared", description: apiErrorMessage(err), variant: "destructive" }),
            })
          }
        >
          <Eraser /> Clear cached data
        </Button>
      </div>
    </Sub>
  )
}
