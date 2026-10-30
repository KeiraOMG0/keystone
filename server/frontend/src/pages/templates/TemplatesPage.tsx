import { useMemo, useState, type FormEvent } from "react"
import { Link, useNavigate } from "react-router-dom"
import { Ellipsis, FileText, Pencil, Plus, Search, Trash2, Upload } from "lucide-react"
import { ImportTemplateDialog } from "./ImportTemplateDialog"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Field } from "@/components/ui/field"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow, SortableHead } from "@/components/ui/table"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { useToast } from "@/components/ui/toast"
import { PageContainer, PageHeader } from "@/components/patterns/PageHeader"
import { ConfirmDialog } from "@/components/patterns/ConfirmDialog"
import { EmptyState, ErrorState, ListSkeleton, TableSkeleton } from "@/components/patterns/states"
import Forbidden403 from "@/pages/forbidden/Forbidden403"
import { useCreateTemplate, useDeleteTemplate, useTemplates } from "@/api/templates"
import { ApiError } from "@/api/client"
import { describeApiError } from "@/lib/apiError"
import { useMediaQuery } from "@/hooks/useMediaQuery"
import { formatDateTime, formatRelativeTime } from "@/lib/format"
import type { ForbiddenError, TemplateSummary } from "@/types/api"

const NAME_PATTERN = /^[\w-]+$/
const UNCATEGORIZED = "__uncategorized__"

type SortKey = "name" | "updated"

/**
 * PROTECTED. Message templates: category tabs, search, a sortable table with a
 * row menu (Edit / Delete with confirm), and a New template dialog. Backed by
 * GET/POST/DELETE /api/templates (needs "manage_templates"); a 403 renders
 * <Forbidden403> in place.
 */
