import { useMemo, useState } from "react"
import { Braces, Search } from "lucide-react"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ErrorState } from "@/components/patterns/states"
import { describeApiError } from "@/lib/apiError"
import { usePlaceholders, type PlaceholderEntry } from "@/api/placeholders"

/**
 * Template editor: every placeholder the bot supports (core/placeholder_engine.py),
 * searchable and grouped by category, with the bot's example value for the
 * signed-in user. Clicking one inserts it at the cursor of the last text field
 * you were typing in (`onInsert`).
 */
export function PlaceholdersPanel({ onInsert }: { onInsert: (token: string) => void }) {
  const query = usePlaceholders()
  const [search, setSearch] = useState("")
  const [category, setCategory] = useState<string>("all")

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return (query.data?.placeholders ?? []).filter(
      (p) =>
        (category === "all" || p.category === category) &&
        (!q || p.syntax.toLowerCase().includes(q) || p.description.toLowerCase().includes(q))
    )
  }, [query.data, search, category])

  const groups = useMemo(() => {
    const out = new Map<string, PlaceholderEntry[]>()
    for (const p of filtered) out.set(p.category, [...(out.get(p.category) ?? []), p])
    return [...out.entries()]
  }, [filtered])

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Braces className="size-4" aria-hidden /> Placeholders
        </CardTitle>
        <CardDescription>
          Click one to insert it where your cursor was. <code className="font-mono">{"{name|backup}"}</code> uses the
          backup when the value is empty, e.g. <code className="font-mono">{"{reason|No reason given}"}</code>. Examples
          show you as the member.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap gap-2">
          <div className="relative min-w-[180px] flex-1">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input
              aria-label="Search placeholders"
              placeholder="Search, e.g. reason, join, role"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-8"
            />
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Category">
          {["all", ...(query.data?.categories ?? [])].map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setCategory(c)}
              aria-pressed={category === c}
              className={
                "rounded-full border px-2.5 py-0.5 text-xs transition-colors " +
                (category === c
                  ? "border-primary bg-primary-soft text-primary-soft-foreground"
                  : "border-border text-muted-foreground hover:bg-hover")
              }
            >
              {c === "all" ? "All" : c}
            </button>
          ))}
        </div>

        {query.isPending ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </div>
        ) : query.isError ? (
          <ErrorState title="Couldn't load placeholders" message={describeApiError(query.error)} onRetry={() => query.refetch()} />
        ) : groups.length === 0 ? (
          <p className="text-sm text-muted-foreground">No placeholder matches &quot;{search}&quot;.</p>
        ) : (
          <div className="flex max-h-[520px] flex-col gap-4 overflow-auto pr-1">
            {groups.map(([cat, items]) => (
              <section key={cat} className="flex flex-col gap-1.5">
                <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{cat}</h3>
                {items.map((p) => (
                  <button
                    key={p.syntax}
                    type="button"
                    onMouseDown={(e) => e.preventDefault() /* keep the cursor in the field being edited */}
                    onClick={() => onInsert(p.syntax)}
                    className="flex flex-col gap-0.5 rounded-md border border-border px-2.5 py-1.5 text-left transition-colors hover:bg-hover"
                    title={`Works in: ${p.where}`}
                  >
                    <span className="flex flex-wrap items-baseline gap-x-2">
                      <code className="font-mono text-xs font-medium text-foreground">{p.syntax}</code>
                      {p.example && p.example !== "\n" ? (
                        <span className="truncate text-xs text-muted-foreground">→ {p.example}</span>
                      ) : null}
                    </span>
                    <span className="text-xs text-muted-foreground">{p.description}</span>
                  </button>
                ))}
              </section>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
