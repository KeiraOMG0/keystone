import { Server } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { ResolvedUser } from "@/components/ResolvedUser"
import { useServerInfo } from "@/api/members"
import { describeApiError } from "@/lib/apiError"
import { formatDateTime } from "@/lib/format"

/** The `/serverinfo` facts (GET /api/dashboard/server-info, staff_base). */
export function ServerInfoCard() {
  const q = useServerInfo()
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Server className="size-4 text-muted-foreground" aria-hidden /> Server
        </CardTitle>
      </CardHeader>
      <CardContent>
        {q.isPending ? (
          <div className="flex flex-col gap-2">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-4 w-3/5" />
          </div>
        ) : q.isError ? (
          <p className="text-sm text-muted-foreground">Could not load server facts: {describeApiError(q.error)}</p>
        ) : (
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <dt className="text-muted-foreground">Owner</dt>
            <dd>{q.data.owner_id ? <ResolvedUser userId={q.data.owner_id} /> : "Unknown"}</dd>
            <dt className="text-muted-foreground">Created</dt>
            <dd>{formatDateTime(q.data.created_at)}</dd>
            <dt className="text-muted-foreground">Boosts</dt>
            <dd>
              {q.data.boosts} (tier {q.data.boost_tier})
            </dd>
            <dt className="text-muted-foreground">Channels</dt>
            <dd>
              {q.data.text_channels} text, {q.data.voice_channels} voice, {q.data.categories} categories
            </dd>
            <dt className="text-muted-foreground">Roles / emojis</dt>
            <dd>
              {q.data.roles} / {q.data.emojis}
            </dd>
            <dt className="text-muted-foreground">Verification</dt>
            <dd className="capitalize">{q.data.verification_level.replace("_", " ")}</dd>
          </dl>
        )}
      </CardContent>
    </Card>
  )
}
