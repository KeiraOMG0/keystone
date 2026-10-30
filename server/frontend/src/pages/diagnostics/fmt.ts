export { formatDuration, formatDateTime, parseUtc } from "@/pages/honeypot/format"

const UNITS = ["B", "KB", "MB", "GB", "TB"]

export function fmtBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "n/a"
  let v = Math.abs(n)
  let i = 0
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024
    i++
  }
  return `${n < 0 ? "-" : ""}${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${UNITS[i]}`
}

export function fmtNum(n: number | null | undefined): string {
  return n === null || n === undefined ? "n/a" : n.toLocaleString()
}

export function fmtPct(n: number | null | undefined, digits = 1): string {
  return n === null || n === undefined ? "n/a" : `${n.toFixed(digits)}%`
}

export function fmtMs(n: number | null | undefined): string {
  return n === null || n === undefined ? "n/a" : `${n < 10 ? n.toFixed(1) : Math.round(n)} ms`
}

/** Full uptime like "3d 4h 12m" (the shared formatDuration rounds to one unit). */
export function fmtUptime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "n/a"
  const s = Math.floor(seconds)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d) return `${d}d ${h}h ${m}m`
  if (h) return `${h}h ${m}m`
  if (m) return `${m}m ${s % 60}s`
  return `${s}s`
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "n/a"
  const d = new Date(iso.includes("T") || iso.endsWith("Z") ? iso : iso.replace(" ", "T") + "Z")
  return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString()
}

export function fmtEpoch(sec: number | null | undefined): string {
  return sec ? new Date(sec * 1000).toLocaleString() : "n/a"
}
