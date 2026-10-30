import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import type {
  CreateTicketPanelRequest,
  CreateTicketPanelResponse,
  GuildCategory,
  GuildChannel,
} from "@/types/api"

async function fetchCategories(signal?: AbortSignal): Promise<GuildCategory[]> {
  return apiFetch<GuildCategory[]>("/api/categories", { signal })
}

/** Wraps GET /api/categories (requires "manage_tickets" server-side). */
export function useGuildCategories() {
  return useQuery({
    queryKey: ["categories"],
    queryFn: ({ signal }) => fetchCategories(signal),
  })
}

async function fetchTicketPanelChannels(signal?: AbortSignal): Promise<GuildChannel[]> {
  return apiFetch<GuildChannel[]>("/api/ticket-panel-channels", { signal })
}

/** Wraps GET /api/ticket-panel-channels (requires "manage_tickets" server-side). */
export function useTicketPanelChannels() {
  return useQuery({
    queryKey: ["ticket-panel-channels"],
    queryFn: ({ signal }) => fetchTicketPanelChannels(signal),
  })
}

async function createTicketPanelRequest(
  input: CreateTicketPanelRequest
): Promise<CreateTicketPanelResponse> {
  return apiFetch<CreateTicketPanelResponse>("/api/ticket-panels", {
    method: "POST",
    body: JSON.stringify(input),
  })
}

/**
 * Wraps POST /api/ticket-panels. This actually posts a live Discord message
 * (via the internal API's TicketService.create_panel + post_panel) — check
 * the response's `post_errors` array even on a successful (2xx) mutation,
 * since the panel row can be created while the Discord post itself fails.
 * Invalidates the ticket-panels list on success so TicketsPage picks up the
 * new panel immediately.
 */
export function useCreateTicketPanel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: createTicketPanelRequest,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["ticket-panels"] }),
  })
}
