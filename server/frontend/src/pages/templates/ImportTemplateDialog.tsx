import { useState, type FormEvent } from "react"
import { useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Field } from "@/components/ui/field"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { useToast } from "@/components/ui/toast"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"
import { describeApiError } from "@/lib/apiError"
import { useImportTemplate } from "@/api/templates"

/**
 * Parity with /template import: paste (or load) the JSON from Export or
 * /template export. Always creates a NEW template; a name that already exists
 * is refused (use "Import as" to rename).
 */
export function ImportTemplateDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const importTemplate = useImportTemplate()
  const { toast } = useToast()
  const navigate = useNavigate()
  const [text, setText] = useState("")
  const [nameOverride, setNameOverride] = useState("")
  const [parseError, setParseError] = useState<string | null>(null)
  useUnsavedChangesGuard(open && (!!text.trim() || !!nameOverride.trim()))

  function reset() {
    setText("")
    setNameOverride("")
    setParseError(null)
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    let data: unknown
    try {
      data = JSON.parse(text)
    } catch (err) {
      setParseError(`Not valid JSON: ${err instanceof Error ? err.message : "parse error"}`)
      return
    }
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      setParseError("The JSON must be an object (the output of Export).")
      return
    }
    setParseError(null)
    importTemplate.mutate(
      { data: data as Record<string, unknown>, name_override: nameOverride.trim() || null },
      {
        onSuccess: (r) => {
          toast({ title: `Imported "${r.name}"`, variant: "success" })
          reset()
          onOpenChange(false)
          navigate(`/dashboard/templates/${r.template_id}`)
        },
        onError: (err) => toast({ title: "Import failed", description: describeApiError(err), variant: "destructive" }),
      }
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[620px]">
        <DialogHeader>
          <DialogTitle>Import template</DialogTitle>
          <DialogDescription>Paste the JSON from Export or /template export, or load a .json file.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <Input
            type="file"
            accept="application/json,.json"
            aria-label="Load a JSON file"
            onChange={async (e) => {
              const file = e.target.files?.[0]
              if (file) setText(await file.text())
            }}
          />
          <Field label="Template JSON" required error={parseError}>
            {(p) => <Textarea {...p} className="font-mono text-xs" rows={10} value={text} onChange={(e) => setText(e.target.value)} />}
          </Field>
          <Field label="Import as" hint="Optional new name (letters, digits, - and _). Defaults to the name in the JSON.">
            {(p) => <Input {...p} className="font-mono" value={nameOverride} onChange={(e) => setNameOverride(e.target.value)} />}
          </Field>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={!text.trim()} loading={importTemplate.isPending}>
              Import
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
