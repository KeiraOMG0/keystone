import { useState } from "react"
import { Link } from "react-router-dom"
import { Search } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Alert } from "@/components/ui/alert"
import { Skeleton } from "@/components/ui/skeleton"
import { Badge } from "@/components/ui/badge"
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/card"
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table"
import { useMediaQuery } from "@/hooks/useMediaQuery"
import { useStatus, type ServiceStatus } from "@/api/status"
import { PublicFooter } from "@/components/layout/PublicShell"

interface ErrorRow {
  code: string
  name: string
  meaning: string
  fix: string
}

/**
 * HTTP-level errors: these come back with no JSON body (or a body the
 * frontend never gets to see), because they're raised before server/app.py
 * gets a chance to run: either Cloudflare Tunnel/the OS can't reach the
 * dashboard process at all (502/504), or the dashboard is up but the
 * request never reaches a route handler (network failure client-side).
 */
const TRANSPORT_ERRORS: ErrorRow[] = [
  {
    code: "0",
    name: "Network error",
    meaning:
      "The browser couldn't complete the request at all — no response came back. Usually your own connection, or the dashboard process/tunnel is fully down.",
    fix: "Check your internet connection. If it persists, the dashboard process (server/app.py, port 8080) or the Cloudflare Tunnel (cloudflared tunnel run idlesys) may be down on the host.",
  },
  {
    code: "502",
    name: "Bad Gateway",
    meaning:
      "The dashboard backend is reachable, but IT couldn't reach the bot's internal API (internal_api, port 8766) — every dashboard route that needs live Discord data calls it via server/services/internal_client.py, and this is what that call raises as InternalAPIError when it fails.",
    fix: "The Discord bot process (main.py) is probably not running, still starting up, or crashed. Restart it. If it's running, check its logs for a startup error on the internal API's port 8766.",
  },
  {
    code: "503",
    name: "Service Unavailable",
    meaning:
      "The dashboard process itself isn't running or isn't accepting connections yet (e.g. mid-restart).",
    fix: "Start/restart the dashboard: venv/Scripts/python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8080",
  },
  {
    code: "504",
    name: "Gateway Timeout",
    meaning:
      "The dashboard backend accepted the request but took too long to respond — usually the internal API call hanging (see internal_client.py's 10s request timeout / 5s connect timeout) or Discord's API itself being slow.",
    fix: "Usually transient. If it keeps happening, check the bot process isn't stuck/deadlocked.",
  },
]

/** App-level `{"error": "..."}` bodies the dashboard API returns on top of a real HTTP status. */
const APP_ERRORS: ErrorRow[] = [
  {
    code: "401 unauthenticated",
    name: "No session",
    meaning: "You're not logged in, or your session cookie expired.",
    fix: "Log in again with Discord.",
  },
  {
    code: "403 not_a_member",
    name: "Not a server member",
    meaning: "You're logged in, but not a member of the Discord server this dashboard manages.",
    fix: "Join the Discord server with the same account you signed in with, then reload.",
  },
  {
    code: "403 forbidden",
    name: "Missing permission",
    meaning:
      "You're a member, but your roles don't include the specific permission the page/action requires (checked live against your Discord roles on every request — see server/permissions/guards.py).",
    fix: "Ask a server admin to grant your role the required permission on the Permissions page.",
  },
  {
    code: "502 internal_api_unavailable",
    name: "Bot unreachable",
    meaning:
      "The app-level version of the 502 above — the dashboard backend caught InternalAPIError while trying to reach the bot process.",
    fix: "Same as HTTP 502: the bot process is down, starting, or crashed. Restart it.",
  },
  {
    code: "400",
    name: "Invalid request",
    meaning: "The data submitted (a form, a panel/template config, etc.) failed validation.",
    fix: "Check the field the error message points to and correct it. This is a genuine input problem, not a server issue.",
  },
  {
    code: "404",
    name: "Not found",
    meaning:
      "Whatever you're trying to view/edit (a ticket, template, panel, warning, role) no longer exists — usually because it was deleted elsewhere (Discord directly, or another dashboard tab) since your page loaded.",
    fix: "Refresh the page/list to pick up the current state.",
  },
]

function badgeVariant(code: string): "warning" | "destructive" {
  return code.startsWith("40") ? "warning" : "destructive"
}

function anchorFor(code: string): string {
  return `code-${code.replace(/\s+/g, "-")}`
}

