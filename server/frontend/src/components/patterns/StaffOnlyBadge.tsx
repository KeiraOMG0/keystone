import { Lock } from "lucide-react"
import { Badge } from "@/components/ui/badge"

/** "Staff only" marker shown beside the title of staff-gated pages. */
export function StaffOnlyBadge() {
  return (
    <Badge variant="accent">
      <Lock aria-hidden /> Staff only
    </Badge>
  )
}
