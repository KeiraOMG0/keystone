import { apiFetch } from "@/api/client"
import type { ResolveResponse } from "@/types/api"

interface ResolveParams {
  userIds?: string[]
  roleIds?: string[]
  channelIds?: string[]
}

/** Wraps POST /api/resolve (requires any one bot permission server-side). See
 *  src/hooks/useResolve.ts for the shared, deduping react-query hook built
 *  on top of this — page/component code should use that, not this directly. */
export async function resolveIds(params: ResolveParams): Promise<ResolveResponse> {
  return apiFetch<ResolveResponse>("/api/resolve", {
    method: "POST",
    body: JSON.stringify({
      user_ids: params.userIds ?? [],
      role_ids: params.roleIds ?? [],
      channel_ids: params.channelIds ?? [],
    }),
  })
}
