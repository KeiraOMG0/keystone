import { Card } from "@/components/ui/card"
import { Alert } from "@/components/ui/alert"
import { ErrorState } from "@/components/patterns/states"
import { ApiError } from "@/api/client"
import { describeApiError } from "@/lib/apiError"
import { useLabOptions } from "@/api/debugLab"
import { PermissionSimulatorCard } from "./PermissionSimulatorCard"
import { PreviewCard } from "./PreviewCard"
import { AntispamTesterCard } from "./AntispamTesterCard"
import { LiveInspectorCard } from "./LiveInspectorCard"
import { LatencyCard } from "./LatencyCard"

/**
 * Debug page "Lab" tab (owner only). Side-effect-free test tools; see
 * services/debug_lab_service.py. The only Discord output is the optional
 * "Send copy" of a preview into the Actions tab's target channel through
 * SafeChannel. The live inspector and latency matrix work even when the lab
 * options can't load (the bot is down, or the dashboard has not been
 * restarted onto the Lab routes yet: then /api/debug/lab/* is a 404).
 */
export function LabTab({ channelId, channelName }: { channelId: string; channelName?: string }) {
  const options = useLabOptions(true)
  const notDeployed = options.error instanceof ApiError && options.error.status === 404
  return (
    <div className="flex flex-col gap-6">
      <Alert variant="info" title="Lab">
        Read-only test tools. Nothing here changes settings, roles or records; "Send copy" posts a [TEST] message to the target
        channel only.
      </Alert>
      {options.isError ? (
        notDeployed ? (
          <Alert variant="warning" title="Lab backend not live yet">
            The dashboard hasn't been restarted onto the Lab routes. Restart the dashboard (and the bot) to enable these tools.
          </Alert>
        ) : (
          <Card>
            <ErrorState
              title="Could not load Lab options"
              message={describeApiError(options.error)}
              onRetry={() => options.refetch()}
              retrying={options.isFetching}
              showStatusLink
            />
          </Card>
        )
      ) : null}
      <PermissionSimulatorCard options={options.data} />
      <div className="grid gap-6 xl:grid-cols-2">
        <PreviewCard modes={["welcome", "boost", "event"]} options={options.data} channelId={channelId} channelName={channelName} />
        <PreviewCard modes={["template"]} options={options.data} channelId={channelId} channelName={channelName} />
      </div>
      <AntispamTesterCard options={options.data} />
      <div className="grid gap-6 xl:grid-cols-2">
        <LiveInspectorCard />
        <LatencyCard />
      </div>
    </div>
  )
}
