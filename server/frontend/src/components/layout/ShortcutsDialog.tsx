import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Kbd } from "@/components/ui/kbd"

const ROWS: { keys: string[]; label: string }[] = [
  { keys: ["Ctrl", "K"], label: "Open the command palette" },
  { keys: ["?"], label: "Show this dialog" },
  { keys: ["/"], label: "Focus the page search" },
  { keys: ["["], label: "Collapse or expand the sidebar" },
  { keys: ["g", "o"], label: "Go to Overview" },
  { keys: ["g", "m"], label: "Go to Moderation" },
  { keys: ["g", "h"], label: "Go to Honeypot" },
  { keys: ["g", "t"], label: "Go to Tickets" },
  { keys: ["Esc"], label: "Close a dialog, menu, drawer or the palette" },
]

export function ShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription>Shortcuts are ignored while you are typing in a field. None of them change data.</DialogDescription>
        </DialogHeader>
        <dl className="flex flex-col divide-y divide-border">
          {ROWS.map((r) => (
            <div key={r.label} className="flex items-center justify-between gap-4 py-2">
              <dt className="text-base text-foreground">{r.label}</dt>
              <dd className="flex shrink-0 items-center gap-1">
                {r.keys.map((k, i) => (
                  <span key={i} className="flex items-center gap-1">
                    {i > 0 ? <span className="text-xs text-subtle-foreground">then</span> : null}
                    <Kbd>{k}</Kbd>
                  </span>
                ))}
              </dd>
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  )
}
