/**
 * Chart palette (colour-blind-safe order, theme-aware through CSS variables).
 * Use `var(--chart-n)` strings for Recharts colour props; never raw hex.
 * Series must also differ by line style or direct labels, never colour alone.
 */
export const CHART = {
  c1: "var(--chart-1)",
  c2: "var(--chart-2)",
  c3: "var(--chart-3)",
  c4: "var(--chart-4)",
  c5: "var(--chart-5)",
  grid: "var(--border)",
  tick: { fill: "var(--subtle-foreground)", fontSize: 11 },
} as const

