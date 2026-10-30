import type { DiagnosticsResponse, Sec } from "@/api/diagnostics"
import { fmtBytes, fmtMs, fmtPct } from "@/pages/diagnostics/fmt"

export type Level = "ok" | "warn" | "bad" | "na"

export interface Check {
  id: string
  label: string
  level: Level
  detail: string
  /** Section id to jump to. */
  section?: string
}

const GIB = 1024 ** 3
const isErr = (s: Sec | undefined) => !s || typeof s.error === "string"

/**
 * Derives the green/amber/red overview from the dump. Pure function of the
 * response so it is cheap to re-run every refresh. A check whose source
 * section failed reports "na" (unknown) instead of a false green.
 */
export function computeHealth(d: DiagnosticsResponse): Check[] {
  const out: Check[] = []
  const bot = d.bot
  const dash = d.dashboard

  out.push({
    id: "bot",
    label: "Bot process reachable",
    level: bot.reachable ? "ok" : "bad",
    detail: bot.reachable ? `round trip ${fmtMs(bot.round_trip_ms)}` : "The dashboard cannot reach the bot's internal API",
    section: "dashboard",
  })

  if (!bot.reachable) {
    const dp = dash?.process
    if (dp && !dp.error) {
      out.push(...dashboardChecks(dash))
    }
    return out
  }

  if (bot.psutil_available === false) {
    out.push({ id: "psutil", label: "psutil installed", level: "warn", detail: "Process/system stats unavailable: pip install psutil in the venv", section: "process" })
  }

  const sectionErrors = (["process", "children", "system", "discord", "database", "services", "cookies", "config", "logs", "activity"] as const).filter(
    (k) => typeof (bot[k] as Sec | undefined)?.error === "string" && !(k === "activity"),
  )
  out.push({
    id: "sections",
    label: "All diagnostics sections collected",
    level: sectionErrors.length ? "warn" : "ok",
    detail: sectionErrors.length ? `Failed: ${sectionErrors.join(", ")}` : "No collector errors",
  })

  // Discord permissions
  const perms = bot.discord?.permissions
  if (isErr(bot.discord) || !perms || perms.error) {
    out.push({ id: "perms", label: "Bot Discord permissions", level: "na", detail: perms?.error ?? "unknown", section: "discord" })
  } else {
    const missing: string[] = perms.missing ?? []
    out.push({
      id: "perms",
      label: "Bot Discord permissions",
      level: missing.length ? "bad" : "ok",
      detail: missing.length ? `Missing: ${missing.join(", ")}` : perms.administrator ? "Administrator" : "All required permissions held",
      section: "discord",
    })
  }

  // Gateway
  if (!isErr(bot.discord)) {
    const lat = bot.discord?.latency_ms as number | null
    const ready = bot.discord?.ready
    out.push({
      id: "gateway",
      label: "Discord gateway",
      level: !ready ? "bad" : lat != null && lat > 1000 ? "bad" : lat != null && lat > 300 ? "warn" : "ok",
      detail: `${ready ? "ready" : "NOT ready"}, latency ${fmtMs(lat)}`,
      section: "discord",
    })
  }

  // Pot server
  const pot = bot.services?.pot_server
  if (pot && !pot.error && pot.available !== false) {
    const bad = pot.enabled && pot.state !== "running"
    out.push({
      id: "pot",
      label: "PO-token (pot) server",
      level: !pot.enabled ? "na" : bad ? "bad" : pot.restart_count > 3 ? "warn" : "ok",
      detail: !pot.enabled ? "disabled" : `${pot.state}, ${pot.restart_count ?? 0} restart(s)${pot.last_error ? `, last error: ${String(pot.last_error).slice(0, 80)}` : ""}`,
      section: "services",
    })
  }

  // Cookies
  const ck = bot.cookies
  if (ck && !ck.error) {
    if (!ck.configured || !ck.present) {
      out.push({ id: "cookies", label: "Music cookies", level: "warn", detail: ck.configured ? "cookies file is missing" : "no cookies file configured", section: "cookies" })
    } else {
      const ageDays = (ck.age_hours ?? 0) / 24
      const level: Level = ck.key_auth_expired ? "bad" : ck.key_auth_missing || ck.expired_count > 0 || ageDays > 14 ? "warn" : "ok"
      const notes = [
        ck.key_auth_expired ? "key auth cookies EXPIRED" : "",
        ck.key_auth_missing ? "no key auth cookies present" : "",
        ck.expired_count ? `${ck.expired_count} expired` : "",
        `file ${ageDays.toFixed(1)}d old`,
      ].filter(Boolean)
      out.push({ id: "cookies", label: "Music cookies", level, detail: notes.join(", "), section: "cookies" })
    }
  }

  // Memory
  const rss = bot.process?.rss_bytes as number | undefined
  if (rss != null) {
    out.push({
      id: "mem",
      label: "Bot memory (RSS)",
      level: rss > 2 * GIB ? "bad" : rss > GIB ? "warn" : "ok",
      detail: fmtBytes(rss),
      section: "process",
    })
  }
  const ram = bot.system?.memory
  if (ram && !isErr(bot.system)) {
    out.push({
      id: "ram",
      label: "Host RAM",
      level: ram.percent >= 95 ? "bad" : ram.percent >= 90 ? "warn" : "ok",
      detail: `${fmtPct(ram.percent)} used, ${fmtBytes(ram.available)} free`,
      section: "system",
    })
    const disk = bot.system?.db_disk
    if (disk && !disk.error) {
      const free = 100 - disk.percent
      out.push({
        id: "disk",
        label: "Disk (database drive)",
        level: free < 5 ? "bad" : free < 10 ? "warn" : "ok",
        detail: `${fmtBytes(disk.free_bytes)} free (${free.toFixed(1)}%)`,
        section: "system",
      })
    }
  }

  // Event loop
  const lag = bot.process?.asyncio?.loop_lag_ms as number | undefined
  if (lag != null) {
    out.push({
      id: "lag",
      label: "Event-loop lag",
      level: lag > 500 ? "bad" : lag > 100 ? "warn" : "ok",
      detail: fmtMs(lag),
      section: "process",
    })
  }

  // Database
  const db = bot.database
  if (db && !db.error) {
    const size = db.size_bytes as number | null
    const wal = db.wal_bytes as number | null
    const qc = db.quick_check
    const level: Level = qc && qc !== "ok" ? "bad" : (size ?? 0) > 500 * 1024 ** 2 || (wal ?? 0) > 100 * 1024 ** 2 ? "warn" : "ok"
    out.push({
      id: "db",
      label: "Database",
      level,
      detail: `${fmtBytes(size)} (WAL ${fmtBytes(wal)}), quick_check ${qc ?? "n/a"}, migration ${db.migrations?.latest_version ?? "?"}`,
      section: "database",
    })
  }

  // Loops
  const loops = bot.services?.background_loops
  if (Array.isArray(loops)) {
    const failed = loops.filter((l: Sec) => l.failed)
    const stopped = loops.filter((l: Sec) => !l.running && !l.failed && !l.error)
    out.push({
      id: "loops",
      label: "Background task loops",
      level: failed.length ? "bad" : stopped.length ? "warn" : "ok",
      detail: `${loops.length - stopped.length - failed.length}/${loops.length} running${stopped.length ? `, stopped: ${stopped.map((l: Sec) => l.name).join(", ")}` : ""}${failed.length ? `, FAILED: ${failed.map((l: Sec) => l.name).join(", ")}` : ""}`,
      section: "services",
    })
  }

  // Logs
  const files = bot.logs?.files
  if (Array.isArray(files)) {
    const errs = files.reduce((n: number, f: Sec) => n + (f.level_counts?.ERROR ?? 0) + (f.level_counts?.CRITICAL ?? 0), 0)
    out.push({
      id: "logs",
      label: "Recent log errors",
      level: errs > 0 ? "warn" : "ok",
      detail: `${errs} ERROR/CRITICAL line(s) in scanned log tails`,
      section: "logs",
    })
  }

  out.push(...dashboardChecks(dash))
  return out
}

