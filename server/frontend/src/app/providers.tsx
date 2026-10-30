import { useState, type ReactNode } from "react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { BrowserRouter } from "react-router-dom"
import { AuthProvider } from "@/context/AuthContext"
import { LiveProvider } from "@/live/LiveProvider"

export function AppProviders({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: false,
            retry: 1,
            // Going back to a page within 15 s reuses its data instead of refetching.
            // Saves still invalidate explicitly and live updates push changes, so
            // nothing shows stale after an edit.
            staleTime: 15_000,
          },
        },
      })
  )

  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <LiveProvider>{children}</LiveProvider>
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