export default function TemplatesPage() {
  const templatesQuery = useTemplates()
  const createTemplate = useCreateTemplate()
  const deleteTemplate = useDeleteTemplate()
  const { toast } = useToast()
  const navigate = useNavigate()
  const isPhone = useMediaQuery("(max-width: 639px)")

  const [dialogOpen, setDialogOpen] = useState(false)
  const [newName, setNewName] = useState("")
  const [newCategory, setNewCategory] = useState("")
  const [nameTouched, setNameTouched] = useState(false)
  const [toDelete, setToDelete] = useState<TemplateSummary | null>(null)
  const [importOpen, setImportOpen] = useState(false)

  const [search, setSearch] = useState("")
  const [categoryFilter, setCategoryFilter] = useState("all")
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({ key: "name", dir: "asc" })

  const categories = useMemo(() => {
    const set = new Set<string>()
    for (const template of templatesQuery.data ?? []) {
      if (template.category) set.add(template.category)
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b))
  }, [templatesQuery.data])

  const rows = useMemo(() => {
    const query = search.trim().toLowerCase()
    const filtered = (templatesQuery.data ?? []).filter((template) => {
      if (categoryFilter === UNCATEGORIZED && template.category) return false
      if (categoryFilter !== "all" && categoryFilter !== UNCATEGORIZED && template.category !== categoryFilter) return false
      if (!query) return true
      return template.name.toLowerCase().includes(query) || (template.description?.toLowerCase().includes(query) ?? false)
    })
    const dir = sort.dir === "asc" ? 1 : -1
    return [...filtered].sort((a, b) =>
      sort.key === "name"
        ? a.name.localeCompare(b.name) * dir
        : (new Date(a.updated_at).getTime() - new Date(b.updated_at).getTime()) * dir
    )
  }, [templatesQuery.data, search, categoryFilter, sort])

  const forbidden =
    templatesQuery.isError &&
    templatesQuery.error instanceof ApiError &&
    templatesQuery.error.status === 403 &&
    isForbiddenError(templatesQuery.error.body)
      ? (templatesQuery.error.body as ForbiddenError)
      : null

  if (forbidden) {
    return <Forbidden403 reason="forbidden" requiredPermission={forbidden.required_permission} />
  }

  const trimmedName = newName.trim()
  const nameInvalid = trimmedName.length > 0 && !NAME_PATTERN.test(trimmedName)
  const canSubmit = trimmedName.length > 0 && !nameInvalid && !createTemplate.isPending

  function handleCreate(e: FormEvent) {
    e.preventDefault()
    setNameTouched(true)
    if (!canSubmit) return
    createTemplate.mutate(
      { name: trimmedName, category: newCategory.trim() || null },
      {
        onSuccess: (res) => {
          setDialogOpen(false)
          setNewName("")
          setNewCategory("")
          setNameTouched(false)
          toast({ title: "Template created", variant: "success" })
          navigate(`/dashboard/templates/${res.template_id}`)
        },
        onError: (error) => {
          toast({
            title: "Failed to create template",
            description: describeApiError(error, "Failed to create template."),
            variant: "destructive",
          })
        },
      }
    )
  }

  function handleDelete(template: TemplateSummary) {
    deleteTemplate.mutate(template.id, {
      onSuccess: () => toast({ title: "Template deleted", variant: "success" }),
      onError: (error) => {
        toast({
          title: "Failed to delete template",
          description: describeApiError(error, "Failed to delete template."),
          variant: "destructive",
        })
      },
    })
  }

  function toggleSort(key: SortKey) {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "name" ? "asc" : "desc" }))
  }
  const dirFor = (key: SortKey) => (sort.key === key ? sort.dir : false)

  return (
    <PageContainer>
      <PageHeader
        title="Message templates"
        description="Reusable messages and embeds used by tickets, events and schedules."
        actions={
          <>
            <Button variant="secondary" onClick={() => setImportOpen(true)}>
              <Upload /> Import
            </Button>
            <Button onClick={() => setDialogOpen(true)}>
              <Plus /> New template
            </Button>
          </>
        }
      />

      <Card className="gap-0 py-0">
        <CardToolbar>
          {templatesQuery.data && templatesQuery.data.length > 0 ? (
            <div className="border-b border-border">
              <Tabs value={categoryFilter} onValueChange={setCategoryFilter} className="gap-0">
                <TabsList className="px-3">
                  <TabsTrigger value="all">All</TabsTrigger>
                  {categories.map((category) => (
                    <TabsTrigger key={category} value={category}>
                      {category}
                    </TabsTrigger>
                  ))}
                  <TabsTrigger value={UNCATEGORIZED}>Uncategorized</TabsTrigger>
                </TabsList>
              </Tabs>
              <div className="px-4 pb-3 pt-3">
                <div className="relative w-full sm:w-72">
                  <Search aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-subtle-foreground" />
                  <Input
                    type="text"
                    data-page-search
                    aria-label="Search templates"
                    placeholder="Search templates"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    className="h-[30px] w-full pl-9 text-sm max-md:h-11"
                  />
                </div>
              </div>
            </div>
          ) : null}
        </CardToolbar>

        {templatesQuery.isPending ? (isPhone ? <ListSkeleton rows={4} avatar={false} /> : <TableSkeleton rows={4} cols={4} />) : null}

        {templatesQuery.isError && !forbidden ? (
          <ErrorState
            title="Could not load templates"
            message={describeApiError(templatesQuery.error)}
            onRetry={() => void templatesQuery.refetch()}
            retrying={templatesQuery.isRefetching}
            showStatusLink
          />
        ) : null}

        {templatesQuery.data ? (
          templatesQuery.data.length === 0 ? (
            <EmptyState
              icon={FileText}
              title="No templates yet"
              description="Templates are reusable messages and embeds."
              action={
                <Button onClick={() => setDialogOpen(true)}>
                  <Plus /> New template
                </Button>
              }
            />
          ) : rows.length === 0 ? (
            <EmptyState icon={Search} title="No matches" description={search.trim() ? `No templates match "${search.trim()}".` : "No templates match this filter."} />
          ) : isPhone ? (
            <ul className="flex flex-col divide-y divide-border">
              {rows.map((t) => (
                <li key={t.id} className="flex items-center gap-2 pr-2">
                  <Link to={`/dashboard/templates/${t.id}`} className="flex min-h-12 min-w-0 flex-1 flex-col justify-center px-4 py-2 hover:bg-hover">
                    <span className="truncate font-mono text-sm font-medium text-primary">{t.name}</span>
                    <span className="truncate text-xs text-muted-foreground">
                      {t.category ?? "Uncategorized"} · {formatRelativeTime(t.updated_at)}
                    </span>
                  </Link>
                  <RowMenu template={t} onEdit={() => navigate(`/dashboard/templates/${t.id}`)} onDelete={() => setToDelete(t)} />
                </li>
              ))}
            </ul>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableHead direction={dirFor("name")} onSort={() => toggleSort("name")}>
                    Name
                  </SortableHead>
                  <TableHead>Category</TableHead>
                  <TableHead>Description</TableHead>
                  <SortableHead direction={dirFor("updated")} onSort={() => toggleSort("updated")}>
                    Updated
                  </SortableHead>
                  <TableHead className="w-12 text-right">
                    <span className="sr-only">Actions</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell>
                      <Link to={`/dashboard/templates/${t.id}`} className="rounded-sm font-mono text-xs font-medium text-primary hover:underline">
                        {t.name}
                      </Link>
                    </TableCell>
                    <TableCell>{t.category ? <Badge variant="accent">{t.category}</Badge> : <span className="text-subtle-foreground">Uncategorized</span>}</TableCell>
                    <TableCell className="max-w-[280px] truncate text-muted-foreground">{t.description || "No description"}</TableCell>
                    <TableCell className="text-muted-foreground" title={formatDateTime(t.updated_at)}>
                      {formatRelativeTime(t.updated_at)}
                    </TableCell>
                    <TableCell className="text-right">
                      <RowMenu template={t} onEdit={() => navigate(`/dashboard/templates/${t.id}`)} onDelete={() => setToDelete(t)} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )
        ) : null}
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-w-[560px]">
          <DialogHeader>
            <DialogTitle>New template</DialogTitle>
            <DialogDescription>Create a named template, then edit its embed content on the next screen.</DialogDescription>
          </DialogHeader>
          <form onSubmit={handleCreate} className="flex flex-col gap-4">
            <Field label="Name" required hint="Letters, digits, - and _" error={nameTouched && nameInvalid ? "Only letters, digits, - and _ are allowed." : null}>
              {(p) => (
                <Input
                  {...p}
                  className="font-mono"
                  autoFocus
                  autoComplete="off"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onBlur={() => setNameTouched(true)}
                />
              )}
            </Field>
            <Field label="Category" hint="Optional. Used for the tabs on this page.">
              {(p) => <Input {...p} value={newCategory} onChange={(e) => setNewCategory(e.target.value)} />}
            </Field>
            <DialogFooter>
              <Button type="button" variant="secondary" onClick={() => setDialogOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={trimmedName.length === 0 || nameInvalid} loading={createTemplate.isPending}>
                {createTemplate.isPending ? "Creating" : "Create and edit"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ImportTemplateDialog open={importOpen} onOpenChange={setImportOpen} />

      <ConfirmDialog
        open={toDelete !== null}
        onOpenChange={(o) => !o && setToDelete(null)}
        title={`Delete template "${toDelete?.name ?? ""}"?`}
        description="Anything that uses this template by name will stop finding it. This cannot be undone."
        confirmLabel="Delete template"
        confirmIcon={<Trash2 />}
        onConfirm={() => toDelete && handleDelete(toDelete)}
      />
    </PageContainer>
  )
}

/** Thin wrapper so the filter row can sit inside the card (kept for readability). */
function CardToolbar({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}

function RowMenu({ template, onEdit, onDelete }: { template: TemplateSummary; onEdit: () => void; onDelete: () => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${template.name}`} className="max-md:size-11">
          <Ellipsis />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuItem onSelect={onEdit}>
          <Pencil /> Edit
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem danger onSelect={onDelete}>
          <Trash2 /> Delete
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function isForbiddenError(body: unknown): body is ForbiddenError {
  return (
    typeof body === "object" &&
    body !== null &&
    "error" in body &&
    (body as { error: unknown }).error === "forbidden"
  )
}