function dashboardChecks(dash: Sec | undefined): Check[] {
  const out: Check[] = []
  if (!dash) return out
  const rtt = dash.internal_api?.round_trip_ms as number | undefined
  if (rtt != null && dash.internal_api?.reachable) {
    out.push({ id: "rtt", label: "Dashboard to bot round trip", level: rtt > 3000 ? "bad" : rtt > 1000 ? "warn" : "ok", detail: fmtMs(rtt), section: "dashboard" })
  }
  const tunnel = dash.stack
  if (tunnel && !tunnel.error) {
    out.push({
      id: "tunnel",
      label: "Cloudflare tunnel process",
      level: tunnel.tunnel_running ? "ok" : "warn",
      detail: tunnel.tunnel_running ? `${tunnel.tunnel_processes.length} cloudflared process(es)` : "no cloudflared process found on this host",
      section: "dashboard",
    })
  }
  const rss = dash.process?.rss_bytes as number | undefined
  if (rss != null) {
    out.push({ id: "dashmem", label: "Dashboard memory (RSS)", level: rss > GIB ? "warn" : "ok", detail: fmtBytes(rss), section: "dashboard" })
  }
  const errs = dash.requests?.status_classes?.["5xx"] as number | undefined
  if (errs) out.push({ id: "dash5xx", label: "Dashboard 5xx responses", level: "warn", detail: `${errs} since dashboard start`, section: "dashboard" })
  return out
}

export function overall(checks: Check[]): Level {
  if (checks.some((c) => c.level === "bad")) return "bad"
  if (checks.some((c) => c.level === "warn")) return "warn"
  return "ok"
}
