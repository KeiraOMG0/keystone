import { useEffect, useState } from "react"
import { X } from "lucide-react"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Field } from "@/components/ui/field"
import { useMemberSearch } from "@/api/moderation"
import { useAuth } from "@/hooks/useAuth"

export interface PickedMember {
  id: string
  label: string
}

const ID_RE = /^\d{15,21}$/

/**
 * Lab member picker: username/display-name typeahead (GET
 * /api/moderation/members/search, which the owner passes as staff) or a pasted
 * Discord ID. "Me" picks the logged-in owner.
 */
export function MemberPicker({
  value,
  onChange,
  label = "Member",
}: {
  value: PickedMember | null
  onChange: (m: PickedMember | null) => void
  label?: string
}) {
  const { user } = useAuth()
  const [text, setText] = useState("")
  const [debounced, setDebounced] = useState("")
  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(text.trim()), 250)
    return () => window.clearTimeout(t)
  }, [text])
  const isId = ID_RE.test(debounced)
  const search = useMemberSearch(isId ? "" : debounced)
  const results = search.data?.members ?? []

  if (value) {
    return (
      <Field label={label}>
        {() => (
          <div className="flex min-h-9 items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 text-sm">
            <span className="truncate font-medium text-foreground">{value.label}</span>
            <span className="truncate font-mono text-xs text-muted-foreground">{value.id}</span>
            <Button type="button" size="xs" variant="ghost" className="ml-auto" onClick={() => onChange(null)} aria-label="Clear member">
              <X />
            </Button>
          </div>
        )}
      </Field>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      <Field
        label={label}
        hint="Type 2+ letters of a name, or paste an ID."
        labelAction={
          user ? (
            <Button type="button" size="xs" variant="link" onClick={() => onChange({ id: user.id, label: `${user.username} (you)` })}>
              Use me
            </Button>
          ) : undefined
        }
      >
        {(p) => <Input {...p} value={text} autoComplete="off" placeholder="Search members…" onChange={(e) => setText(e.target.value)} />}
      </Field>
      {isId ? (
        <Button type="button" size="sm" variant="secondary" className="self-start" onClick={() => onChange({ id: debounced, label: `ID ${debounced}` })}>
          Use ID {debounced}
        </Button>
      ) : null}
      {!isId && debounced.length >= 2 ? (
        search.isPending ? (
          <p className="text-xs text-muted-foreground">Searching…</p>
        ) : search.isError ? (
          <p className="text-xs text-danger">Search failed. Paste an ID instead.</p>
        ) : results.length === 0 ? (
          <p className="text-xs text-muted-foreground">No members match.</p>
        ) : (
          <ul className="flex max-h-48 flex-col overflow-auto rounded-lg border border-border">
            {results.slice(0, 8).map((m) => (
              <li key={m.id}>
                <button
                  type="button"
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-hover focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
                  onClick={() => {
                    onChange({ id: m.id, label: m.display_name || m.username })
                    setText("")
                  }}
                >
                  <img src={m.avatar_url} alt="" className="size-5 rounded-full" />
                  <span className="truncate text-foreground">{m.display_name}</span>
                  <span className="truncate text-xs text-muted-foreground">@{m.username}</span>
                </button>
              </li>
            ))}
          </ul>
        )
      ) : null}
    </div>
  )
}
