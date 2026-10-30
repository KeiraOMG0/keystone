import { useEffect, useId, useState } from "react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Field } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select } from "@/components/ui/select"
import { useToast } from "@/components/ui/toast"
import { ResolvedUser } from "@/components/ResolvedUser"
import { useBirthdayTimezones, useSetBirthday, type BirthdayEntry } from "@/api/birthdays"
import { useBotOffline } from "@/hooks/useBotOffline"
import { useUnsavedChangesGuard } from "@/hooks/useUnsavedChangesGuard"
import { describeApiError } from "@/lib/apiError"
import type { MemberSearchResult } from "@/types/api"
import { MemberChip, MemberPicker } from "./MemberPicker"

export const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
]
/** Feb has 29: a Feb 29 birthday is celebrated on Feb 28 in non-leap years (bot-side rule). */
const MONTH_DAYS = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

/**
 * Add or edit one member's birthday (= /override set-birthday + set-timezone;
 * bypasses add-only). `entry` null = add (pick a member). Dates and timezones
 * are validated bot-side too; the form only prevents the obvious mistakes.
 */
export function BirthdayEntryDialog({
  open,
  onOpenChange,
  entry,
  existingIds,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  entry: BirthdayEntry | null
  existingIds: Set<string>
}) {
  const { toast } = useToast()
  const { offline, reason } = useBotOffline()
  const setBirthday = useSetBirthday()
  const timezones = useBirthdayTimezones()
  const listId = useId()
  const [member, setMember] = useState<MemberSearchResult | null>(null)
  const [month, setMonth] = useState(1)
  const [day, setDay] = useState(1)
  const [tz, setTz] = useState("")
  const [useServerTz, setUseServerTz] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    setMember(null)
    setMonth(entry?.month ?? 1)
    setDay(entry?.day ?? 1)
    setTz(entry?.user_timezone ?? "")
    setUseServerTz(!entry?.user_timezone)
    setError(null)
  }, [open, entry])

  const dirty =
    open &&
    (entry
      ? month !== entry.month || day !== entry.day || (useServerTz ? "" : tz) !== (entry.user_timezone ?? "")
      : member !== null)
  useUnsavedChangesGuard(dirty)

  const maxDay = MONTH_DAYS[month - 1]
  const tzList = timezones.data?.timezones
  const tzTrim = tz.trim()
  const tzUnknown = !useServerTz && tzTrim !== "" && tzList !== undefined && !tzList.some((z) => z.toLowerCase() === tzTrim.toLowerCase())
  const userId = entry?.user_id ?? member?.id ?? null
  const willOverwrite = !entry && member !== null && existingIds.has(member.id)

  function submit() {
    if (!userId) return
    setError(null)
    setBirthday.mutate(
      {
        userId,
        month,
        day: Math.min(day, maxDay),
        timezone: useServerTz || !tzTrim ? null : tzTrim,
        clear_timezone: useServerTz || !tzTrim,
      },
      {
        onSuccess: (res) => {
          toast({ title: res.created ? "Birthday added" : "Birthday updated", variant: "success" })
          onOpenChange(false)
        },
        onError: (e) => setError(describeApiError(e)),
      }
    )
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[520px]">
        <DialogHeader>
          <DialogTitle>{entry ? "Edit birthday" : "Add a birthday"}</DialogTitle>
          <DialogDescription>
            Same as /override set-birthday: works even when add-only mode is on. Changing the date re-arms this
            year's announcement.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          {entry ? (
            <ResolvedUser userId={entry.user_id} />
          ) : member ? (
            <div className="flex flex-col gap-1.5">
              <MemberChip member={member} onRemove={() => setMember(null)} />
              {willOverwrite ? (
                <p className="text-xs text-warning">This member already has a birthday; saving replaces it.</p>
              ) : null}
            </div>
          ) : (
            <MemberPicker onPick={setMember} />
          )}

          <div className="grid grid-cols-2 gap-3">
            <Field label="Month">
              {(p) => (
                <Select {...p} value={month} onChange={(e) => setMonth(Number(e.target.value))}>
                  {MONTHS.map((name, i) => (
                    <option key={name} value={i + 1}>
                      {name}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
            <Field label="Day" hint={month === 2 && day === 29 ? "Celebrated on Feb 28 in non-leap years." : undefined}>
              {(p) => (
                <Select {...p} value={Math.min(day, maxDay)} onChange={(e) => setDay(Number(e.target.value))}>
                  {Array.from({ length: maxDay }, (_, i) => i + 1).map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </Select>
              )}
            </Field>
          </div>

          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <Checkbox
                id={`${listId}-servertz`}
                checked={useServerTz}
                onCheckedChange={(v) => setUseServerTz(v === true)}
              />
              <Label htmlFor={`${listId}-servertz`}>Use the server's timezone</Label>
            </div>
            {!useServerTz ? (
              <Field
                label="Member's timezone"
                hint="IANA name, e.g. Europe/London. The announcement fires at midnight in this timezone."
                error={tzUnknown ? "Not a timezone the bot knows." : undefined}
              >
                {(p) => (
                  <>
                    <Input {...p} list={listId} value={tz} onChange={(e) => setTz(e.target.value)} placeholder="Europe/London" autoComplete="off" />
                    <datalist id={listId}>
                      {(tzList ?? []).map((z) => (
                        <option key={z} value={z} />
                      ))}
                    </datalist>
                  </>
                )}
              </Field>
            ) : null}
          </div>

          {error ? <p role="alert" className="text-sm text-danger">{error}</p> : null}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            onClick={submit}
            loading={setBirthday.isPending}
            disabled={!userId || tzUnknown || offline || setBirthday.isPending}
            title={reason}
          >
            {entry ? "Save" : "Add birthday"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
