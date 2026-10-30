import * as React from "react"
import { AlertTriangle } from "lucide-react"

import { Label } from "@/components/ui/label"
import { cn } from "@/lib/utils"

/**
 * Label + control + hint/error wiring. Pass a render function so the control
 * receives the generated ids/aria props:
 *
 *   <Field label="Name" hint="Up to 100 characters" error={nameError}>
 *     {(p) => <Input {...p} value={name} onChange={...} />}
 *   </Field>
 *
 * `error` replaces the hint, is announced (`role="alert"` region linked with
 * aria-describedby) and sets aria-invalid on the control.
 */
function Field({
  label,
  hint,
  error,
  required,
  className,
  children,
  labelAction,
}: {
  label: React.ReactNode
  hint?: React.ReactNode
  error?: React.ReactNode
  required?: boolean
  className?: string
  labelAction?: React.ReactNode
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: true }) => React.ReactNode
}) {
  const id = React.useId()
  const descId = `${id}-desc`
  const hasDesc = Boolean(error || hint)
  return (
    <div data-slot="field" className={cn("flex flex-col gap-1.5", className)}>
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id}>
          {label}
          {required ? (
            <span aria-hidden className="ml-0.5 text-danger">
              *
            </span>
          ) : null}
        </Label>
        {labelAction}
      </div>
      {children({
        id,
        "aria-describedby": hasDesc ? descId : undefined,
        "aria-invalid": error ? true : undefined,
      })}
      {error ? (
        <p id={descId} role="alert" className="flex items-start gap-1 text-xs leading-4 text-danger">
          <AlertTriangle className="mt-px size-3 shrink-0" aria-hidden />
          <span>{error}</span>
        </p>
      ) : hint ? (
        <p id={descId} className="text-xs leading-4 text-muted-foreground">
          {hint}
        </p>
      ) : null}
    </div>
  )
}

/** Character counter for textareas: warns at 90 percent, danger at the limit. */
function CharCounter({ value, max, className }: { value: number; max: number; className?: string }) {
  return (
    <span
      className={cn(
        "text-xs tabular-nums",
        value >= max ? "text-danger" : value >= max * 0.9 ? "text-warning" : "text-subtle-foreground",
        className
      )}
    >
      {value} / {max}
    </span>
  )
}

export { Field, CharCounter }
