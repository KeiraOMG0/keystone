import { useEffect, useMemo, useRef, useState } from "react"
import { Lock, ShieldAlert, Upload, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select } from "@/components/ui/select"
import { Field } from "@/components/ui/field"
import { Switch } from "@/components/ui/switch"
import { Checkbox } from "@/components/ui/checkbox"
import { Badge } from "@/components/ui/badge"
import { Alert } from "@/components/ui/alert"
import { Tip } from "@/components/ui/tooltip"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { useToast } from "@/components/ui/toast"
import { roleErrorMessage, useCreateRole, useMoveRole, useUpdateRole, type RoleFieldsInput } from "@/api/roleAdmin"
import type { AdminRole, RoleAdminOverview } from "@/types/api"
import { cn } from "@/lib/utils"
import {
  HEX_RE,
  INTERNAL_PERMISSION_HELP,
  PRESET_COLORS,
  ceilingRole,
  grantablePermissions,
  positionOptions,
  readFileAsBase64,
  sameSet,
} from "./roleUtils"

const ICON_MAX_BYTES = 256 * 1024

/** The role as it was when the dialog opened; edits are diffed against this. */
type RoleSnapshot = Pick<
  AdminRole,
  "name" | "color" | "hoist" | "mentionable" | "permissions" | "internal_permissions" | "unicode_emoji" | "position"
>

function snapshotOf(role: AdminRole): RoleSnapshot {
  return {
    name: role.name,
    color: role.color,
    hoist: role.hoist,
    mentionable: role.mentionable,
    permissions: [...role.permissions],
    internal_permissions: [...role.internal_permissions],
    unicode_emoji: role.unicode_emoji,
    position: role.position,
  }
}

function snapshotChanged(a: RoleSnapshot, b: RoleSnapshot): boolean {
  return (
    a.name !== b.name ||
    (a.color ?? null) !== (b.color ?? null) ||
    a.hoist !== b.hoist ||
    a.mentionable !== b.mentionable ||
    (a.unicode_emoji ?? null) !== (b.unicode_emoji ?? null) ||
    a.position !== b.position ||
    !sameSet(a.permissions, b.permissions) ||
    !sameSet(a.internal_permissions, b.internal_permissions)
  )
}

/** Apply the user's own toggles (form vs snapshot) on top of the live set, so a
 * concurrent change to OTHER flags made elsewhere is kept, not reverted. */
function mergeDelta(live: string[], initial: string[], form: string[]): string[] {
  const init = new Set(initial)
  const now = new Set(form)
  const out = new Set(live)
  for (const p of now) if (!init.has(p)) out.add(p)
  for (const p of init) if (!now.has(p)) out.delete(p)
  return [...out].sort()
}

/**
 * Create / edit a role. Discord fields are editable only when the role is
 * manageable by the bot AND by the viewer (create always can, given Manage
 * Roles); internal bot permissions are editable on any role except @everyone
 * and managed roles. Permission checkboxes are limited to what both the bot
 * and the viewer hold (Discord refuses the rest); Administrator is offered
 * only to the owner when the bot has it and needs a typed confirmation.
 * Saving sends only the fields changed since the dialog opened (diffed
 * against a snapshot, not the live-refreshed role), so a live update while
 * the dialog is open is never silently reverted. The server re-checks every rule.
 */