function ErrorTable({ rows }: { rows: ErrorRow[] }) {
  const isPhone = useMediaQuery("(max-width: 639px)")
  if (rows.length === 0) {
    return <p className="px-5 py-6 text-center text-sm text-muted-foreground">No error codes match your search.</p>
  }
  if (isPhone) {
    return (
      <ul className="flex flex-col divide-y divide-border">
        {rows.map((row) => (
          <li key={row.code} id={anchorFor(row.code)} className="flex scroll-mt-20 flex-col gap-1.5 px-4 py-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={badgeVariant(row.code)}>
                <span className="font-mono">{row.code}</span>
              </Badge>
              <span className="text-base font-medium">{row.name}</span>
            </div>
            <p className="text-sm text-muted-foreground">{row.meaning}</p>
            <p className="text-sm text-foreground">{row.fix}</p>
          </li>
        ))}
      </ul>
    )
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-44">Code</TableHead>
          <TableHead className="w-44">Name</TableHead>
          <TableHead>What it means</TableHead>
          <TableHead>What to do</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.code} id={anchorFor(row.code)} className="scroll-mt-20 align-top">
            <TableCell>
              <a href={`#${anchorFor(row.code)}`} className="rounded-sm">
                <Badge variant={badgeVariant(row.code)}>
                  <span className="font-mono">{row.code}</span>
                </Badge>
              </a>
            </TableCell>
            <TableCell className="font-medium whitespace-normal">{row.name}</TableCell>
            <TableCell className="whitespace-normal text-muted-foreground">{row.meaning}</TableCell>
            <TableCell className="whitespace-normal text-muted-foreground">{row.fix}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function ServiceRow({ label, service }: { label: string; service: ServiceStatus | undefined }) {
  const isUp = service?.status === "up"
  return (
    <div className="flex items-center justify-between gap-3 py-3">
      <div className="flex items-center gap-2.5">
        {service ? (
          <span className={`size-2 rounded-full ${isUp ? "bg-success" : "bg-danger"}`} aria-hidden />
        ) : (
          <Skeleton className="size-2 rounded-full" />
        )}
        <span className="text-base font-medium text-foreground">{label}</span>
      </div>
      <div className="flex items-center gap-2">
        {service?.detail ? <span className="text-xs text-muted-foreground">{service.detail}</span> : null}
        {service?.latency_ms != null ? (
          <span className="text-xs text-muted-foreground tabular-nums">{service.latency_ms}ms</span>
        ) : null}
        {service ? (
          <Badge variant={isUp ? "success" : "destructive"}>{isUp ? "Operational" : "Down"}</Badge>
        ) : (
          <Skeleton className="h-[22px] w-20 rounded-full" />
        )}
      </div>
    </div>
  )
}

function LiveStatus() {
  const { data, isPending, isError, dataUpdatedAt } = useStatus()
  const allUp = data ? data.dashboard.status === "up" && data.bot.status === "up" && data.internal_api.status === "up" : null

  return (
    <div className="flex flex-col gap-4">
      {isError ? (
        <Alert variant="danger" title="Could not check status">
          Could not reach the dashboard backend at all. It is likely fully down (see "Network error" / 502-504 below).
        </Alert>
      ) : allUp === null ? null : allUp ? (
        <Alert variant="success" title="All systems operational" />
      ) : (
        <Alert variant="danger" title="Some services are down">
          See the rows below, then the error codes that match.
        </Alert>
      )}
      <Card>
        <CardHeader>
          <CardTitle>Live status</CardTitle>
          <CardDescription>
            Auto-refreshes every 10 seconds. This page loads even when everything below is down.
            {dataUpdatedAt
              ? ` Last checked ${new Date(dataUpdatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}.`
              : ""}
          </CardDescription>
        </CardHeader>
        <CardContent className="divide-y divide-border">
          {isError ? (
            <p className="py-3 text-sm text-muted-foreground">No service data available.</p>
          ) : (
            <>
              <ServiceRow label="Website / Dashboard" service={isPending ? undefined : data?.dashboard} />
              <ServiceRow label="Discord bot" service={isPending ? undefined : data?.bot} />
              <ServiceRow label="Internal API" service={isPending ? undefined : data?.internal_api} />
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/**
 * PUBLIC page (no auth: a 502 often means you can't get past login in the
 * first place, so this can't live behind RequireAuth). Lists every error shape
 * the dashboard can actually produce: transport-level (no JSON body, raised
 * before server/app.py's routes run) and app-level (a real {"error": "..."}
 * body from server/permissions/guards.py or server/routes/me.py). Kept in sync
 * manually with those two sources.
 */
export default function StatusPage() {
  const [query, setQuery] = useState("")
  const q = query.trim().toLowerCase()
  const match = (r: ErrorRow) => !q || `${r.code} ${r.name} ${r.meaning} ${r.fix}`.toLowerCase().includes(q)
  const transport = TRANSPORT_ERRORS.filter(match)
  const app = APP_ERRORS.filter(match)

  return (
    <div className="flex min-h-svh flex-col bg-background">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-border bg-card px-4 md:px-8">
        <div className="flex items-center gap-2.5">
          <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-sm font-semibold text-primary-foreground" aria-hidden>
            K
          </div>
          <span className="text-md font-semibold">Service status</span>
        </div>
        <Button asChild variant="ghost" size="sm">
          <Link to="/">Back to home</Link>
        </Button>
      </header>

      <main className="mx-auto flex w-full max-w-[1000px] flex-1 flex-col gap-6 px-4 py-8 md:py-12">
        <div className="flex flex-col gap-2">
          <h1 className="text-2xl font-semibold tracking-tight text-foreground">Status &amp; API error codes</h1>
          <p className="max-w-2xl text-base text-muted-foreground">
            Live status for the bot and dashboard, plus every error the API can return, what it means, and what to do about it. If you hit
            something not listed here, it's a bug: please report it.
          </p>
        </div>

        <LiveStatus />

        <div className="sticky top-0 z-10 -mx-4 border-b border-border bg-background/95 px-4 py-3 backdrop-blur md:mx-0 md:rounded-lg md:border">
          <div className="relative">
            <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-subtle-foreground" />
            <Input
              type="search"
              aria-label="Search error codes"
              placeholder="Search error codes"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="w-full pl-9"
            />
          </div>
        </div>

        <Card className="gap-0 py-0">
          <CardHeader className="border-b py-4">
            <CardTitle>Transport errors</CardTitle>
            <CardDescription>
              Raised before a request ever reaches the dashboard's own route handlers: the server, the tunnel, or your connection couldn't
              complete the round trip.
            </CardDescription>
          </CardHeader>
          <ErrorTable rows={transport} />
        </Card>

        <Card className="gap-0 py-0">
          <CardHeader className="border-b py-4">
            <CardTitle>Application errors</CardTitle>
            <CardDescription>
              Returned by the dashboard API itself as an HTTP status plus a JSON body shaped like{" "}
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">{'{ "error": "..." }'}</code>.
            </CardDescription>
          </CardHeader>
          <ErrorTable rows={app} />
        </Card>
      </main>
      <PublicFooter />
    </div>
  )
}
