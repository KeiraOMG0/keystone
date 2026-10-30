import { useEffect, useState, type ReactNode } from "react"
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

/**
 * Confirm-destructive pattern: title names the object, the body states the
 * consequence, initial focus is on Cancel, and the action button says what it
 * does ("Delete ticket"). `typeToConfirm` (e.g. the ticket number or "GRANT")
 * keeps the action disabled until the user types it, for the highest-impact
 * actions. Controlled: the caller owns `open`.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  confirmIcon,
  onConfirm,
  destructive = true,
  typeToConfirm,
  loading,
  children,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: ReactNode
  description: ReactNode
  confirmLabel: string
  confirmIcon?: ReactNode
  onConfirm: () => void
  destructive?: boolean
  typeToConfirm?: string
  loading?: boolean
  children?: ReactNode
}) {
  const [typed, setTyped] = useState("")
  useEffect(() => {
    if (!open) setTyped("")
  }, [open])
  const ok = !typeToConfirm || typed.trim().toLowerCase() === typeToConfirm.toLowerCase()

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        {children}
        {typeToConfirm ? (
          <div className="flex flex-col gap-1.5 py-1">
            <Label htmlFor="confirm-typed">
              Type <span className="font-mono font-semibold">{typeToConfirm}</span> to confirm
            </Label>
            <Input id="confirm-typed" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          </div>
        ) : null}
        <AlertDialogFooter>
          <AlertDialogCancel autoFocus>Cancel</AlertDialogCancel>
          <Button
            variant={destructive ? "destructive" : "default"}
            disabled={!ok}
            loading={loading}
            onClick={() => {
              onConfirm()
              onOpenChange(false)
            }}
          >
            {confirmIcon}
            {confirmLabel}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