export function RoleEditorDialog({
  open,
  onOpenChange,
  mode,
  role,
  data,
  disabledReason,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  mode: "create" | "edit"
  role: AdminRole | null
  data: RoleAdminOverview
  /** Set while the bot is offline: Save is disabled with this tooltip. */
  disabledReason?: string
}) {
  const { toast } = useToast()
  const createRole = useCreateRole()
  const updateRole = useUpdateRole()
  const moveRole = useMoveRole()

  const [name, setName] = useState("")
  const [color, setColor] = useState("")
  const [hoist, setHoist] = useState(false)
  const [mentionable, setMentionable] = useState(false)
  const [perms, setPerms] = useState<string[]>([])
  const [internal, setInternal] = useState<string[]>([])
  const [emoji, setEmoji] = useState("")
  const [iconData, setIconData] = useState<string | null>(null)
  const [iconName, setIconName] = useState<string | null>(null)
  const [clearIcon, setClearIcon] = useState(false)
  const [placement, setPlacement] = useState("bottom")
  const [position, setPosition] = useState(1)
  const [adminConfirmOpen, setAdminConfirmOpen] = useState(false)
  const [adminConfirmed, setAdminConfirmed] = useState(false)
  const [permFilter, setPermFilter] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [touched, setTouched] = useState(false)
  const [initial, setInitial] = useState<RoleSnapshot | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  // Reset the form each time the dialog opens (or switches role). Later live
  // refreshes of `role` do NOT reset it (that would discard typing); the
  // snapshot keeps what the form started from.
  useEffect(() => {
    if (!open) return
    loadFrom(role)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, role?.id])

  function loadFrom(role: AdminRole | null) {
    setInitial(role ? snapshotOf(role) : null)
    setName(role?.name ?? "")
    setColor(role?.color ?? "")
    setHoist(role?.hoist ?? false)
    setMentionable(role?.mentionable ?? false)
    setPerms(role?.permissions ?? [])
    setInternal(role?.internal_permissions ?? [])
    setEmoji(role?.unicode_emoji ?? "")
    setIconData(null)
    setIconName(null)
    setClearIcon(false)
    setPlacement("bottom")
    setPosition(role?.position ?? 1)
    setAdminConfirmed(false)
    setPermFilter("")
    setError(null)
    setTouched(false)
  }

  const botHeld = useMemo(() => new Set(data.bot.permissions), [data.bot.permissions])
  const held = useMemo(() => grantablePermissions(data), [data])
  const ceiling = ceilingRole(data)
  const isOwner = data.actor ? data.actor.is_owner : true
  const staleInLive = mode === "edit" && role !== null && initial !== null && snapshotChanged(initial, snapshotOf(role))
  const discordEditable = mode === "create" ? data.bot.manage_roles : Boolean(role?.manageable)
  const internalEditable = mode === "create" || (role !== null && !role.managed && !role.is_default)
  const iconsSupported = data.guild.role_icons
  const pending = createRole.isPending || updateRole.isPending || moveRole.isPending

  const nameError = touched && !name.trim() ? "Give the role a name." : name.length > 100 ? "At most 100 characters." : undefined
  const colorError = color && !HEX_RE.test(color) ? "Use a hex colour like #5865F2." : undefined

  const placementOptions = useMemo(() => {
    const top = ceilingRole(data).position
    return data.roles.filter((r) => !r.is_default && r.position < top)
  }, [data])
  const movePositions = useMemo(() => positionOptions(data, role?.id), [data, role?.id])

  function togglePerm(key: string, on: boolean) {
    if (key === "administrator" && on && !initial?.permissions.includes("administrator")) {
      setAdminConfirmOpen(true)
      return
    }
    setPerms((p) => (on ? [...new Set([...p, key])] : p.filter((x) => x !== key)))
  }

  function toggleInternal(key: string, on: boolean) {
    setInternal((p) => (on ? [...new Set([...p, key])] : p.filter((x) => x !== key)))
  }

  async function onPickIcon(file: File | undefined) {
    if (!file) return
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      setError("Role icons must be PNG, JPEG or WebP images.")
      return
    }
    if (file.size > ICON_MAX_BYTES) {
      setError("Role icons must be 256 KB or smaller.")
      return
    }
    try {
      setIconData(await readFileAsBase64(file))
      setIconName(file.name)
      setEmoji("")
      setClearIcon(false)
      setError(null)
    } catch {
      setError("Could not read that file.")
    }
  }

  async function submit() {
    setTouched(true)
    setError(null)
    if (!name.trim() || nameError || colorError) return
    const adding = perms.includes("administrator") && !(initial?.permissions.includes("administrator") ?? false)
    try {
      if (mode === "create") {
        const body: RoleFieldsInput & { name: string; position?: number | null; above_role_id?: string | null } = {
          name: name.trim(),
          color: color || null,
          hoist,
          mentionable,
          permissions: perms,
          internal_permissions: internal,
          confirm_administrator: adding && adminConfirmed,
        }
        if (iconsSupported && iconData) body.icon_base64 = iconData
        else if (iconsSupported && emoji.trim()) body.unicode_emoji = emoji.trim()
        if (placement === "top") body.position = 250
        else if (placement !== "bottom") body.above_role_id = placement
        const res = await createRole.mutateAsync(body)
        toast({
          title: `Created ${res.role.name}`,
          description: res.warnings?.length ? res.warnings.join(" ") : undefined,
          variant: res.warnings?.length ? "info" : "success",
        })
        onOpenChange(false)
        return
      }
      if (!role || !initial) return
      // Diff the form against the snapshot taken when the dialog opened, NOT the
      // live `role` prop (which a `roles` event or poll may have refreshed):
      // only fields the user actually changed are sent, so a concurrent edit
      // made elsewhere is never reverted by saving an unrelated field.
      const body: RoleFieldsInput = {}
      if (discordEditable) {
        if (name.trim() !== initial.name) body.name = name.trim()
        if ((color || null) !== (initial.color ?? null)) body.color = color || ""
        if (hoist !== initial.hoist) body.hoist = hoist
        if (mentionable !== initial.mentionable) body.mentionable = mentionable
        if (!sameSet(perms, initial.permissions)) {
          const merged = mergeDelta(role.permissions, initial.permissions, perms)
          if (!sameSet(merged, role.permissions)) body.permissions = merged
        }
        if (iconsSupported) {
          if (iconData) body.icon_base64 = iconData
          else if (clearIcon) body.clear_icon = true
          else if (emoji.trim() && emoji.trim() !== (initial.unicode_emoji ?? "")) body.unicode_emoji = emoji.trim()
        }
        if (adding) body.confirm_administrator = adminConfirmed
      }
      if (internalEditable && !sameSet(internal, initial.internal_permissions)) {
        const merged = mergeDelta(role.internal_permissions, initial.internal_permissions, internal)
        if (!sameSet(merged, role.internal_permissions)) body.internal_permissions = merged
      }
      const moving = discordEditable && position !== initial.position && position !== role.position
      if (Object.keys(body).length === 0 && !moving) {
        onOpenChange(false)
        return
      }
      if (Object.keys(body).length > 0) await updateRole.mutateAsync({ roleId: role.id, body })
      if (moving) await moveRole.mutateAsync({ roleId: role.id, position })
      toast({ title: `Saved ${name.trim() || role.name}`, variant: "success" })
      onOpenChange(false)
    } catch (e) {
      setError(roleErrorMessage(e))
    }
  }

  const filter = permFilter.trim().toLowerCase()
  const previewColor = color && HEX_RE.test(color) ? color : undefined

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-[720px]">
          <DialogHeader>
            <DialogTitle>{mode === "create" ? "Create role" : `Edit ${role?.name ?? "role"}`}</DialogTitle>
            <DialogDescription>
              {mode === "create"
                ? "New roles are created below the bot's highest role. Changes are applied in Discord immediately."
                : "Changes are applied in Discord immediately and logged with your name."}
            </DialogDescription>
          </DialogHeader>

          {!discordEditable && role ? (
            <Alert variant="warning" title="Discord settings are read-only for this role">
              {role.unmanageable_reason}
              {internalEditable ? " You can still change its bot permissions below." : null}
            </Alert>
          ) : null}
          {staleInLive ? (
            <Alert variant="info" title="This role changed while you were editing">
              <span className="flex flex-wrap items-center gap-2">
                Saving sends only the fields you changed here; other changes are kept.
                <Button type="button" variant="secondary" size="sm" onClick={() => loadFrom(role)}>
                  Reload latest
                </Button>
              </span>
            </Alert>
          ) : null}
          {error ? <Alert variant="danger">{error}</Alert> : null}

          <form
            className="flex flex-col gap-5"
            onSubmit={(e) => {
              e.preventDefault()
              void submit()
            }}
          >
            {/* Preview */}
            <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2">
              <span className="text-xs text-muted-foreground">Preview</span>
              <span className="text-base font-semibold" style={previewColor ? { color: previewColor } : undefined}>
                {emoji ? `${emoji} ` : null}@{name.trim() || "new role"}
              </span>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Name" required error={nameError}>
                {(p) => (
                  <Input
                    {...p}
                    value={name}
                    maxLength={100}
                    disabled={!discordEditable}
                    onChange={(e) => setName(e.target.value)}
                    onBlur={() => setTouched(true)}
                    autoFocus={mode === "create"}
                  />
                )}
              </Field>
              <Field label="Colour" hint="Leave empty for no colour" error={colorError}>
                {(p) => (
                  <div className="flex items-center gap-2">
                    <input
                      type="color"
                      aria-label="Pick a colour"
                      className="h-9 w-11 shrink-0 cursor-pointer rounded-lg border border-input bg-card p-1 disabled:cursor-not-allowed"
                      value={previewColor ?? "#99aab5"}
                      disabled={!discordEditable}
                      onChange={(e) => setColor(e.target.value)}
                    />
                    <Input
                      {...p}
                      value={color}
                      placeholder="#5865F2"
                      maxLength={7}
                      disabled={!discordEditable}
                      onChange={(e) => {
                        const v = e.target.value.trim()
                        setColor(v && !v.startsWith("#") ? `#${v}` : v)
                      }}
                      className="font-mono"
                    />
                    {color ? (
                      <Tip label="No colour">
                        <Button type="button" variant="ghost" size="icon-sm" disabled={!discordEditable} onClick={() => setColor("")} aria-label="Clear colour">
                          <X />
                        </Button>
                      </Tip>
                    ) : null}
                  </div>
                )}
              </Field>
            </div>
            {discordEditable ? (
              <div className="-mt-2 flex flex-wrap gap-1.5" role="group" aria-label="Preset colours">
                {PRESET_COLORS.map((c) => (
                  <button
                    key={c}
                    type="button"
                    aria-label={`Use ${c}`}
                    onClick={() => setColor(c)}
                    className={cn(
                      "size-6 rounded-md border border-border outline-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
                      color.toLowerCase() === c && "ring-2 ring-ring ring-offset-2 ring-offset-card"
                    )}
                    style={{ backgroundColor: c }}
                  />
                ))}
              </div>
            ) : null}

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="flex items-start justify-between gap-3 rounded-lg border border-border px-3 py-2.5">
                <span>
                  <span className="block text-base font-medium text-foreground">Show separately</span>
                  <span className="block text-xs text-muted-foreground">Display members with this role in their own group.</span>
                </span>
                <Switch checked={hoist} onCheckedChange={setHoist} disabled={!discordEditable} aria-label="Show separately" />
              </label>
              <label className="flex items-start justify-between gap-3 rounded-lg border border-border px-3 py-2.5">
                <span>
                  <span className="block text-base font-medium text-foreground">Mentionable</span>
                  <span className="block text-xs text-muted-foreground">Allow anyone to @mention this role.</span>
                </span>
                <Switch checked={mentionable} onCheckedChange={setMentionable} disabled={!discordEditable} aria-label="Mentionable" />
              </label>
            </div>

            {/* Icon */}
            {iconsSupported ? (
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Emoji icon" hint="A standard emoji shown next to names">
                  {(p) => (
                    <Input
                      {...p}
                      value={emoji}
                      maxLength={16}
                      disabled={!discordEditable || Boolean(iconData)}
                      placeholder="e.g. a single emoji"
                      onChange={(e) => {
                        setEmoji(e.target.value)
                        setClearIcon(false)
                      }}
                    />
                  )}
                </Field>
                <div className="flex flex-col gap-1.5">
                  <span className="text-sm font-medium text-foreground">Image icon</span>
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      ref={fileRef}
                      type="file"
                      accept="image/png,image/jpeg,image/webp"
                      className="sr-only"
                      onChange={(e) => void onPickIcon(e.target.files?.[0])}
                    />
                    <Button type="button" variant="secondary" size="sm" disabled={!discordEditable} onClick={() => fileRef.current?.click()}>
                      <Upload /> {iconName ? "Replace image" : "Upload image"}
                    </Button>
                    {iconName ? <span className="truncate text-xs text-muted-foreground">{iconName}</span> : null}
                    {mode === "edit" && (role?.icon_url || role?.unicode_emoji) && !iconData ? (
                      <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                        <Checkbox
                          checked={clearIcon}
                          disabled={!discordEditable}
                          onCheckedChange={(v) => {
                            setClearIcon(v === true)
                            if (v === true) setEmoji("")
                          }}
                        />
                        Remove current icon
                      </label>
                    ) : null}
                  </div>
                  <span className="text-xs text-muted-foreground">PNG, JPEG or WebP, up to 256 KB.</span>
                </div>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">Role icons are unavailable: this server needs boost level 2 for them.</p>
            )}

            {/* Position */}
            {mode === "create" ? (
              <Field
                label="Position"
                hint={
                  ceiling.isActor
                    ? `New roles must sit below your highest role (${ceiling.name}).`
                    : `New roles must sit below the bot's highest role (${ceiling.name}).`
                }
              >
                {(p) => (
                  <Select {...p} value={placement} onChange={(e) => setPlacement(e.target.value)} disabled={!discordEditable}>
                    <option value="bottom">Bottom (just above @everyone)</option>
                    <option value="top">Directly below {ceiling.name}</option>
                    {placementOptions.map((r) => (
                      <option key={r.id} value={r.id}>
                        Directly above {r.name}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            ) : discordEditable ? (
              <Field label="Position" hint={`Only positions below ${ceiling.name} are available.`}>
                {(p) => (
                  <Select {...p} value={String(position)} onChange={(e) => setPosition(Number(e.target.value))}>
                    {role && !movePositions.some((o) => o.value === role.position) ? (
                      <option value={role.position}>Position {role.position} (current)</option>
                    ) : null}
                    {movePositions.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.value === role?.position ? `Position ${o.value} (current)` : o.label}
                      </option>
                    ))}
                  </Select>
                )}
              </Field>
            ) : null}

            {/* Discord permissions */}
            <section className="flex flex-col gap-3">
              <div className="flex flex-wrap items-end justify-between gap-2">
                <div>
                  <h3 className="text-md font-semibold text-foreground">Discord permissions</h3>
                  <p className="text-xs text-muted-foreground">
                    {isOwner
                      ? "Only permissions the bot has can be granted or removed; the rest are locked."
                      : "Only permissions both you and the bot have can be granted or removed (Administrator is owner-only); the rest are locked."}
                  </p>
                </div>
                <Input
                  value={permFilter}
                  onChange={(e) => setPermFilter(e.target.value)}
                  placeholder="Filter permissions"
                  aria-label="Filter permissions"
                  className="h-8 w-48"
                />
              </div>
              {data.permission_catalog.map((group) => {
                const items = group.permissions.filter((p) => {
                  if (p.key === "administrator" && !held.has("administrator") && !perms.includes("administrator")) return false
                  return !filter || p.label.toLowerCase().includes(filter) || p.key.includes(filter)
                })
                if (items.length === 0) return null
                const count = group.permissions.filter((p) => perms.includes(p.key)).length
                return (
                  <fieldset key={group.group} className="rounded-lg border border-border px-3 py-2.5">
                    <legend className="px-1 text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                      {group.group} <span className="font-normal tabular-nums">({count}/{group.permissions.length})</span>
                    </legend>
                    <div className="grid gap-x-4 gap-y-1.5 sm:grid-cols-2">
                      {items.map((p) => {
                        const lockedByBot = !botHeld.has(p.key)
                        const locked = !held.has(p.key)
                        const lockReason = lockedByBot
                          ? "The bot does not have this permission, so it cannot grant or remove it."
                          : p.key === "administrator"
                            ? "Only the server owner can grant or remove Administrator."
                            : "You do not have this permission yourself, so you cannot grant or remove it."
                        const disabled = !discordEditable || locked
                        const row = (
                          <label
                            className={cn(
                              "flex min-h-8 items-center gap-2 rounded-md px-1 text-sm",
                              disabled ? "cursor-not-allowed text-muted-foreground" : "cursor-pointer text-foreground hover:bg-hover"
                            )}
                          >
                            <Checkbox
                              checked={perms.includes(p.key)}
                              disabled={disabled}
                              onCheckedChange={(v) => togglePerm(p.key, v === true)}
                            />
                            <span className="min-w-0 flex-1 truncate">{p.label}</span>
                            {p.key === "administrator" ? (
                              <ShieldAlert className="size-3.5 text-danger" aria-label="Grants every permission" />
                            ) : p.dangerous ? (
                              <Badge variant="warning" className="h-[18px] px-1.5">sensitive</Badge>
                            ) : null}
                            {locked ? <Lock className="size-3 text-subtle-foreground" aria-hidden /> : null}
                          </label>
                        )
                        return locked && discordEditable ? (
                          <Tip key={p.key} label={lockReason}>
                            {row}
                          </Tip>
                        ) : (
                          <div key={p.key}>{row}</div>
                        )
                      })}
                    </div>
                  </fieldset>
                )
              })}
            </section>

            {/* Internal bot permissions */}
            <section className="flex flex-col gap-2">
              <div>
                <h3 className="text-md font-semibold text-foreground">Bot permissions</h3>
                <p className="text-xs text-muted-foreground">
                  Keystone's own permission checks (same grants as the Permissions page).
                </p>
              </div>
              {role?.is_default_staff ? (
                <Alert variant="info">This is a default staff role: it already passes every bot permission check.</Alert>
              ) : null}
              {!internalEditable ? (
                <p className="text-xs text-muted-foreground">
                  {role?.is_default ? "@everyone" : "Managed roles"} cannot hold bot permissions.
                </p>
              ) : (
                <div className="grid gap-x-4 gap-y-1.5 rounded-lg border border-border px-3 py-2.5 sm:grid-cols-2">
                  {data.valid_internal_permissions.map((perm) => (
                    <label key={perm} className="flex min-h-8 cursor-pointer items-start gap-2 rounded-md px-1 py-1 text-sm hover:bg-hover">
                      <Checkbox className="mt-0.5" checked={internal.includes(perm)} onCheckedChange={(v) => toggleInternal(perm, v === true)} />
                      <span className="min-w-0">
                        <span className="block font-mono text-xs text-foreground">{perm}</span>
                        {INTERNAL_PERMISSION_HELP[perm] ? (
                          <span className="block text-xs text-muted-foreground">{INTERNAL_PERMISSION_HELP[perm]}</span>
                        ) : null}
                      </span>
                    </label>
                  ))}
                </div>
              )}
            </section>

            <DialogFooter>
              <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" loading={pending} disabled={Boolean(disabledReason) || Boolean(colorError)} title={disabledReason}>
                {mode === "create" ? "Create role" : "Save changes"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={adminConfirmOpen}
        onOpenChange={setAdminConfirmOpen}
        title="Grant Administrator?"
        description="Administrator bypasses every channel permission and lets members with this role do anything in the server, including deleting channels and banning people. Only give it to fully trusted staff."
        confirmLabel="Grant Administrator"
        typeToConfirm="ADMINISTRATOR"
        onConfirm={() => {
          setAdminConfirmed(true)
          setPerms((p) => [...new Set([...p, "administrator"])])
        }}
      />
    </>
  )
}
