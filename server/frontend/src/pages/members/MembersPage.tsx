import { useEffect, useState, type FormEvent } from "react"
import { Link, useNavigate } from "react-router-dom"
import { Search, UserSearch } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { EmptyState, ErrorState, ListSkeleton } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { ApiError } from "@/api/client"
import { useStaffMemberSearch } from "@/api/members"
import { describeApiError } from "@/lib/apiError"

/**
 * Members (`/dashboard/members`, staff_base): find a member by name or paste
 * any Discord user ID (also people who left or were banned) and open their
 * profile (`/dashboard/members/:userId`).
 */
export default function MembersPage() {
  const [input, setInput] = useState("")
  const [debounced, setDebounced] = useState("")
  const navigate = useNavigate()
  useEffect(() => {
    const t = setTimeout(() => setDebounced(input.trim()), 300)
    return () => clearTimeout(t)
  }, [input])
  const isId = /^\d{15,21}$/.test(input.trim())
  const search = useStaffMemberSearch(isId ? "" : debounced)

  function submit(e: FormEvent) {
    e.preventDefault()
    const q = input.trim()
    if (/^\d{15,21}$/.test(q)) navigate(`/dashboard/members/${q}`)
    else if (search.data?.members[0]) navigate(`/dashboard/members/${search.data.members[0].id}`)
  }

  const err = search.error
  if (err instanceof ApiError && err.status === 403 && (err.body as { error?: string } | null)?.error === "forbidden") {
    return <Forbidden403 reason="forbidden" requiredPermission="staff_base" />
  }

  return (
    <PageContainer>
      <PageHeader title="Members" description="Look anyone up, including people who left or were banned, and open their profile." />
      <Card>
        <CardHeader>
          <CardTitle>Find a member</CardTitle>
          <CardDescription>Type a username or nickname (members only), or paste a Discord user ID (anyone).</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <form onSubmit={submit} className="flex gap-2">
            <div className="relative flex-1">
              <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-subtle-foreground" />
              <Input
                data-page-search
                aria-label="Find a member"
                placeholder="Username, nickname or ID"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                className="w-full pl-9"
              />
            </div>
            <Button type="submit" disabled={!input.trim()}>
              Open
            </Button>
          </form>
          {isId ? (
            <p className="text-sm text-muted-foreground">
              Press Open to view <code className="font-mono">{input.trim()}</code>.
            </p>
          ) : debounced.length < 2 ? (
            <EmptyState icon={UserSearch} title="Search for someone" description="At least two characters, or a user ID." className="py-6" />
          ) : search.isPending ? (
            <ListSkeleton rows={3} />
          ) : search.isError ? (
            <ErrorState title="Could not search" message={describeApiError(search.error)} onRetry={() => void search.refetch()} retrying={search.isRefetching} />
          ) : search.data.members.length === 0 ? (
            <p className="py-4 text-center text-sm text-muted-foreground">No member matches "{debounced}". Paste their ID to look up someone who left.</p>
          ) : (
            <ul className="flex flex-col divide-y divide-border">
              {search.data.members.map((m) => (
                <li key={m.id}>
                  <Link to={`/dashboard/members/${m.id}`} className="flex min-h-12 items-center gap-3 px-2 py-2 hover:bg-hover">
                    <Avatar size="sm">
                      <AvatarImage src={m.avatar_url} alt="" />
                      <AvatarFallback>{m.display_name.slice(0, 1).toUpperCase()}</AvatarFallback>
                    </Avatar>
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate font-medium text-foreground">{m.display_name}</span>
                      <span className="truncate text-xs text-muted-foreground">@{m.username}</span>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </PageContainer>
  )
}
