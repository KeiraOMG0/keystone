import { Gauge, Play } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Alert } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { describeApiError } from "@/lib/apiError"
import { useLabLatency } from "@/api/debugLab"

/**
 * Lab: internal API health/latency matrix. The dashboard backend times a fixed
 * list of read-only internal GET routes one after another (the real dashboard
 * -> bot round trip) and reports status + ms. Not disabled while the bot looks
 * offline: that is exactly when the matrix is useful.
 */
export function LatencyCard() {
  const probe = useLabLatency()
  const r = probe.data
  const max = r ? Math.max(1, ...r.results.map((x) => x.ms)) : 1
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Gauge className="size-4" aria-hidden /> Internal API latency
        </CardTitle>
        <CardDescription>Times read-only bot API routes from the dashboard process, one at a time.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={() => probe.mutate()} loading={probe.isPending} disabled={probe.isPending}>
            <Play /> {r ? "Run again" : "Run"}
          </Button>
          {r ? <span className="text-xs text-muted-foreground">Total {r.total_ms} ms</span> : null}
        </div>
        {probe.isError ? <Alert variant="danger" title="Could not run the probes">{describeApiError(probe.error)}</Alert> : null}
        {r ? (
          <div className="overflow-auto rounded-lg border border-border">
            <table className="w-full text-xs">
              <thead className="bg-card text-left text-muted-foreground">
                <tr>
                  <th className="px-2 py-1 font-medium">Route</th>
                  <th className="px-2 py-1 font-medium">Status</th>
                  <th className="px-2 py-1 text-right font-medium">ms</th>
                  <th className="w-1/3 px-2 py-1 font-medium">
                    <span className="sr-only">Bar</span>
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {r.results.map((x) => {
                  const ok = x.status !== null && x.status < 400
                  return (
                    <tr key={x.path}>
                      <td className="px-2 py-1">
                        <span className="text-foreground">{x.name}</span>
                        <span className="block font-mono text-[11px] text-muted-foreground">{x.path}</span>
                      </td>
                      <td className="px-2 py-1">
                        <Badge variant={ok ? "success" : x.status === null ? "destructive" : "warning"}>
                          {x.status ?? x.error ?? "error"}
                        </Badge>
                      </td>
                      <td className="px-2 py-1 text-right font-mono">{x.ms}</td>
                      <td className="px-2 py-1">
                        <div className="h-1.5 rounded-full bg-muted">
                          <div
                            className={ok ? "h-1.5 rounded-full bg-primary" : "h-1.5 rounded-full bg-danger"}
                            style={{ width: `${Math.max(2, (x.ms / max) * 100)}%` }}
                          />
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : !probe.isPending ? (
          <p className="text-sm text-muted-foreground">Not run yet.</p>
        ) : null}
      </CardContent>
    </Card>
  )
}
