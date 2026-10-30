import { useQuery } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"

/*
 * Template placeholders (GET /api/templates/placeholders, manage_templates;
 * server/routes/templates.py -> internal GET /guilds/{id}/placeholders ->
 * core/placeholder_engine.catalogue()). `example` is rendered by the bot with
 * the signed-in user as the member and example moderation data.
 */

export interface PlaceholderEntry {
  name: string
  /** What to type, e.g. "{mention}" or "{channel:general}". */
  syntax: string
  category: string
  description: string
  /** Which messages it has a value in. */
  where: string
  example: string
}

export interface PlaceholderCatalogue {
  categories: string[]
  placeholders: PlaceholderEntry[]
  /** False when the viewer isn't a member (examples about a member are blank). */
  sample_member: boolean
}

export function usePlaceholders() {
  return useQuery({
    queryKey: ["templates", "placeholders"],
    queryFn: ({ signal }) => apiFetch<PlaceholderCatalogue>("/api/templates/placeholders", { signal }),
    staleTime: 5 * 60_000,
  })
}

const PATTERN = /\{(\w+)(?::([^{}|]*))?(?:\|([^{}]*))?\}/g

/**
 * Preview-only approximation of the bot's resolve_text: plain placeholders use
 * the bot's example values; `{name:arg}` uses the example when the catalogue
 * shows that exact form, otherwise a readable stand-in (#channel, @role, ...).
 * Unknown placeholders stay as written, like on the bot.
 */
export function resolvePlaceholders(text: string | null | undefined, catalogue: PlaceholderEntry[] | undefined): string {
  if (!text) return ""
  if (!catalogue) return text
  const bySyntax = new Map(catalogue.map((p) => [p.syntax, p.example]))
  const names = new Set(catalogue.map((p) => p.name))
  const firstArgExample = (name: string) =>
    [...bySyntax.entries()].find(([k]) => k.startsWith(`{${name}:`))?.[1] ?? ""
  const valueOf = (name: string, arg: string | undefined): string | null => {
    if (!names.has(name)) return null
    if (arg === undefined) return bySyntax.get(`{${name}}`) ?? firstArgExample(name)
    const exact = bySyntax.get(`{${name}:${arg}}`)
    if (exact !== undefined) return exact
    const a = arg.trim()
    switch (name) {
      case "channel":
        return `#${a}`
      case "role":
      case "member":
        return `@${a}`
      case "emoji":
        return `:${a}:`
      case "random":
        return a.split(";")[0]?.trim() ?? ""
      default:
        return bySyntax.get(`{${name}}`) ?? firstArgExample(name)
    }
  }
  return text.replace(PATTERN, (whole, name: string, arg: string | undefined, fallback: string | undefined) => {
    const value = valueOf(name, arg)
    if (value === null) return whole
    if (!value && fallback !== undefined) {
      const fb = fallback.trim()
      const alt = /^\w+$/.test(fb) ? valueOf(fb, undefined) : null
      return alt || fallback
    }
    return value
  })
}
