import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { apiFetch } from "@/api/client"
import { useLiveConnected, useLiveTopic } from "@/live/useLive"
import { invalidateSoon } from "@/live/invalidate"
import type {
  Ticket,
  TicketActionResponse,
  TicketMessagesResponse,
  TicketPanel,
  TicketStatus,
} from "@/types/api"

/** Fallback poll for ticket lists/detail, used ONLY while the live socket is down. */
export const TICKETS_FALLBACK_POLL_MS = 30_000
/**
 * The bot publishes no `tickets` event for a new chat message (only opened/
 * claimed/closed/archived/deleted), so the detail page's chat log polls at
 * this interval while it is open, live or not.
 */
export const TICKET_MESSAGES_POLL_MS = 10_000

/**
 * LIVE `tickets` topic (requires "manage_tickets"): opened/claimed/closed/
 * archived/deleted events re-fetch the (active) ticket lists and detail views.
 * Mount once per ticket page. Uses the coalesced invalidation (live/invalidate)
 * because the sidebar's open-ticket badge listens to the same topic.
 */
export function useTicketsLive() {
  const queryClient = useQueryClient()
  useLiveTopic("tickets", () => {
    invalidateSoon(queryClient, ["tickets"])
  })
}

function ticketsQueryKey(status: TicketStatus | "all") {
  return ["tickets", status] as const
}

function ticketQueryKey(ticketId: string) {
  return ["tickets", "detail", ticketId] as const
}

async function fetchTickets(status: TicketStatus | "all", signal?: AbortSignal): Promise<Ticket[]> {
  return apiFetch<Ticket[]>(`/api/tickets?status=${encodeURIComponent(status)}`, { signal })
}

/**
 * Wraps GET /api/tickets?status=... (requires "manage_tickets" server-side).
 * Live via the `tickets` topic; polls only while the socket is down.
 */
export function useTickets(status: TicketStatus | "all", enabled = true) {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: ticketsQueryKey(status),
    queryFn: ({ signal }) => fetchTickets(status, signal),
    enabled,
    refetchInterval: connected ? false : TICKETS_FALLBACK_POLL_MS,
  })
}

async function fetchTicket(ticketId: string, signal?: AbortSignal): Promise<Ticket> {
  return apiFetch<Ticket>(`/api/tickets/${encodeURIComponent(ticketId)}`, { signal })
}

/** Wraps GET /api/tickets/{ticketId} (requires "manage_tickets" server-side). Polls only while the socket is down. */
export function useTicket(ticketId: string) {
  const connected = useLiveConnected()
  return useQuery({
    queryKey: ticketQueryKey(ticketId),
    queryFn: ({ signal }) => fetchTicket(ticketId, signal),
    enabled: ticketId.length > 0,
    refetchInterval: connected ? false : TICKETS_FALLBACK_POLL_MS,
  })
}

async function fetchTicketPanels(signal?: AbortSignal): Promise<TicketPanel[]> {
  return apiFetch<TicketPanel[]>("/api/ticket-panels", { signal })
}

/**
 * Wraps GET /api/ticket-panels (requires "manage_tickets" server-side).
 * Used to show which panel ("Verify Me", "Get Help", etc.) a ticket came
 * from — read-only context, no panel management here.
 */
export function useTicketPanels() {
  return useQuery({
    queryKey: ["ticket-panels"],
    queryFn: ({ signal }) => fetchTicketPanels(signal),
  })
}

function invalidateTicket(queryClient: ReturnType<typeof useQueryClient>, ticketId: string) {
  // Broad invalidate of every status-filtered list (open/closed/archived/all)
  // since we don't know client-side which tab the user was on, or which
  // status the action just moved this ticket out of. The ["tickets"] prefix
  // also covers this ticket's detail and messages, and invalidateSoon merges
  // it with the live event the bot publishes for the same action.
  invalidateSoon(queryClient, ticketQueryKey(ticketId), ["tickets"])
}

async function closeTicketRequest(ticketId: string): Promise<TicketActionResponse> {
  return apiFetch<TicketActionResponse>(`/api/tickets/${encodeURIComponent(ticketId)}/close`, {
    method: "POST",
  })
}

/** Wraps POST /api/tickets/{ticketId}/close. Invalidates the ticket detail + all list queries on success. */
export function useCloseTicket() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: closeTicketRequest,
    onSuccess: (_data, ticketId) => invalidateTicket(queryClient, ticketId),
  })
}

async function claimTicketRequest(ticketId: string): Promise<TicketActionResponse> {
  return apiFetch<TicketActionResponse>(`/api/tickets/${encodeURIComponent(ticketId)}/claim`, {
    method: "POST",
  })
}

/** Wraps POST /api/tickets/{ticketId}/claim. Invalidates the ticket detail + all list queries on success. */
export function useClaimTicket() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: claimTicketRequest,
    onSuccess: (_data, ticketId) => invalidateTicket(queryClient, ticketId),
  })
}

async function deleteTicketRequest(ticketId: string): Promise<TicketActionResponse> {
  return apiFetch<TicketActionResponse>(`/api/tickets/${encodeURIComponent(ticketId)}`, {
    method: "DELETE",
  })
}

/**
 * Wraps DELETE /api/tickets/{ticketId} — server-side only allows this for
 * status === "archived" tickets (server/routes/tickets.py -> ticket_service
 * .delete_ticket rejects anything else with a 400). Destructive and
 * irreversible; the caller (UI) is responsible for confirming with the user
 * before calling `.mutate()`; this hook does not itself prompt.
 * Invalidates the ticket detail + all list queries, and the caller should
 * also navigate away since the detail query will 404 after this succeeds.
 */
export function useDeleteTicket() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: deleteTicketRequest,
    onSuccess: (_data, ticketId) => invalidateTicket(queryClient, ticketId),
  })
}

async function fetchTicketMessages(ticketId: string, signal?: AbortSignal): Promise<TicketMessagesResponse> {
  return apiFetch<TicketMessagesResponse>(`/api/tickets/${encodeURIComponent(ticketId)}/messages`, { signal })
}

/**
 * Wraps GET /api/tickets/{ticketId}/messages (requires "manage_tickets"
 * server-side). `enabled: false` in the response means chat logging is off
 * server-side (config.TICKET_CHAT_LOGGING_ENABLED) — the UI should show a
 * disabled-state indicator rather than treating an empty list as "no
 * messages yet". Only meaningful for open/archived tickets, but the hook
 * itself doesn't restrict on status — the caller decides when to render it.
 *
 * `poll` (the detail page passes it while the ticket is OPEN): re-fetch every
 * TICKET_MESSAGES_POLL_MS, because no live event exists for new chat messages.
 * Ticket status events still invalidate it through the ["tickets"] prefix.
 */
export function useTicketMessages(ticketId: string, enabled = true, poll = false) {
  return useQuery({
    queryKey: ["tickets", "messages", ticketId],
    queryFn: ({ signal }) => fetchTicketMessages(ticketId, signal),
    enabled: enabled && ticketId.length > 0,
    refetchInterval: poll ? TICKET_MESSAGES_POLL_MS : false,
  })
}
