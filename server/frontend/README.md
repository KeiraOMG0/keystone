# Dashboard frontend

React 19 + TypeScript + Vite 8 + Tailwind v4 + Radix/shadcn UI + `@tanstack/react-query` + Recharts. This is the UI for the Keystone (formerly kick-bot) staff dashboard. Every request is a same-origin `fetch` to `/api/*` with cookies, answered by the dashboard backend in `server/` (which proxies to the bot's internal API). Live updates come over one same-origin WebSocket (`/api/ws`, ARCHITECTURE section 22). No JWTs; auth state comes from `GET /api/me`.

Full system documentation: [`../../docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) (section 9 for routes and pages).

## Commands

Run from this directory (`server/frontend`):

```
npm install
npm run dev       # Vite dev server on :5173, proxies /api to http://127.0.0.1:8080
npm run build     # tsc -b && vite build -> dist/
npm run lint      # oxlint
npm run preview   # serve the built dist/ locally
```

`npm run build` doubles as the type check. The production dashboard serves `dist/` from the FastAPI backend (`server/app.py`), so after changing the frontend, rebuild and refresh the browser; no backend restart is needed. `dist/` is what actually gets served, not the dev server.

The dev server only works while the dashboard backend (`uvicorn server.app:app`, port 8080) and the bot are running. For OAuth login use `http://localhost:8080` (not `127.0.0.1`), because the redirect URI and cookies are host-specific.

## Layout

```
src/
  main.tsx, app/        App shell, providers, route tree (routes.tsx), RequireAuth guard
  pages/                One folder per page (lazy-loaded chunks)
  components/           layout/ (AppShell, Sidebar, navLinks.ts, CommandPalette, breadcrumbs, useHotkeys),
                        patterns/ (PageHeader, states, StatCard, SaveBar, ConfirmDialog, LivePill, ChartCard...),
                        embed/ (EmbedPreview, DiscordMessage), ui/ (shadcn-style primitives)
  api/                  One module per backend area; react-query hooks + apiFetch client
  context/, hooks/      AuthContext, ThemeContext (light/dark/system), PrefsContext (density, sidebar), DeveloperMode,
                        useAuth, useResolve, useBotOffline, useMediaQuery
  lib/                  utils (cn), format (relative time, Discord ID check), storage (try/catch localStorage), chart (palette)
  styles/globals.css    THE design tokens (:root light, .dark), type scale, motion utilities
  types/
```

## Routes

| Path | Page | Access |
|---|---|---|
| `/` | Home / login | public |
| `/status` | Service status | public |
| `/appeal`, `/appeal/:token` | Public ban-appeal form (outside the app shell) | public |
| `/dashboard` | Overview | logged-in guild member |
| `/dashboard/moderation` | Moderation | `moderate_members` |
| `/dashboard/honeypot` | Honeypot config, catches, stats, appeals | `moderate_members` (approve/deny and edits need `ban_members`) |
| `/dashboard/tickets`, `/tickets/:ticketId`, `/tickets/panels/new` | Tickets and panel builder | `manage_tickets` |
| `/dashboard/self-roles` | Reaction-role panels | `manage_reaction_roles` |
| `/dashboard/templates`, `/templates/:templateId` | Template list and embed editor | `manage_templates` |
| `/dashboard/qotd` | Question of the Day | `manage_qotd` |
| `/dashboard/permissions` | Permission grants | server-side `require_staff` |
| `/dashboard/settings` | Guild settings | server-side `require_staff` |
| `/dashboard/diagnostics` | Ops console | server-side `require_staff` |

Nav visibility (`requiredPermission` in `src/components/layout/navLinks.ts`) is UX only; every backend route enforces permissions itself.

## Design system and theming

The spec is `docs/DESIGN_SYSTEM.md`. Short version:

- **Tokens live in `src/styles/globals.css`** (`:root` = light default, `.dark` = hand-tuned dark), mapped to Tailwind through `@theme inline`. Use tokens (`bg-card`, `text-muted-foreground`, `text-danger`, `bg-danger-soft`, `border-input`, `bg-primary-soft`...), never raw hex. Exceptions that mirror Discord: the embed colour bar, the Discord button blue (`#5865F2`) and the APP badge.
- **Theme:** `localStorage["kb-theme"]` = `light | dark | system`. The inline script in `index.html` sets the `dark` class before first paint; `ThemeContext` keeps it in sync (and follows the OS while on `system`). The toggle is in the profile menu; palette action "Switch theme".
- **Fonts:** Geist and Geist Mono, self-hosted via `@fontsource-variable/geist(-mono)` imported in `main.tsx` (no third-party requests).
- **Per-browser preferences** (all try/catch): theme, table density (`kb-density`, `<html data-density>`), collapsed sidebar (`kb-sidebar`), last tab on Honeypot/QOTD, recent pages for the palette.
- **Text scale:** `text-2xs..text-3xl` are redefined in `globals.css` (base = 14px). Page title `text-2xl`, card title `text-md`.
- **Charts:** colours only through `CHART` (`src/lib/chart.ts`, `var(--chart-n)`), series 1..5 in colour-blind-safe order; every chart has `role="img"` + `aria-label`.
- **Destructive text/badges:** use `text-danger` / `bg-danger-soft`. `bg-destructive` is only for solid confirm buttons.
- **Motion:** `.kb-shimmer`, `.kb-live-dot`, `.kb-new` (live item tint) are all disabled under `prefers-reduced-motion`.
- **Do not nest `Segmented`/ToggleGroup inside a Radix DropdownMenu** (nested roving focus froze the renderer). The profile menu uses plain radio buttons for the theme.

### Shared building blocks

`patterns/PageHeader` (+`PageContainer` widths), `patterns/states` (`EmptyState`, `ErrorState`, `ListSkeleton`, `TableSkeleton`, `StatRowSkeleton`, `PageSkeleton`), `StatCard`, `ChartCard`, `SaveBar` (sticky "N unsaved changes"), `ConfirmDialog` (named target, optional type-to-confirm), `DensityToggle`, `StaffOnlyBadge`, `LivePill`. UI primitives in `components/ui/`: `button` (variants incl. `destructive-outline`, `loading` prop), `badge` (+`CountBadge`), `field` (label/hint/error wiring, `CharCounter`), `alert`, `table` (+`SortableHead`), `tabs`, `segmented`, `checkbox`, `switch`, `dropdown-menu`, `tooltip` (`Tip`), `sheet`, `progress`, `kbd`, `toast` (supports `action: { label, onClick }` for Undo).

### Shell, nav and shortcuts

`AppShell` = skip link, sidebar (248px, 64px rail via `[` or the button; rail by default at tablet widths), 56px header (breadcrumbs from `NAV_LINKS` + `useBreadcrumb("#0141")` on detail pages, palette trigger, `LivePill`, profile menu), connection banners (bot offline / offline polling / reconnecting after 10 s / back online), phone drawer + bottom nav. Nav groups: Overview, Moderation, Community, System; ticket and pending-appeal count badges come from `useTickets("open")` / `useHoneypotAppeals("pending")`. Shortcuts: `Ctrl/Cmd+K`, `?`, `/` (focuses `[data-page-search]`), `[`, `g` then `o/m/h/t`.

## Adding a page

1. Create `src/pages/<feature>/<Name>Page.tsx`. Wrap content in `PageContainer` + `PageHeader` (title, one-line description, ONE primary action); render loading as a skeleton, empty/error with `EmptyState`/`ErrorState` (with retry), and a 403 with the inline `Forbidden403`. Disable bot-dependent actions with `useBotOffline()` (reason in `title`).
2. Lazy-import it and add a `<Route>` under `/dashboard` in `src/app/routes.tsx`.
3. Add an entry to `NAV_LINKS` in `src/components/layout/navLinks.ts` (`group`: overview | moderation | community | system; drives the sidebar, drawer, bottom nav, breadcrumbs, palette and `g` shortcuts; `commands.ts` holds palette actions).
4. Add a `src/api/<feature>.ts` module for its react-query hooks; live pages use `useLiveTopic` plus `refetchInterval: connected ? false : <interval>`.
5. Edit forms use `Field` + `SaveBar`; destructive actions use `ConfirmDialog`; reversible ones use a toast with an Undo `action` ONLY when the API has a real inverse call.

## Gotchas

- Discord IDs are 64-bit; the API sends them as **strings**. Never wrap them in `Number()`.
- The `@/` import alias maps to `src/`.
- `dist/`, `node_modules/` are build output; do not edit them.
