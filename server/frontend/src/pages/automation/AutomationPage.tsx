import { useSearchParams } from "react-router-dom"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { LiveCaption } from "@/components/patterns/LivePill"
import { useAuth } from "@/hooks/useAuth"
import { useAutomationLive } from "@/api/automation"

import { EventsTab } from "./EventsTab"
import { GreetingsTab } from "./GreetingsTab"
import { SchedulesTab } from "./SchedulesTab"
import { AutorolesTab } from "./AutorolesTab"
import { ManagedTab } from "./ManagedTab"

const TABS = [
  { value: "events", label: "Events", permission: "manage_events" },
  { value: "greetings", label: "Welcome & Boost", permission: "staff_base" },
  { value: "schedules", label: "Schedules", permission: "manage_schedules" },
  { value: "autoroles", label: "Autoroles", permission: "manage_autorole" },
  { value: "managed", label: "Managed messages", permission: "manage_templates" },
] as const

type TabValue = (typeof TABS)[number]["value"]

/**
 * PROTECTED. /dashboard/automation: web parity for /event, /welcome, /boost,
 * /schedule, /autorole and /managed. Every tab is gated by the SAME permission
 * as its slash command (server/routes/automation.py); a tab the viewer lacks
 * renders an inline 403 (its query is never sent). The nav item shows with
 * any of them (AUTOMATION_ANY_PERMISSION). One `automation` live subscription
 * for the whole page keeps every tab current.
 */
export default function AutomationPage() {
  const { permissions } = useAuth()
  const [params, setParams] = useSearchParams()
  const firstAllowed = TABS.find((t) => permissions[t.permission])?.value ?? "events"
  const requested = params.get("tab") as TabValue | null
  const tab: TabValue = requested && TABS.some((t) => t.value === requested) ? requested : firstAllowed
  useAutomationLive(true)

  return (
    <PageContainer>
      <PageHeader
        title="Automation"
        description="Event messages, welcome and boost posts, scheduled sends, automatic roles and tracked messages. Same settings as the bot's slash commands."
        badge={<LiveCaption fallbackText="Live updates paused. Refreshing every minute." />}
      />
      <Tabs
        value={tab}
        onValueChange={(v) => {
          const next = new URLSearchParams(params)
          next.set("tab", v)
          setParams(next, { replace: true })
        }}
      >
        <TabsList>
          {TABS.map((t) => (
            <TabsTrigger key={t.value} value={t.value}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="events">
          <EventsTab allowed={!!permissions.manage_events} />
        </TabsContent>
        <TabsContent value="greetings">
          <GreetingsTab allowed={!!permissions.staff_base} />
        </TabsContent>
        <TabsContent value="schedules">
          <SchedulesTab allowed={!!permissions.manage_schedules} />
        </TabsContent>
        <TabsContent value="autoroles">
          <AutorolesTab allowed={!!permissions.manage_autorole} />
        </TabsContent>
        <TabsContent value="managed">
          <ManagedTab allowed={!!permissions.manage_templates} />
        </TabsContent>
      </Tabs>
    </PageContainer>
  )
}
