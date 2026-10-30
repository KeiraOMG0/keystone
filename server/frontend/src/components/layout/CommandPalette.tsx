import { useEffect, useMemo, useState } from "react"
import { Dialog } from "radix-ui"
import { useNavigate } from "react-router-dom"
import { CornerDownLeft, Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { buildCommands, type Command, type CommandGroup } from "@/components/layout/commands"
import type { NavItem } from "@/components/layout/navLinks"
import { useAuth } from "@/hooks/useAuth"
import { useTheme } from "@/context/ThemeContext"
import { Kbd } from "@/components/ui/kbd"
import { readRecent } from "@/lib/storage"
import { guardedNavigate } from "@/hooks/useUnsavedChangesGuard"

const GROUP_ORDER: CommandGroup[] = ["Recent", "Pages", "Settings", "Actions"]

interface CommandPaletteProps {
  items: NavItem[]
  open: boolean
  onOpenChange: (open: boolean) => void
  onShowShortcuts: () => void
}

/** Fuzzy score: substring beats subsequence; -1 = no match. */
function score(text: string, q: string): number {
  const t = text.toLowerCase()
  const i = t.indexOf(q)
  if (i >= 0) return 100 - i - (t.length - q.length) * 0.1
  let ti = 0
  for (const ch of q) {
    ti = t.indexOf(ch, ti)
    if (ti < 0) return -1
    ti++
  }
  return 20
}

/**
 * Ctrl+K / Cmd+K command palette: pages, individual Settings tabs (deep-linked
 * via ?tab=), actions (incl. Switch theme) and the last visited pages. Matches
 * fuzzily against each command's label AND keywords, so "warn" finds
 * Moderation even though the sidebar label is just "Moderation".
 */
export function CommandPalette({ items, open, onOpenChange: setOpen, onShowShortcuts }: CommandPaletteProps) {
  const [query, setQuery] = useState("")
  const [activeIndex, setActiveIndex] = useState(0)
  const navigate = useNavigate()
  const { permissions } = useAuth()
  const { resolved, setTheme } = useTheme()

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault()
        setOpen(!open)
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [open, setOpen])

  useEffect(() => {
    if (!open) setQuery("")
  }, [open])

  const commands = useMemo(
    () =>
      buildCommands(items, {
        cycleTheme: () => setTheme(resolved === "dark" ? "light" : "dark"),
        showShortcuts: onShowShortcuts,
      }),
    [items, resolved, setTheme, onShowShortcuts]
  )

  const visibleCommands = useMemo(
    () => commands.filter((c) => !c.requiredPermission || permissions[c.requiredPermission]),
    [commands, permissions]
  )

  const filtered = useMemo<Command[]>(() => {
    const q = query.trim().toLowerCase()
    if (!q) {
      const pageByPath = new Map(visibleCommands.filter((c) => c.group === "Pages").map((c) => [c.to, c]))
      const recent = readRecent()
        .map((p) => pageByPath.get(p))
        .filter((c): c is Command => Boolean(c))
        .map((c) => ({ ...c, id: `recent:${c.id}`, group: "Recent" as const }))
      return [...recent, ...visibleCommands]
    }
    return visibleCommands
      .map((c) => ({
        c,
        s: Math.max(score(c.label, q), ...(c.keywords ?? []).map((k) => score(k, q) - 10)),
      }))
      .filter((x) => x.s >= 0)
      .sort((a, b) => GROUP_ORDER.indexOf(a.c.group) - GROUP_ORDER.indexOf(b.c.group) || b.s - a.s)
      .map((x) => x.c)
  }, [visibleCommands, query])

  useEffect(() => {
    setActiveIndex(0)
  }, [filtered.length, query])

  const grouped = useMemo(() => {
    const map = new Map<CommandGroup, Command[]>()
    for (const cmd of filtered) {
      const list = map.get(cmd.group) ?? []
      list.push(cmd)
      map.set(cmd.group, list)
    }
    return GROUP_ORDER.filter((g) => map.has(g)).map((g) => ({ group: g, commands: map.get(g)! }))
  }, [filtered])

  // Flat order = grouped order, so arrow keys follow what is on screen.
  const flat = useMemo(() => grouped.flatMap((g) => g.commands), [grouped])

  function run(cmd: Command) {
    setOpen(false)
    if (cmd.run) cmd.run()
    else if (cmd.to) {
      const to = cmd.to
      // Asks first when the current page has unsaved changes.
      guardedNavigate(to, () => navigate(to))
    }
  }

  function handleInputKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault()
      setActiveIndex((i) => Math.min(i + 1, flat.length - 1))
    } else if (e.key === "ArrowUp") {
      e.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, 0))
    } else if (e.key === "Enter") {
      e.preventDefault()
      const target = flat[activeIndex]
      if (target) run(target)
    }
  }

  useEffect(() => {
    document.getElementById(`cmd-${flat[activeIndex]?.id}`)?.scrollIntoView({ block: "nearest" })
  }, [activeIndex, flat])

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-(--overlay) data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <Dialog.Content
          className="fixed top-[12%] left-1/2 z-50 w-[calc(100%-2rem)] max-w-xl -translate-x-1/2 overflow-hidden rounded-xl border border-border bg-popover text-popover-foreground shadow-md outline-none data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95"
          aria-describedby={undefined}
        >
          <Dialog.Title className="sr-only">Command palette</Dialog.Title>
          <div className="flex items-center gap-2 border-b border-border px-4">
            <Search className="size-4 shrink-0 text-muted-foreground" aria-hidden />
            <input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search pages, settings, actions…"
              aria-label="Search commands"
              className="h-12 w-full bg-transparent text-base text-foreground outline-none placeholder:text-subtle-foreground"
              onKeyDown={handleInputKeyDown}
              role="combobox"
              aria-expanded
              aria-controls="cmd-list"
              aria-activedescendant={flat[activeIndex] ? `cmd-${flat[activeIndex].id}` : undefined}
              aria-autocomplete="list"
            />
            <Kbd>Esc</Kbd>
          </div>
          <div id="cmd-list" className="max-h-[min(24rem,60vh)] overflow-y-auto p-1.5" role="listbox" aria-label="Commands">
            {flat.length === 0 ? (
              <p className="px-2.5 py-8 text-center text-sm text-muted-foreground">No matches for “{query}”.</p>
            ) : (
              grouped.map(({ group, commands: groupCommands }) => (
                <div key={group} role="group" aria-label={group} className="mb-1 last:mb-0">
                  <span className="block px-2.5 py-1.5 text-2xs font-semibold tracking-[0.06em] text-subtle-foreground uppercase">
                    {group}
                  </span>
                  {groupCommands.map((cmd) => {
                    const index = flat.indexOf(cmd)
                    const active = index === activeIndex
                    return (
                      <div
                        key={cmd.id}
                        id={`cmd-${cmd.id}`}
                        role="option"
                        aria-selected={active}
                        onClick={() => run(cmd)}
                        onMouseMove={() => setActiveIndex(index)}
                        className={cn(
                          "flex min-h-9 w-full cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-base text-foreground",
                          active && "bg-accent"
                        )}
                      >
                        <cmd.icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                        <span className="flex-1 truncate">{cmd.label}</span>
                        {active ? <CornerDownLeft className="size-3.5 text-subtle-foreground" aria-hidden /> : null}
                      </div>
                    )
                  })}
                </div>
              ))
            )}
          </div>
          <div className="hidden items-center gap-4 border-t border-border px-4 py-2 text-xs text-subtle-foreground sm:flex">
            <span className="flex items-center gap-1.5"><Kbd>↑</Kbd><Kbd>↓</Kbd> navigate</span>
            <span className="flex items-center gap-1.5"><Kbd>↵</Kbd> open</span>
            <span className="flex items-center gap-1.5"><Kbd>Esc</Kbd> close</span>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
