import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import type {
  CreateTemplateResponse,
  GuildChannel,
  TemplateEmbed,
  TemplateFull,
  TemplateSummary,
} from "@/types/api"

function templatesQueryKey(category?: string) {
  return ["templates", category ?? "all"] as const
}

function templateQueryKey(templateId: number) {
  return ["templates", "detail", templateId] as const
}

async function fetchTemplates(category?: string, signal?: AbortSignal): Promise<TemplateSummary[]> {
  const qs = category ? `?category=${encodeURIComponent(category)}` : ""
  return apiFetch<TemplateSummary[]>(`/api/templates${qs}`, { signal })
}

/** Wraps GET /api/templates?category=... (requires "manage_templates" server-side). */
export function useTemplates(category?: string) {
  return useQuery({
    queryKey: templatesQueryKey(category),
    queryFn: ({ signal }) => fetchTemplates(category, signal),
  })
}

async function fetchTemplate(templateId: number, signal?: AbortSignal): Promise<TemplateFull> {
  return apiFetch<TemplateFull>(`/api/templates/${templateId}`, { signal })
}

/** Wraps GET /api/templates/{id} (requires "manage_templates" server-side). */
export function useTemplate(templateId: number | null) {
  return useQuery({
    queryKey: templateQueryKey(templateId ?? -1),
    queryFn: ({ signal }) => fetchTemplate(templateId as number, signal),
    enabled: templateId !== null,
  })
}

export interface CreateTemplateInput {
  name: string
  description?: string | null
  category?: string | null
  content?: string | null
}

async function createTemplateRequest(input: CreateTemplateInput): Promise<CreateTemplateResponse> {
  return apiFetch<CreateTemplateResponse>("/api/templates", {
    method: "POST",
    body: JSON.stringify(input),
  })
}

/** Wraps POST /api/templates. Invalidates the template list on success. */
export function useCreateTemplate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: createTemplateRequest,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["templates"] }),
  })
}

export interface UpdateTemplateInput {
  templateId: number
  name?: string
  description?: string | null
  category?: string | null
  content?: string | null
}

async function updateTemplateRequest(input: UpdateTemplateInput): Promise<{ success: boolean }> {
  const { templateId, ...body } = input
  return apiFetch<{ success: boolean }>(`/api/templates/${templateId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  })
}

/** Wraps PATCH /api/templates/{id}. Invalidates the detail + list queries on success. */
export function useUpdateTemplate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: updateTemplateRequest,
    onSuccess: (_data, input) => {
      queryClient.invalidateQueries({ queryKey: templateQueryKey(input.templateId) })
      queryClient.invalidateQueries({ queryKey: ["templates"] })
    },
  })
}

export interface ReplaceEmbedsInput {
  templateId: number
  embeds: TemplateEmbed[]
}

async function replaceEmbedsRequest(input: ReplaceEmbedsInput): Promise<TemplateFull> {
  return apiFetch<TemplateFull>(`/api/templates/${input.templateId}/embeds`, {
    method: "PUT",
    body: JSON.stringify({ embeds: input.embeds }),
  })
}

/** Wraps PUT /api/templates/{id}/embeds — replaces the FULL embed set in one call. */
export function useReplaceTemplateEmbeds() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: replaceEmbedsRequest,
    onSuccess: (_data, input) => {
      queryClient.invalidateQueries({ queryKey: templateQueryKey(input.templateId) })
      queryClient.invalidateQueries({ queryKey: ["templates"] })
    },
  })
}

async function fetchTemplateChannels(signal?: AbortSignal): Promise<GuildChannel[]> {
  return apiFetch<GuildChannel[]>("/api/templates/channels", { signal })
}

/** Wraps GET /api/templates/channels — text-channel picker for the "send to channel" action. */
export function useTemplateChannels() {
  return useQuery({
    queryKey: ["templates", "channels"],
    queryFn: ({ signal }) => fetchTemplateChannels(signal),
  })
}

export interface PostTemplateInput {
  templateId: number
  channelId: string
}

async function postTemplateRequest(input: PostTemplateInput): Promise<{ message_id: string; channel_id: string }> {
  return apiFetch(`/api/templates/${input.templateId}/post`, {
    method: "POST",
    body: JSON.stringify({ channel_id: input.channelId }),
  })
}

/** Wraps POST /api/templates/{id}/post — renders the template and sends it
 *  live to a channel as a tracked managed message (auto-updates on future
 *  template edits, same as the `/template post` slash command). */
export function usePostTemplate() {
  return useMutation({
    mutationFn: postTemplateRequest,
  })
}

async function deleteTemplateRequest(templateId: number): Promise<{ success: boolean }> {
  return apiFetch<{ success: boolean }>(`/api/templates/${templateId}`, {
    method: "DELETE",
  })
}

/** Wraps DELETE /api/templates/{id}. Invalidates the template list on success. */
export function useDeleteTemplate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: deleteTemplateRequest,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["templates"] }),
  })
}

// ---------------------------------------------------------------------------
// Parity with /template history | rollback | export | import | preview
// (all manage_templates). Rollback re-syncs managed messages bot-side.
// ---------------------------------------------------------------------------

export interface TemplateVersion {
  version_number: number
  /** Discord snowflake (user who made the change). */
  changed_by: string | null
  change_summary: string | null
  created_at: string | null
}

export function useTemplateVersions(templateId: number | null) {
  return useQuery({
    queryKey: ["templates", "versions", templateId ?? -1],
    queryFn: ({ signal }) => apiFetch<{ versions: TemplateVersion[] }>(`/api/templates/${templateId}/versions`, { signal }),
    enabled: templateId !== null,
  })
}

export function useRollbackTemplate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { templateId: number; version: number }) =>
      apiFetch<{ success: boolean }>(`/api/templates/${input.templateId}/rollback`, {
        method: "POST",
        body: JSON.stringify({ version_number: input.version }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["templates"] }),
  })
}

export type TemplateExport = Record<string, unknown> & { name: string }

export function exportTemplate(templateId: number): Promise<TemplateExport> {
  return apiFetch<TemplateExport>(`/api/templates/${templateId}/export`)
}

export function useImportTemplate() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (input: { data: Record<string, unknown>; name_override?: string | null }) =>
      apiFetch<{ template_id: number; name: string }>("/api/templates/import", { method: "POST", body: JSON.stringify(input) }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["templates"] }),
  })
}

/** Discord embed dict (discord.Embed.to_dict) as rendered by the bot. */
export interface RenderedEmbed {
  title?: string
  description?: string
  url?: string
  color?: number
  footer?: { text?: string; icon_url?: string }
  image?: { url?: string }
  thumbnail?: { url?: string }
  author?: { name?: string; url?: string; icon_url?: string }
  fields?: { name: string; value: string; inline?: boolean }[]
  timestamp?: string
}

export function useBotPreview() {
  return useMutation({
    mutationFn: (templateId: number) =>
      apiFetch<{ content: string | null; embeds: RenderedEmbed[] }>(`/api/templates/${templateId}/preview`),
  })
}
