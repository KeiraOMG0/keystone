import { useEffect, useState } from "react"
import { Search, X } from "lucide-react"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ListSkeleton } from "@/components/patterns/states"
import { useBirthdayMemberSearch } from "@/api/birthdays"
import { describeApiError } from "@/lib/apiError"
import type { MemberSearchResult } from "@/types/api"

export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

/** Member typeahead over /api/birthdays/members/search (name or display name, 2+ characters). */
export function MemberPicker({
  onPick,
  disabled,
  excludeIds,
  placeholder = "Find a member: type a name (2+ characters)",
  id,
}: {
  onPick: (member: MemberSearchResult) => void
  disabled?: boolean
  excludeIds?: string[]
  placeholder?: string
  id?: string
}) {
  const [q, setQ] = useState("")
  const debounced = useDebounced(q)
  const search = useBirthdayMemberSearch(debounced)
  const results = (search.data?.members ?? []).filter((m) => !excludeIds?.includes(m.id))

  return (
    <div className="flex flex-col gap-2">
      <div className="relative">
        <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
        <Input
          id={id}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={placeholder}
          aria-label="Find a member"
          className="pl-9"
          disabled={disabled}
          autoComplete="off"
        />
      </div>
      {debounced.trim().length >= 2 ? (
        <div className="max-h-48 overflow-y-auto rounded-lg border border-border">
          {search.isPending ? <ListSkeleton rows={2} /> : null}
          {search.isError ? <p className="px-3 py-2 text-sm text-danger">{describeApiError(search.error)}</p> : null}
          {search.data && results.length === 0 ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">No members match.</p>
          ) : null}
          {results.map((m) => (
            <button
              type="button"
              key={m.id}
              className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-hover"
              onClick={() => {
                onPick(m)
                setQ("")
              }}
            >
              <Avatar size="sm">
                <AvatarImage src={m.avatar_url} alt="" />
                <AvatarFallback>{m.display_name.slice(0, 1)}</AvatarFallback>
              </Avatar>
              <span className="min-w-0 truncate text-sm">
                {m.display_name} <span className="text-muted-foreground">@{m.username}</span>
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}

/** A picked member as a removable chip. */
export function MemberChip({ member, onRemove }: { member: MemberSearchResult; onRemove?: () => void }) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-border bg-card py-0.5 pr-1 pl-0.5 text-sm">
      <Avatar size="sm">
        <AvatarImage src={member.avatar_url} alt="" />
        <AvatarFallback>{member.display_name.slice(0, 1)}</AvatarFallback>
      </Avatar>
      {member.display_name}
      {onRemove ? (
        <Button type="button" variant="ghost" size="icon-xs" onClick={onRemove} aria-label={`Remove ${member.display_name}`}>
          <X />
        </Button>
      ) : null}
    </span>
  )
}
