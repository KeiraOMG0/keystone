# Kick-Bot Dashboard: Design System and Redesign Spec

**Mockups:** kept privately (not part of the repository).
24 live artboards: shell + Overview (light/dark), navigation and permission gating, Moderation (light/dark), Tickets list and detail, Honeypot (configured light/dark, first-run setup, stats dialog), Diagnostics, Template editor, Settings, Permissions, public Appeal (desktop and phone), phone shell and drawer, Login/403, loading/empty/error/offline states, components and tokens (light/dark). The artboards use a scratch stylesheet (`kb.css`) that mirrors the tokens below. Where the mockup and this file disagree, this file is the spec.

**Audience:** the engineer implementing the redesign in `server/frontend` (React 19, TypeScript, Tailwind v4, shadcn/ui on the `radix-ui` package, Recharts, TanStack Query).
**Status:** design only. Nothing in `server/`, `cogs/` or elsewhere was changed to produce this.

---

## 1. Direction

A calm, confident admin console in the spirit of Linear, Stripe and Vercel dashboards.

- Neutral cool-slate surfaces, one restrained indigo accent, generous whitespace, thin borders instead of heavy shadows.
- Light is the default. Dark is a first-class, hand-tuned theme, not an inversion. Both follow `prefers-color-scheme` until the user picks one in the profile menu.
- One accent only. Status colours (success, warning, danger, info) are separate from the accent and always paired with an icon or text, never colour alone.
- Typography: Geist (sans) and Geist Mono (IDs, code, numbers in diagnostics). Tabular numerals wherever digits line up.
- Motion is subtle (120 to 200 ms), never blocks input, and is removed under `prefers-reduced-motion`.
- Accessible by default: WCAG AA text contrast, 3:1 for control edges and focus rings, visible focus on every interactive element, full keyboard navigation, 44px touch targets on phones.

### UX principles (and what they change)

1. **One page header pattern everywhere.** Title, one-line description, primary action on the right. Users always know where they are and what the main thing to do is.
2. **Summary first, detail second.** Stats and status above tables and logs.
3. **Show state in form, not just words.** Pills, dots with icons, badges. Live/offline is always visible in the shell.
4. **Hide, don't disable, what a user cannot use** (nav items, pages). Disable only for temporary conditions (bot offline, saving) and say why.
5. **Destructive actions confirm; reversible actions undo.** Ban, kick, delete, and granting the `manage_permissions` grant use a confirm dialog that names the target. Warn, timeout, remove-warning, close ticket, approve/deny appeal use an **Undo toast** (server call delayed 5 s, or a compensating call where one exists; see section 11 for what is safe).
6. **Inline validation beside the field**, in plain words ("A Discord ID is 17 to 20 digits. This one has 15."), not only in a toast.
7. **Skeletons, not spinners,** for page and card loading. Spinners only inside buttons.
8. **Sticky context:** table filter bars and settings save bars stick while scrolling.
9. **Remember preferences per browser:** theme, table density, sidebar collapsed, last-used tab per page (localStorage, wrapped in try/catch).
10. **Fewer clicks:** primary actions on the page header, command palette for everything, keyboard shortcuts, optimistic updates for safe toggles (Honeypot enable, self-role entries).

---

## 2. What MUST NOT change

This is a visual and interaction redesign. Treat the following as frozen.

- **Routes and route structure** in `src/app/routes.tsx`: `/`, `/status`, `/appeal`, `/appeal/:token`, and under `/dashboard`: index, `moderation`, `honeypot`, `tickets`, `tickets/:ticketId`, `tickets/panels/new`, `permissions`, `self-roles`, `templates`, `templates/:templateId`, `qotd`, `settings`, `diagnostics`. The single `<RequireAuth><AppShell>` layout route stays.
- **The public `/appeal` and `/appeal/:token` routes stay outside `AppShell` and outside `RequireAuth`.** Banned users cannot sign in. No sidebar, no nav, no live socket, no authenticated calls on those pages. `/` and `/status` are also public and shell-less.
- **All API calls, hooks and payloads** in `src/api/*`. Do not rename endpoints, change query keys or reshape responses. New UI state goes in components or `localStorage`.
- **Permission gates:** `NAV_LINKS[].requiredPermission` filtering (pure UX), the per-page inline `Forbidden403` branch on 403, `permissions.ban_members` gating Honeypot management, `permissions.moderate_members` / `manage_tickets` gating Overview feeds, and `require_staff` pages (Permissions, Settings, Diagnostics). The server remains the only real enforcement.
- **The live websocket layer:** `LiveProvider`, `useLive`, `LIVE_TOPICS`, and the per-page live hooks (`useModerationLive`, `useTicketsLive`, `useHoneypotLive`, the `stats` and `diagnostics` topics). Mount each hook exactly where it is mounted today (once per page). Do not move a hook into a component that can unmount on tab change, and do not add a second socket.
- **The data shown.** Every field, stat, column, label, filter and action that exists today still exists. Redesign changes arrangement and styling. If something must move (for example Honeypot stats becoming a side card), the same data stays reachable.
- **The 403 semantics:** `not_a_member` vs `forbidden` with `required_permission` text.
- **Deep links:** `?tab=` on Settings (used by the command palette), `Ctrl/Cmd+K`.
- **Copy that carries meaning** (error explanations on `/status`, the appeal explanation text, the "How do I find my User ID?" steps). Restyle, do not rewrite.

---

## 3. Design tokens

Mechanism: CSS variables on `:root` (light) and `.dark` (dark), consumed by Tailwind v4 through `@theme inline`. Variable names match the shadcn names already in `src/styles/globals.css`, so existing `bg-primary`, `text-muted-foreground`, `border-border` classes keep working. New tokens are additive.

### 3.1 Paste-ready CSS

Replace the token block in `src/styles/globals.css` (keep `@import "tailwindcss"; @import "tw-animate-css";`).

```css
:root {
  /* surfaces */
  --background: #F6F7F9;
  --foreground: #0F1629;
  --card: #FFFFFF;
  --card-foreground: #0F1629;
  --popover: #FFFFFF;
  --popover-foreground: #0F1629;
  --secondary: #EEF0F4;
  --secondary-foreground: #0F1629;
  --muted: #EEF0F4;
  --muted-foreground: #465063;   /* secondary text  */
  --subtle-foreground: #5F6879;  /* tertiary text: timestamps, placeholders */
  --hover: #F3F4F7;              /* row and ghost hover */
  --accent: #EEF0F4;             /* shadcn accent = neutral hover surface */
  --accent-foreground: #0F1629;

  /* brand */
  --primary: #3B4BD6;
  --primary-hover: #2F3DB8;
  --primary-foreground: #FFFFFF;
  --primary-soft: #EDEFFC;       /* selected nav item, accent badge */
  --primary-soft-foreground: #3B4BD6;

  /* lines and focus */
  --border: #E3E6EC;             /* decorative dividers, card edges */
  --input: #8790A2;              /* control edges: 3.2:1 on white */
  --ring: #3B4BD6;

  /* status: text/icon colour on surfaces + soft background */
  --success: #0B7A4B;  --success-soft: #E5F5EC;
  --warning: #8F5200;  --warning-soft: #FDF1DA;
  --danger:  #BE1F2D;  --danger-soft:  #FDEBEC;
  --info:    #1B64AE;  --info-soft:    #E6F0FB;
  --destructive: #C7222F;              /* solid fill (buttons) */
  --destructive-foreground: #FFFFFF;

  /* presence (Overview), aliases onto status + neutral */
  --idle: var(--warning);
  --dnd: var(--danger);
  --offline: var(--subtle-foreground);

  /* charts, colour-blind-safe order (see 3.5) */
  --chart-1: #3B5BDB;  --chart-2: #D9730D;  --chart-3: #0E8F80;
  --chart-4: #B5389F;  --chart-5: #6B7385;

  /* sidebar */
  --sidebar: #FFFFFF;
  --sidebar-foreground: #465063;
  --sidebar-primary: #3B4BD6;
  --sidebar-primary-foreground: #FFFFFF;
  --sidebar-accent: #EDEFFC;               /* active item background */
  --sidebar-accent-foreground: #3B4BD6;
  --sidebar-border: #E3E6EC;
  --sidebar-ring: #3B4BD6;

  /* Discord-style embed preview (Honeypot catches, templates, ticket panel) */
  --embed-bg: #F2F3F5;  --embed-foreground: #2E3338;  --embed-muted: #5C6470;

  /* elevation */
  --shadow-sm: 0 1px 2px rgb(15 22 41 / .06);
  --shadow-md: 0 8px 24px rgb(15 22 41 / .10), 0 2px 6px rgb(15 22 41 / .06);
  --overlay: rgb(15 22 41 / .45);

  --radius: 0.5rem; /* 8px, base */
}

.dark {
  --background: #0C0F15;
  --foreground: #E8EBF2;
  --card: #141821;
  --card-foreground: #E8EBF2;
  --popover: #141821;
  --popover-foreground: #E8EBF2;
  --secondary: #1B202B;
  --secondary-foreground: #E8EBF2;
  --muted: #1B202B;
  --muted-foreground: #9AA3B5;
  --subtle-foreground: #8A93A6;
  --hover: #1A1F2A;
  --accent: #1B202B;
  --accent-foreground: #E8EBF2;

  --primary: #8B98FF;
  --primary-hover: #A3AEFF;
  --primary-foreground: #0B1030;
  --primary-soft: #22284A;
  --primary-soft-foreground: #A9B3FF;

  --border: #262C39;
  --input: #5F6A80;
  --ring: #8B98FF;

  --success: #4FD69C;  --success-soft: #12291F;
  --warning: #F3B95A;  --warning-soft: #2E2210;
  --danger:  #FF8A8F;  --danger-soft:  #33161A;
  --info:    #7DBBFF;  --info-soft:    #12263B;
  --destructive: #D5323F;
  --destructive-foreground: #FFFFFF;

  --chart-1: #8FA8FF;  --chart-2: #F5A64B;  --chart-3: #45D0BD;
  --chart-4: #E884D6;  --chart-5: #A3ABBD;

  --sidebar: #141821;
  --sidebar-foreground: #9AA3B5;
  --sidebar-primary: #8B98FF;
  --sidebar-primary-foreground: #0B1030;
  --sidebar-accent: #22284A;
  --sidebar-accent-foreground: #A9B3FF;
  --sidebar-border: #262C39;
  --sidebar-ring: #8B98FF;

  --embed-bg: #2B2D31;  --embed-foreground: #DBDEE1;  --embed-muted: #A3A6AA;

  --shadow-sm: 0 1px 2px rgb(0 0 0 / .4);
  --shadow-md: 0 8px 24px rgb(0 0 0 / .5), 0 0 0 1px rgb(255 255 255 / .04);
  --overlay: rgb(0 0 0 / .6);
}

html { color-scheme: light; }
html.dark { color-scheme: dark; }
```

The `--idle`, `--dnd` and `--offline` presence aliases exist so current `text-idle`, `text-dnd`, `text-offline` classes keep working. They resolve per theme through the tokens they point at.

Add to the existing `@theme inline` block:

```css
@theme inline {
  /* existing --color-* mappings stay; add: */
  --color-hover: var(--hover);
  --color-subtle-foreground: var(--subtle-foreground);
  --color-primary-hover: var(--primary-hover);
  --color-primary-soft: var(--primary-soft);
  --color-primary-soft-foreground: var(--primary-soft-foreground);
  --color-success-soft: var(--success-soft);
  --color-warning-soft: var(--warning-soft);
  --color-danger: var(--danger);
  --color-danger-soft: var(--danger-soft);
  --color-info: var(--info);
  --color-info-soft: var(--info-soft);
  --color-chart-1: var(--chart-1); --color-chart-2: var(--chart-2);
  --color-chart-3: var(--chart-3); --color-chart-4: var(--chart-4);
  --color-chart-5: var(--chart-5);
  --color-sidebar: var(--sidebar);
  --color-sidebar-foreground: var(--sidebar-foreground);
  --color-sidebar-primary: var(--sidebar-primary);
  --color-sidebar-primary-foreground: var(--sidebar-primary-foreground);
  --color-sidebar-accent: var(--sidebar-accent);
  --color-sidebar-accent-foreground: var(--sidebar-accent-foreground);
  --color-sidebar-border: var(--sidebar-border);
  --color-sidebar-ring: var(--sidebar-ring);
  --color-embed: var(--embed-bg);
  --color-embed-foreground: var(--embed-foreground);
  --color-embed-muted: var(--embed-muted);

  --radius-sm: 6px;  --radius-md: 8px;  --radius-lg: 12px;  --radius-xl: 14px;

  --shadow-sm: var(--shadow-sm);
  --shadow-md: var(--shadow-md);

  --font-sans: "Geist Variable", "Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-mono: "Geist Mono Variable", "Geist Mono", ui-monospace, "Cascadia Code", Consolas, monospace;

  /* type scale (size / line-height) */
  --text-2xs: 0.6875rem;  --text-2xs--line-height: 1rem;      /* 11/16 overline */
  --text-xs:  0.75rem;    --text-xs--line-height: 1rem;       /* 12/16 caption, badge, table head */
  --text-sm:  0.8125rem;  --text-sm--line-height: 1.25rem;    /* 13/20 secondary, dense table */
  --text-base:0.875rem;   --text-base--line-height: 1.25rem;  /* 14/20 default body, inputs, buttons */
  --text-md:  0.9375rem;  --text-md--line-height: 1.375rem;   /* 15/22 card title */
  --text-lg:  1rem;       --text-lg--line-height: 1.5rem;     /* 16/24 dialog title, lead */
  --text-xl:  1.25rem;    --text-xl--line-height: 1.75rem;    /* 20/28 section heading */
  --text-2xl: 1.5rem;     --text-2xl--line-height: 2rem;      /* 24/32 page title */
  --text-3xl: 1.75rem;    --text-3xl--line-height: 2.25rem;   /* 28/36 stat number, login, appeal */
}
```

Also: `body { @apply bg-background text-foreground font-sans antialiased; font-size: 14px; }`, `.num { font-variant-numeric: tabular-nums }` (or Tailwind's `tabular-nums`), and remove `html { color-scheme: dark; }`.

Note: the current code uses `text-destructive` for error text. In dark mode `--destructive` (solid fill) is too dark to be readable text. Replace every `text-destructive` with `text-danger` and `bg-destructive/10 text-destructive` with `bg-danger-soft text-danger` (about a dozen places; grep for it). Buttons and solid badges keep `bg-destructive text-destructive-foreground`.

### 3.2 Theme switching (light, dark, system)

- Preference stored in `localStorage["kb-theme"]` as `"light" | "dark" | "system"`. Default `"system"`.
- Apply the `dark` class on `<html>` **before first paint**. Add an inline script to `index.html` `<head>`:
  ```html
  <script>
    (function () {
      try {
        var t = localStorage.getItem("kb-theme") || "system";
        var dark = t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
        document.documentElement.classList.toggle("dark", dark);
      } catch (e) {}
    })();
  </script>
  ```
- Add `src/context/ThemeContext.tsx` (`useTheme()` returning `{ theme, resolved, setTheme }`); listen to `matchMedia` changes while theme is `"system"`. Wrap in try/catch for storage errors.
- Toggle lives in the **profile menu** as a three-way segmented control (Light, Dark, System). Also expose a command palette action "Switch theme".
- Recharts: pass `var(--chart-n)` strings for colours so charts follow the theme without re-render logic.

### 3.3 Contrast (WCAG 2.x, computed)

Text needs 4.5:1, large text and UI edges 3:1.

| Pair | Light | Dark |
|---|---|---|
| foreground on background | 16.8 | 16.1 |
| foreground on card | 18.0 | 14.9 |
| muted-foreground on card | 8.3 | 7.0 |
| muted-foreground on background | 7.7 | 7.6 |
| subtle-foreground on card | 5.6 | 5.8 |
| subtle-foreground on background | 5.2 | 6.2 |
| subtle-foreground on muted (#EEF0F4 / #1B202B) | 4.9 | 5.3 |
| primary-foreground on primary (button) | 6.6 | 7.1 |
| primary text/link on card | 6.6 | 6.8 |
| primary text on background | 6.2 | n/a |
| primary-soft-foreground on primary-soft | 5.8 | 7.2 |
| primary-foreground on primary-hover | n/a | 8.9 |
| success on success-soft | 4.8 | 8.4 |
| warning on warning-soft | 5.6 | 8.8 |
| danger on danger-soft | 5.3 | 7.3 |
| info on info-soft | 5.2 | 7.6 |
| success / danger / info / warning text on card | 5.4 / 6.1 / 6.0 / 6.2 | 9.7 / 7.9 / 8.8 / 10.1 |
| destructive-foreground on destructive (solid button) | 5.7 | 4.8 |
| `--input` control edge on card | 3.2 | 3.3 |
| `--ring` focus ring on card | 6.6 | 6.8 |
| embed foreground / muted on embed-bg | 11.5 / 5.4 | 10.2 / 5.6 |
| white on Discord blurple `#5865F2` (sign-in button) | 4.6 | 4.6 |
| chart series on card, light | 5.7 / 3.3 / 4.0 / 5.2 / 4.8 (all at least 3:1 for graphics) | dark: 7.8 / 8.8 / 9.3 / 7.4 / 7.7 |

Notes:
- `--border` (1.25:1) is decorative only (card edges, dividers, table rules). Any edge that identifies a control (input, select, checkbox, switch track) uses `--input`.
- Chart series 2 (orange) is a graphic, never text. Text that appears on a chart uses `--muted-foreground`.
- Re-run the checks if any token is changed. The values above come from the WCAG relative-luminance formula.

### 3.4 Typography

- **Families:** Geist (variable, weights 400/500/600) and Geist Mono (400/500). Self-host with `@fontsource-variable/geist` and `@fontsource-variable/geist-mono` (imported in `main.tsx`), preferred because this is an internal tool and it removes a third-party request. Fallback if you prefer no dependency: Google Fonts `<link>` with `display=swap`. There is no CSP on the dashboard, so either works. The stacks in `@theme` degrade to system fonts.
- **Weights:** 400 body, 500 labels, buttons, nav, 600 headings, card titles, stat values. No 700.
- **Scale** (Tailwind classes after the `@theme` override above):

| Role | Class | Size / line | Weight | Notes |
|---|---|---|---|---|
| Page title (h1) | `text-2xl` | 24/32 | 600 | `tracking-tight` (-0.02em) |
| Big number, login, appeal title | `text-3xl` | 28/36 | 600 | `tabular-nums`, `tracking-tight` |
| Section heading (h2, outside cards) | `text-xl` | 20/28 | 600 | rare |
| Dialog title, lead paragraph | `text-lg` | 16/24 | 600 / 400 | |
| Card title | `text-md` | 15/22 | 600 | |
| Body, inputs, buttons, tables | `text-base` | 14/20 | 400 / 500 | default |
| Secondary, descriptions, dense tables | `text-sm` | 13/20 | 400 | `text-muted-foreground` |
| Caption, badges, table head, timestamps | `text-xs` | 12/16 | 400 / 500 / 600 | |
| Overline (nav group label) | `text-2xs` | 11/16 | 600 | uppercase, `tracking-[0.06em]`, `text-subtle-foreground` |
| Mono (IDs, paths, diagnostics) | `font-mono text-xs` | 12/16 | 400 | |

- Body copy max line length 65ch (`max-w-prose`). Headings use `text-wrap: balance`.
- Minimum text size is 11px, used only for overlines and keycaps.

### 3.5 Chart palette (colour-blind-safe)

Order matters: use series in this order, and never more than five per chart.

| Series | Light | Dark | Hue |
|---|---|---|---|
| chart-1 | `#3B5BDB` | `#8FA8FF` | blue |
| chart-2 | `#D9730D` | `#F5A64B` | orange |
| chart-3 | `#0E8F80` | `#45D0BD` | teal |
| chart-4 | `#B5389F` | `#E884D6` | magenta |
| chart-5 | `#6B7385` | `#A3ABBD` | neutral grey |

Blue vs orange is the primary pair and stays distinguishable under deuteranopia and protanopia. Rules:
- Series also differ by **line style** (solid, dashed `6 3`, dotted `1 4`) or **direct labels**, never colour alone.
- Single-series bar charts use chart-1. Highlight one bar (e.g. peak hour) with chart-2.
- Grid lines: `var(--border)`, dashed `3 4` for intermediate lines, none vertical. Axis text 11px `var(--subtle-foreground)`, no axis lines except the baseline.
- Area fill under a line: same colour at 14% opacity. Emphasise the last point with a 3.5px dot.
- Bars: 2px radius, gaps at least 30% of the bar width.
- Tooltip: a `popover` surface (`bg-popover border shadow-md rounded-md`), 12px text, value in `tabular-nums`.
- Every chart has `role="img"` and an `aria-label` that states the total and the peak (the current Honeypot chart labels are the model).
- Semantic colours (success, warning, danger) are for status, not series. The only exception is Diagnostics where latency-sensitive series use chart-2 (orange) and normal series chart-1.

### 3.6 Spacing, radii, borders, shadows

- **Spacing:** Tailwind's 4px scale. Named tiers: 4 (`gap-1`) icon+text, 8 (`gap-2`) inline clusters, 12 (`gap-3`) form rows inside cards, 16 (`gap-4`) grid and card gaps, 24 (`gap-6`) section rhythm on a page, 32 page horizontal padding on desktop, 16 on phones.
- **Card padding:** 20px (header 16px 20px, body 16px 20px 20px). Dense cards (stat tiles, diagnostics) 16px.
- **Radii:** `sm` 6 (small buttons, keycaps, inline chips), `md` 8 (buttons, inputs, selects, menu items), `lg` 12 (cards, banners), `xl` 14 (dialogs, sheets), `full` (badges, pills, avatars, switches).
- **Borders:** 1px `--border` for surfaces and dividers; 1px `--input` for controls. Cards use border + `shadow-sm`; there are no drop shadows on flat content.
- **Shadows:** `shadow-sm` on cards and secondary buttons; `shadow-md` on popovers, menus, dialogs, toasts. Dark theme uses stronger black shadows plus a 1px inner hairline.
- **Focus ring:** 2px solid `--ring`, 2px offset (`outline-2 outline-offset-2 outline-ring`) for buttons and nav items; inputs use `border-ring` + `ring-3 ring-ring/25`. Never `outline: none` without a replacement.

### 3.7 Motion

| Use | Duration | Easing |
|---|---|---|
| Hover, colour, border | 120 ms | `ease-out` |
| Popover, menu, tooltip in | 150 ms | `cubic-bezier(.16,1,.3,1)` |
| Dialog, sheet, drawer in / out | 200 / 150 ms | `cubic-bezier(.16,1,.3,1)` / `ease-in` |
| Toast in | 200 ms | `cubic-bezier(.16,1,.3,1)`, slide 8px + fade |
| Page transition (`PageTransition`) | 150 ms fade only | `ease-out` |
| New live item (honeypot catch, activity row) | 400 ms fade + 8px slide-down, then a tint that fades over 3 s | `ease-out` |
| Skeleton shimmer | 1.4 s linear loop | |
| Live dot pulse | 2 s opacity 1 to .35 | `ease-in-out` |

All of the above sit inside `@media (prefers-reduced-motion: no-preference)`. Under `reduce`: no slide, no shimmer, no pulse, fades of 0 ms. New live items still get the tint (static, then removed after 3 s) so the change is still visible.

---

## 4. Layout

| Item | Spec |
|---|---|
| Breakpoints | phone below 768, tablet 768 to 1023, desktop 1024 and up, wide 1440 and up |
| Sidebar | 248px fixed, `bg-sidebar`, 1px right border. Collapsed rail: 64px, icons only with tooltips (tablet default and user toggle, shortcut `[`). The toggle sits in the top brand row in both states: "Collapse sidebar" right of the brand when expanded, and in the rail an outlined "Expand sidebar" button replaces the "K" logo (it used to be a small icon in the footer that was easy to miss). Below 768: hidden, replaced by a top bar + drawer + bottom nav (below). |
| Header (top bar) | 56px, `bg-card`, 1px bottom border, horizontal padding 24 (16 phone). Contents left to right: breadcrumb, spacer, command palette trigger (260px, hidden below 640 and replaced by an icon), notifications (reserved, hide until it has content), profile button. |
| Content area | Scrolls under the header. Padding 28 32 40 (16 16 88 on phone to clear the bottom nav). |
| Max widths | Default pages 1200px. Form and reading pages (Settings, Templates editor, panel builder, Permissions intro) 760 to 1000. Diagnostics 1320. Public appeal form 560. All centred with `mx-auto`. |
| Grid | 12-column feel via CSS grid; common patterns: stat row `grid-cols-2 sm:grid-cols-3 xl:grid-cols-5 gap-4`; main + rail `grid-cols-[minmax(0,1fr)_340px] gap-6` (rail stacks below 1024); two-up cards `lg:grid-cols-2 gap-4`. |
| Page header | `PageHeader` component: h1 (24/32, 600), description (14/20, muted), actions right-aligned, wraps under the title on phone. Primary action is the only `primary` button in the header. |
| Breadcrumbs | `Dashboard / Section / Page` in the header (13px, muted, current page 500 foreground). Section maps to the nav group child, page is dynamic (ticket number, template name). Provide via a small `useBreadcrumbs` context set by detail pages; list pages need none beyond section. Each crumb except the last is a link. |
| Phone shell | 56px top bar (menu button 44x44, page title, search icon; no live pill since 2026-10-14, connection problems show as banners). **Bottom nav** (64px, safe-area padded): Overview, Moderation, Honeypot, Tickets, More. Items the user cannot access are omitted and the row re-flows; **More** opens the same drawer as the menu button, which shows the full grouped sidebar. Drawer 248px, scrim `--overlay`, closes on route change, Escape and scrim tap, traps focus. |
| Touch | Interactive targets at least 44x44 on phone (buttons `h-11`, list rows `min-h-12`). Tables become stacked card lists or scroll horizontally inside an `overflow-x-auto` wrapper; never scroll the page body sideways. |

---

## 5. Navigation and information architecture

### 5.1 Proposed sidebar grouping

Reasoned from the real routes and permissions in `navLinks.ts`. Today there are two groups (`operate`, `admin`) and Community tools sit among moderation tools. Proposed four groups:

| Group | Item | Route | Icon (lucide) | Required permission |
|---|---|---|---|---|
| Overview | Overview | `/dashboard` | `LayoutDashboard` | none |
| Moderation | Moderation | `/dashboard/moderation` | `ShieldAlert` | `moderate_members` |
| | Honeypot | `/dashboard/honeypot` | `Target` | `moderate_members` (manage needs `ban_members`) |
| | Tickets | `/dashboard/tickets` | `Ticket` | `manage_tickets` |
| Community | QOTD | `/dashboard/qotd` | `MessageSquareText` | `manage_qotd` |
| | Self-roles | `/dashboard/self-roles` | `Smile` | `manage_reaction_roles` |
| | Templates | `/dashboard/templates` | `FileText` | `manage_templates` |
| System (staff only) | Diagnostics | `/dashboard/diagnostics` | `Activity` | `staff_base` |
| | Permissions | `/dashboard/permissions` | `KeyRound` | `staff_base` |
| | Settings | `/dashboard/settings` | `SlidersHorizontal` | `staff_base` |

- Change `NavItem.group` to `"overview" | "moderation" | "community" | "system"` and render groups in that order. Everything else in `navLinks.ts` (the single source consumed by sidebar, palette, quick actions) stays. Groups with no visible items are not rendered (a Ticket Helper sees Overview + Moderation > Tickets only).
- **Verification** is not a dashboard route today. Reserve a slot under Moderation (`/dashboard/verification`, `manage_verification`) for when it exists; do not add a dead link.
- **Service status** (`/status`, public) moves out of the nav into the sidebar footer and profile menu as a text link, next to the live indicator.
- Badges: numeric `count` pills on Tickets (open count) and Honeypot (pending appeals). They come from data the pages already fetch (`useTickets("open")`, `useHoneypotAppeals("pending")`); if that data is not loaded because the user lacks the permission, no badge. Cap display at 99+.
- Item states: default (muted text), hover (`bg-hover`, foreground text), active (`bg-sidebar-accent`, `text-sidebar-accent-foreground`, `aria-current="page"`, no left border rail), keyboard focus (2px ring, inset), and a badge. There is no "disabled" state; inaccessible items are absent.
- Sidebar footer: the **Live** row (dot + "Live" + ping in ms) and the **Bot** row (icon + "Bot online" / "Bot offline" + "Status" link). These replace `LiveIndicator` and `StatusIndicator` at the bottom of the sidebar and keep their existing data sources (`useLive()`, `useStatus`).
- Brand block at the top: logo tile, "Kick-Bot", guild name in caption text, collapse button.

### 5.2 Command palette (Ctrl/Cmd+K)

- Trigger: header search button (shows `Ctrl K`, `Cmd K` on macOS), the phone search icon, and the shortcut anywhere except inside a text field that handles it.
- Contents grouped: **Pages** (from `NAV_LINKS`, permission-filtered), **Settings tabs** (deep links `?tab=`), **Actions** (from `commands.ts` plus Quick Actions: Warn a user, Generate and post QOTD, New template, New ticket panel, Grant permission, Switch theme), **Recent** (last 5 visited pages in localStorage).
- Fuzzy match on label and keywords; results keep the group order; first result auto-selected; Enter opens, Up/Down navigate, Esc closes, focus returns to the trigger. Footer hint row: `Up Down navigate, Enter open, Esc close`.
- Only pages the user can access appear.
- Implementation: add shadcn `command` (cmdk) inside the existing dialog, or restyle the existing `CommandPalette.tsx`. Keep the item-source contract.

### 5.3 Keyboard shortcuts

| Key | Action |
|---|---|
| `Ctrl/Cmd K` | Command palette |
| `?` | Shortcuts dialog |
| `/` | Focus the page's search field (if any) |
| `[` | Collapse/expand sidebar |
| `g` then `o` / `m` / `h` / `t` | Go to Overview / Moderation / Honeypot / Tickets |
| `Esc` | Close dialog, menu, drawer, palette |
| Tables | `j` / `k` move row focus, `Enter` open (Tickets, Moderation recent list) |

Shortcuts are ignored while focus is in an input, textarea, select or contenteditable, and while a dialog is open. Implement with one `useHotkeys` hook mounted in `AppShell`. Shortcuts never trigger destructive actions.

---

## 6. Component specs

Build on the existing shadcn primitives in `src/components/ui/*`. Names in code font are the file to change or add.

### 6.1 Buttons (`button.tsx`)

| Variant | Use | Light | Dark |
|---|---|---|---|
| `default` (primary) | The single main action per view | bg primary, text primary-foreground, hover primary-hover | same tokens |
| `secondary` | Everything else important | bg card, 1px `--input` border, `shadow-sm`, hover `bg-hover` | same |
| `ghost` | Toolbar, row actions, nav helpers | transparent, muted text, hover `bg-accent` | same |
| `destructive` | Confirming a destructive action (inside a confirm dialog, or Ban on Moderation) | bg destructive, white text | same |
| `outline-destructive` (new, or `destructive-outline`) | Destructive entry points that open a confirm (Kick, Delete ticket, Deny) | transparent, 1px `--danger` border and text | same |
| `link` | Inline navigation ("View all") | primary text, medium weight, underline on hover | same |

Sizes: `xs` h-6 (dense table rows only), `sm` h-[30px] px-2.5 text-[13px] radius 6, `default` h-9 (36) px-3.5 text-sm/base radius 8, `lg` h-11 (44) px-5 text-[15px] (login, appeal submit, phone primary actions), `icon` 36x36, `icon-sm` 30x30. Gap 6 between icon and label; icons 16px (14 in sm).
States: hover (background step), active (`translate-y-px`), focus-visible (2px ring, 2px offset), disabled (opacity .45, `cursor-not-allowed`, and a title or adjacent hint saying why when it is a temporary condition), loading (leading 14px spinner replacing the icon, label changes to a present participle: "Saving", "Banning", `aria-busy`, width stays fixed).

### 6.2 Form fields (`input.tsx`, `textarea.tsx`, `select.tsx`, `label.tsx`, new `field.tsx`)

- New `Field` wrapper: label (13/20, 500) above, control, hint (12/16 muted) below, error (12/16 danger with a 12px warning icon) replacing the hint. Wires `id`, `aria-describedby`, `aria-invalid`.
- Control: h-9, radius 8, `bg-card`, 1px `--input` border, padding 0 12px, `text-base`. Small: h-[30px], radius 6. Placeholder `text-subtle-foreground`. Focus: `border-ring` + `ring-3 ring-ring/25`. Error: `border-danger` (+ `aria-invalid`). Disabled: `bg-muted text-subtle-foreground`, no pointer.
- Textarea: min-h 88, vertical resize, character counter below right (`0 / 1500`, `tabular-nums`, turns `text-warning` at 90 percent, `text-danger` at limit).
- Select: keep native `<select>` styled to match (or move to Radix Select if grouping/search is needed); chevron icon right, same height.
- Switch (`switch.tsx`): 36x20, off track `--input` colour, on track `--primary`, 16px thumb (white light, `primary-foreground` dark). Always paired with a visible label and description; `role="switch"`.
- Checkbox (new `checkbox.tsx`, Radix): 16x16, radius 4, 1.5px `--input` border, checked = primary fill + check icon. Used in the Permissions matrix.
- Segmented control (new `segmented.tsx`, Radix ToggleGroup, `type="single"`): muted track, active segment `bg-card shadow-sm`. Used for density, theme, refresh interval, feed filters.
- **Validation:** validate on blur and on submit, revalidate on change once invalid. Message tells the fix. Submit button is disabled only while a required field is empty; format errors are shown and block submit. Server errors map to the field when the API returns one, otherwise an error toast whose text comes from `describeApiError(error, fallback)` (`src/lib/apiError.ts`; no per-page `formatApiError` copies). Unsaved forms (Settings, Template editor, Honeypot settings, QOTD schedule) show a sticky save bar with "N unsaved changes", Discard and Save; leaving with unsaved edits asks to confirm (see 6.14).
- **Discord ID fields** (Settings IDs, Quick Action "warn a user", honeypot log channel): mono font, digits only, 17 to 20 digits with the message "A Discord ID is 17 to 20 digits. This one has N." The public appeal keeps its existing 15 to 22 digit rule and message.

### 6.3 Badges (`badge.tsx`)

Pill, h-[22px], px-2, 12/16, weight 500. Variants: `default` (muted), `success`, `warning`, `destructive` (danger), `info`, `accent` (primary-soft), `outline` (transparent, 1px border, muted text). Coloured variants use `*-soft` background and the matching text token, with an optional 8px status dot or 12px icon so meaning never depends on colour. Count badge: min-w-5 h-5, primary-soft, 600.

Mapping used across pages: ticket Open = success, Closed = default, Archived = danger; appeal invited = info, pending = warning, approved = success, denied = danger; honeypot action = outline; permission "Staff only" = accent with lock icon; "Restart"/"Live" setting notes = warning/success.

### 6.4 Tables (`table.tsx`, plus new `data-table` helpers)

- Container: card with `overflow-x-auto`. Head row: 12/16, 600, `text-muted-foreground`, `bg-card`, 1px bottom border, `whitespace-nowrap`, padding 10px 16px. Body rows: 1px bottom border, hover `bg-hover`, padding 12px 16px (comfortable) or 7px 16px with 13px text (compact). Last row no border. Numeric columns right-aligned with `tabular-nums`. IDs in `font-mono text-xs`.
- **Sort:** clickable header buttons with a 12px chevron (down = descending) and `aria-sort`; default sort stated in the design (Tickets: number descending). Reuse the sort logic already in Diagnostics tables.
- **Filter:** a filter bar above the table inside the card (search input `sm`, filter chips or tabs, density toggle). It is `position: sticky` under the header when the table is long.
- **Pagination:** footer bar with "N of M", Previous/Next `secondary sm`; page size 25 by default. For live lists (Tickets, warnings) prefer "Show more" appending rows, so live inserts do not jump pages.
- **Density:** Comfortable / Compact segmented control, stored in `localStorage["kb-density"]`, applied to all tables app-wide (`data-density` on `<html>`, table classes read it). Diagnostics defaults to Compact.
- **Empty:** table body replaced by the empty state (6.9). **Loading:** 5 skeleton rows with the real column widths. **Error:** inline error state with Retry.
- Phone: turn into a stacked list of rows (avatar/ID + primary text + badge + meta) under 640px, or wrap in horizontal scroll for wide diagnostic tables.
- Row actions: ghost buttons or a `...` menu (`dropdown-menu`), never more than two inline buttons per row.

### 6.5 Cards and stat cards (`card.tsx`)

- Card: `bg-card`, 1px border, radius 12, `shadow-sm`. Optional header (16px 20px 0, title `text-md` 600, description 13/20 muted, actions right) with `border-b` variant when the body is a list or table. Optional footer (12px 20px, top border) for pagers and hints. `interactive` cards get `hover:border-input` and a focus ring; nothing else lifts or scales.
- **Stat card:** padding 16 20, label 13 muted (with a status dot for presence stats), value `text-3xl` 600 `tabular-nums`, delta 12 muted with an arrow icon and text ("+6 today"). No coloured card backgrounds, no left border rails. Presence stats use dot colours success/warning/danger/subtle for Online/Idle/Do not disturb/Offline and always print the label.
- Live value change: a 3 s arrow indicator (as today) plus a 200 ms number cross-fade. Skeleton: label bar + 32px value bar.
- Presence unavailable state: one wide card "Presence breakdown unavailable right now." (as today).

### 6.6 Dialogs, sheets, menus, popovers

- **Dialog** (`dialog.tsx`): radius 14, `bg-card`, `shadow-md`, `--overlay` scrim, max-w 480 (confirm) / 560 (form) / 880 (stats). Header (title 16/24 600, description 13/20 muted), body, footer right-aligned with Cancel (`secondary`) then the action. Close button top right for large dialogs. Focus trapped, Esc closes, initial focus on the first field or the safest button (Cancel for destructive confirms).
- **Confirm-destructive pattern** (`alert-dialog.tsx`): title names the object ("Ban pixelmoth?"), body states the consequence and that it is logged, optional reason field, footer Cancel + destructive button with icon ("Ban member"). For the highest-impact actions (delete ticket, grant `manage_permissions`) require typing the ticket number or the word GRANT. Never a bare "Are you sure?".
- **Sheet / drawer** (new `sheet.tsx`, Radix Dialog): mobile navigation drawer (left, 248px) and, optionally, detail panels on phone. Same scrim, 200 ms slide.
- **Dropdown menu** (new `dropdown-menu.tsx`): profile menu, row `...` menus, Timeout duration picker. Item h-8, radius 6, icon 16 + label + optional keycap; danger items in `text-danger`; separators 1px border.
- **Tooltip** (new `tooltip.tsx`): 12px, `bg-foreground text-background`, 500 ms delay, used for icon-only buttons and collapsed sidebar labels. Not for essential information.
- **Popover** (new `popover.tsx`): filters, colour picker.

### 6.7 Toasts (`toast.tsx`)

- Bottom-right stack on desktop (340px, `bg-card`, border, `shadow-md`, radius 10), bottom-centre full width minus 16 on phone above the bottom nav.
- Anatomy: 20px status icon, title (13/20, 600), optional description (12/16 muted), optional action (`link` button, e.g. **Undo**), close button. Variants `default`, `success`, `destructive` (rename `destructive` display to error styling with danger icon), add `info`.
- Duration: 4 s; 8 s when it has an action; errors persist until dismissed. `role="status"` (success, info) or `role="alert"` (error). Pausing on hover and focus. Max 3 visible.
- Extend `useToast().toast({ title, description, variant, action?: { label, onClick } })`; existing calls keep working.

### 6.8 Tabs (`tabs.tsx`)

Underline tabs: 14/20 500, muted text, active foreground with a 2px primary underline inset 8px, 1px bottom rule across the width. Count chips inside tabs (`Open 4`, `Appeals 2`). Roving tabindex with arrow keys (Radix). Sticky under the page header on long pages (Diagnostics section nav). Tab choice persisted per page (`?tab=` where it already exists, otherwise localStorage). On phone, tabs scroll horizontally with a fade edge.

### 6.9 Empty states, skeletons, banners

- **Empty state:** 44px rounded-12 tile with a 20px icon (`bg-muted`), title 15/22 600, one sentence 13/20 muted (max 280px wide), optional single action button. Centred with 40px vertical padding. Every list has one, with copy specific to the list ("No open tickets", "Nothing caught yet. Catches show up here live.").
- **Skeleton** (`skeleton.tsx`): `bg-muted` with a soft moving highlight (`linear-gradient(90deg, muted, hover, muted)`), radius 6. Compose skeletons that match the final layout (stat row, list rows with avatar + two lines, table rows). `RouteFallback` becomes a page-header skeleton + a card skeleton.
- **Error state:** danger-soft tile, title "Could not load X", one sentence with the reason (status code when useful), **Try again** button, and a link to `/status` for 502/network errors.
- **Banner** (new `alert.tsx`): radius 10, padding 12/16, soft background + matching text, 16px icon, optional action right. Variants: info, success, warning, danger. Used for offline, bot offline, staff-role notices.

### 6.10 Live indicator and offline states

`LiveIndicator` and the header pill share one source of truth (`useLive()`); states map as follows. **Current state (2026-10-14):** the header pill was removed at the owner's request (`LivePill` no longer exists); the sidebar footer (`SidebarLiveRows`, dot + label) is the only always-visible indicator, and the banners below still announce bot-offline / offline / back-online. The "Pill" column below now describes the sidebar dot colour.

| Live state | Pill | Text | Extra behaviour |
|---|---|---|---|
| `connecting` | neutral, grey dot | Connecting | none |
| `live`, bot reachable | success-soft, pulsing dot | Live | none |
| `live`, `bot.online === false` | danger-soft, static dot | Bot offline | Red banner at top of content: "The bot is offline. Actions that need Discord are paused until it is back." Buttons that hit the bot (Warn, Timeout, Kick, Ban, Send, Post) become disabled with the reason in `title`/hint. Read-only data keeps showing |
| `reconnecting` | warning-soft, pulsing dot | Reconnecting | Data stays; no banner unless it lasts more than 10 s |
| `offline` | danger-soft, wifi-off icon | Offline, polling | Warning banner: "You are offline. Showing data from HH:MM. Retrying." with Retry now. Pages already fall back to polling; show a "last updated" caption on live cards. When it recovers, a green "Back online" banner shows for 3 s |

**In-page live caption** (`LiveCaption` in `components/patterns/LivePill.tsx`): the "Updates live" / "New catches appear here live" text beside live lists. Pulsing success dot + live text only while `useLive().connected`; grey "Connecting to live updates" while connecting; otherwise a static warning dot and the page's fallback text saying how it refreshes meanwhile ("Live updates paused. Refreshing every 30 s."). Every live list uses it (Tickets, Honeypot catches); never hard-code a green "live" dot.

Pill text is 12/16 500, height 26, padding 0 10, radius full. Never rely on the dot colour alone (the label is always present). Do not add or change any live-topic subscriptions to build this; consume `useLive()` as `LiveIndicator` does today.

### 6.11 Embed-style catch cards (`CatchCard.tsx`, `EmbedPreview.tsx`)

A Discord-like embed, deliberately styled as Discord (so moderators recognise it) but themed:
- Container: `bg-embed text-embed-foreground`, radius 6, 4px left colour bar, max-width 520 (full width in the feed column), padding 10 14 12.
- Colour bar by action result (keep current mapping): ok `#ed4245`, partial `#f0b232`, failed `#7b8291`. This bar is the one place a literal hex is allowed, because it mirrors what Discord shows. The meaning is also stated in text ("Ban (ok)").
- Content order: author row (24px avatar + display name 600 13px), title "Honeypot catch" (600), message text 14/20 (or "(no text content)"), fields grid 3 columns (labels 12 600, values 13 muted-embed): User + ID, Action (result), DM sent, Account age, Joined to caught, Signals, and an **Errors** row full width when present, footer 11px "Caught today at 18:03".
- Below the card, outside it: relative time, and an appeal badge (invited info, pending warning, approved success, denied danger).
- New live catch: 2px `--primary` outline with 6px offset, fade+slide-in, tint removed after 3 s (reduced motion: outline only). `aria-live="polite"` on the feed container announces "New catch: username".
- Fields collapse to 1 column below 480px.

### 6.14 Unsaved-changes guard and not-found page

- **One hook:** `useUnsavedChangesGuard(dirty, message?)` (`src/hooks/useUnsavedChangesGuard.ts`). While `dirty`, leaving the page asks first, whatever the route: sidebar/breadcrumb/bottom-nav/in-page links (capture-phase click interceptor), the command palette and `g` hotkeys (they call `guardedNavigate(to, go)`), browser Back/Forward (capture-phase `popstate` listener that runs before React Router's, undoes the move via `history.state.idx` and replays it on confirm), reload/tab close/external URLs (`beforeunload`, the browser's own prompt). In-app cases show ONE styled dialog ("Leave without saving?", Cancel focused, "Leave page"), `UnsavedChangesDialog`, mounted once in `AppShell`. Only a pathname change is guarded (`?tab=` / `#hash` keep the page mounted). `SaveBar` calls the hook itself (`guard={false}` opts out); Template editor, ticket panel builder and Settings also call it. Never write a page-local `beforeunload`/click guard.
- **Not found:** `NotFound404` is the catch-all for unknown `/dashboard/*` paths (in the shell, "Back to Overview") and, with `standalone`, for any other unknown URL (no shell, "Back to home"). Hidden owner-only routes wrap in `OwnerOnlyRoute`, which renders the identical page for non-owners (same "Dashboard / Not found" breadcrumb; owner-only nav items are hidden from the breadcrumb for non-owners).

### 6.12 Charts (Recharts)

Follow 3.5. Wrap in a `ChartCard` (title 13 600, optional legend right, chart area, optional caption). Sparkline (Diagnostics): 48px tall, area + line + end dot, no axes, value right-aligned in the card header. Bar chart: 150 to 200px tall. Tooltip as described. Colours only via `var(--chart-n)`. Respect reduced motion (`isAnimationActive={false}`).

### 6.13 Avatars, keycaps, copy, misc

- Avatar: circle 24/28/40/56, image if available else initials on `primary-soft` (rotate through info/success/warning/danger soft tokens by hash of the ID for lists).
- Keycap (`Kbd`): mono 11/16, 1px border with 2px bottom border, radius 4, `bg-card`.
- Copy button (`copy-button.tsx`): ghost icon button, swaps to a check for 1.5 s, announces "Copied" via `aria-live`. Used on every Discord ID.
- ResolvedUser/Channel/Role components: render name (500) with the raw ID in mono muted on hover/focus tooltip or beside it in dense contexts; loading state is a 16-wide skeleton, failure falls back to the raw ID.

---

## 7. Page-by-page spec

Common frame for every `/dashboard/*` page: `AppShell` (sidebar with live status in its footer, header without a live pill), then `PageHeader`, then content in the max width given. Every data page has four states: loading (skeleton matching layout), empty (specific empty state), error (error state with retry), forbidden (existing `Forbidden403` inline branch, rendered **inside** the shell so the nav stays visible). The offline and bot-offline states in 6.10 apply everywhere. Phone behaviour is stated per page.

### `/` Home / sign in (public, no shell)
- Centred column on `bg-background`: 48px logo tile, h1 "Kick-Bot Dashboard" (28/36), description, a **Continue with Discord** button (`lg`, `#5865F2` background with white text, 4.6:1, Discord icon, real `<a href="/api/auth/login">` top-level navigation), a caption "Only members of Gamer Girls HQ can sign in." and the "Seeing an error? View API error codes" link to `/status`.
- Authenticated users are redirected to `/dashboard` (unchanged).
- States: loading auth (skeleton of the same layout, no flash of the button), sign-in error via query string (danger banner above the button). Phone: same, full-width button.

### `/status` (public status and error codes)
- Standalone page, `max-w-3xl`, simple top bar (logo + "Service status" + Back link). Top: overall status banner (success/danger) from `useStatus`, service rows (Dashboard, Bot API) with status badges and last-checked time. Below: error-code reference as a table (Code, Name, Meaning, How to fix) with codes in mono; anchor links per code; sticky filter box to search codes. Copy stays as is.
- States: loading skeleton rows; error banner "Could not check status" (uses the network error row as guidance). Phone: table becomes stacked rows.

### `/appeal` and `/appeal/:token` (public, standalone, no shell)
- Layout: top bar (logo tile + "Gamer Girls HQ" left, "Ban appeal" right), centred 560px column, page bg `--background`, form on a card with 24px padding. Title 28/36, intro paragraph (existing copy) 15/24 muted.
- Fields (existing): **Name** (required, max 100, hint), **Discord User ID** (15 to 22 digits, mono, error "A User ID is 15 to 22 digits, numbers only." shown once the field has content and is invalid; read-only and pre-filled when the token identifies the account), **What happened?** (textarea, max 1500, counter). Submit **Submit appeal** (`lg`, disabled until valid, loading label "Submitting"). Hint beside it: "A moderator reviews every appeal."
- **"How do I find my User ID?"** helper: a `<details>` disclosure directly under the User ID field (same steps as today, computer and mobile). Open by default on the manual (no token) form when the ID is empty or invalid; closed when the ID is pre-filled. Two columns on desktop, single column on phone with phone steps first. Include an example row with a dummy ID labelled "Example, yours is different". Summary row is 48px tall with a rotating chevron.
- States: token lookup loading (three skeletons, `aria-busy`), **Appealing as X** info notice when identified by link, invalid/expired link (danger banner + link to the manual form), already submitted, rate-limited/server error (danger banner with existing copy), success (success banner, existing copy, form hidden).
- Phone (390): 16px gutter, 44px inputs, 48px full-width submit, top bar 56px. Dark mode follows `prefers-color-scheme` only (no toggle, no storage read is required; reading `kb-theme` is optional and must not throw).
- Must not import `AppShell`, `useAuth` protected calls, or `LiveProvider` consumers. Keep the page a lazy chunk.

### `/dashboard` Overview
- Header: "Overview", "Live activity across Gamer Girls HQ.", actions: **Post QOTD** (secondary, opens the existing generate-and-post flow) and **Warn a user** (primary, opens the existing Quick Action dialog). Actions appear only with the matching permission.
- Row 1: stat cards (Total members, Online, Idle, Do not disturb, Offline) from `useDashboardStats` with the trend indicator.
- Row 2 (`2fr / 1fr`): **Live activity** card (mixed feed of recent warnings, opened tickets, honeypot catches, unbans; filter segmented control All / Moderation / Tickets / Honeypot; newest row tinted 3 s; rows show avatar, sentence, one-line reason, status badge, relative time), and a right column with **Quick actions** (the existing list: Warn a user, Generate and post QOTD, Open tickets, New ticket panel, New template, Grant permission; each permission-filtered, each a full-width ghost row with chevron) and **Bot status** (status badge, gateway latency, uptime, dashboard link, link to Diagnostics for staff).
- Row 3: **Recent warnings** and **Open tickets** cards (top 5 to 3 rows each, "View all" links), shown only with `moderate_members` / `manage_tickets`.
- Data note: the mixed activity feed is built from data sources that already exist (`useRecentWarnings`, `useTickets`, honeypot catches, `moderation`/`tickets`/`honeypot` live topics); do not add endpoints. If a source is not permitted for the user, that event type is omitted. If only one source exists the card keeps its title and hides the filter.
- Empty: "No recent activity" with description. Error per card. Loading: stat skeletons + list skeletons. Phone: stat cards 2x2 (Total spans two), full-width **Warn a user** button below stats, feed as a card, bottom nav.

### `/dashboard/moderation`
- Header: "Moderation", "Look up a member, review their history, and take action." No primary action (the search is the primary interaction; focus it with `/`).
- Layout: left column 340px: **Find a member** card (search input, live results menu below: avatar, username, display name, warning-count badge; matches as you type after 2 characters, Enter picks the first) and **Recently warned** card (compact list, live, with a filter field "Filter by reason, moderator or ID"). Right column: **member header card** (avatar, username, display name, copyable ID, joined date, role badges, current timeout badge, warning count), which also carries the **action bar** (reason input applying to the action, buttons: Warn (primary), Timeout with duration menu, Remove timeout, Kick (outline destructive), Ban (destructive), Unban when banned). Under it a tabbed card: **Warnings** (table: When, Reason, Moderator, Remove) and **Mod actions** (table: When, Action badge, Reason, Moderator).
- Behaviour (updated 2026-10-14): Warn, Timeout, Kick and Ban open the confirm dialog with the reason prefilled from the field (Ban also has a "Delete their recent messages" select, 0-7 days, default "Don't delete any"); Warn and Timeout still show an **Undo toast** after success. Remove timeout and Unban apply immediately. Remove warning is confirmed (destructive history) and sends the member's id so only their warning can be removed. The reason (max 512 characters) is cleared, and any open confirm closed, whenever a different member is selected. Custom timeouts are 1 to 40320 minutes (28 days). Enter in the search box only acts on results for the text currently in the box: if the debounced results are stale it flushes the search and picks the first match when those results arrive. A rank/identity refusal from the bot (403 `hierarchy_blocked` / `actor_not_member`) shows as a danger `Alert` on the action card until another member is picked, besides the toast; all errors go through `describeApiError`. "Updates live" on Recently warned shows only while the socket is connected; the warnings/history/recent queries poll every 30 s only while it is down, and `useModerationLive` subscribes once the recent-warnings query has data. Forbidden: a `forbidden` 403 on any of the page's queries renders `Forbidden403` inline. Actions are not pre-disabled by rank (the API returns no rank data for the viewer); the server check is what counts.
- Before a member is selected the right column shows an empty state "Search for a member to see their history" with the recently warned list as the way in.
- Empty history: "No warnings" / "No mod actions yet". Loading: header skeleton + table skeleton. Error: inline error with retry. Forbidden: `Forbidden403`. Bot offline: action bar disabled with the bot-offline reason. Phone: single column in order Find, member header, actions (buttons wrap 2 per row, 44px), tabs, Recently warned last.

### `/dashboard/honeypot`
- Needs `moderate_members` to view; `ban_members` to change anything (read-only otherwise: controls disabled with a "Only members with Ban Members can change this" hint).
- Header: "Honeypot", "A trap channel that catches hacked accounts and spam bots.", action **View stats** (secondary, opens the stats dialog) only when configured.
- **Not configured:** two columns: **Create honeypot** card (Category name, Channel name, Action on catch select with a hint explaining Ban/Softban/Kick, DM the user first switch, Allow appeals switch disabled unless action is Ban, footer "Needs the Ban Members permission" + **Create honeypot**) and a **How it works** card (three numbered steps: real sequence). Users without `ban_members` see the same page with a read-only notice and no create button.
- **Configured:** status card (target icon tile, "Honeypot is active"/"Honeypot is paused" with badge, action badge, trap channel and log channel chips, **Enabled** switch that updates optimistically and rolls back with an error toast; warns in a warning banner if `channel_exists` is false). Then two columns (rail stacks under 1024):
  - Main: tabs **Catches**, **Appeals (n)**, **Settings**. Catches = feed of embed cards (6.11), live, "New catches appear here live" caption with pulsing dot. Appeals tab = full queue with the status filter select. Settings tab = the settings form (Action on catch, DM the user before acting, Include appeal link in ban DMs, Delete message history 0 to 7 days, Log channel ID optional) with the sticky save bar.
  - Rail (visible on the Catches tab, hidden on others): **At a glance** (24 hours, 7 days, 30 days, 14-day bar sparkline, "Full stats" link) and **Pending appeals** (up to 3 with quote of the member's message, optional note input, **Approve and unban** primary and **Deny** outline-destructive; Approve opens the existing confirm dialog. Neither offers Undo, because approving or denying messages the member).
- **Stats dialog** (880px): four totals row, by-action tiles (Bans, Softbans, Kicks, Fully applied), two charts side by side (catches per day 30 days, catches by hour UTC with the peak bar in chart-2), three columns: Account age, Time to catch, Signals (progress bars in chart-1 with percentages). Data as today.
- States: config loading skeleton (status card + tab strip + two catch skeletons at 520px); feed empty "Nothing caught yet"; error inline; forbidden 403. Phone: status card wraps, tabs scroll, rail cards move under tabs as the first tab content ("Overview" is not added; the At a glance card sits above the feed), catch fields 1 column.

### `/dashboard/tickets`
- Header: "Tickets", "Support and verification tickets opened from your panels.", actions **Manage panels** (secondary, scrolls to the Panels card) and **New ticket panel** (primary, links to `/dashboard/tickets/panels/new`).
- Card with tabs **Open (n)**, **Closed**, **Archived**, **All** (the same status filter as today), a sticky filter row (search by number, opener or purpose; "Updates live" caption; density segmented) and a table: Ticket (mono, sortable), Opened by (avatar + name), Purpose, Status badge, Claimed by (or "Unclaimed" muted), Opened (relative, right-aligned). Row click opens the detail; whole row is a link (`<a>` on the number cell plus row click, keyboard reachable).
- Second card **Panels** listing existing panels if the current API exposes them; if it does not, omit the card (do not add endpoints).
- Empty per tab ("No open tickets", "No closed tickets"). Loading: 5 skeleton rows. Live inserts tint for 3 s. Phone: rows become cards (`#0142 sunny.day`, purpose, badge, time).

### `/dashboard/tickets/:ticketId`
- Breadcrumb `Tickets / #0141`. Header: title "Ticket #0141" + status badge, description "Purpose, opened by X, N minutes ago", actions **Claim** (secondary), **Close ticket** (secondary), `...` menu (Archive, Delete).
- Two columns: **Chat log** card (message list with avatar, name, Staff badge, time, attachment chips; search field; "Captured" badge) and a right rail with **Details** (opened by, panel, purpose, claimed by, opened, channel as mono chip) and **Actions** (Claim, Close, Archive, Delete in the danger zone separated by a rule with the sentence about permanence).
- Delete uses the type-to-confirm dialog (type the ticket number). Close/Claim are immediate with Undo toast. Not found: empty-style error "Ticket not found" with a link back. Loading: header + two card skeletons. Phone: single column, chat log first, actions as full-width buttons.

### `/dashboard/tickets/panels/new`
- Header: "New ticket panel", secondary **Back to tickets**. Two columns (form 1fr, preview 440px sticky, stacks on phone): **Panel configuration** card grouped by sub-headings **Where** (Channel required, Open category, Closed category, Transcript channel), **Behaviour** (Max open tickets per user, Purpose, Naming pattern with a live example "ticket-0142"), **Button** (Template optional, Button label default "Open Ticket", Button emoji). Right: **Message preview** card using the embed/message preview component; the panel button rendered as a blurple Discord-style button.
- Footer bar (sticky): Cancel, **Create panel** (disabled until Channel chosen; loading "Creating"). Success toast with link "View panel". Field hints say what "optional" defaults to.

### `/dashboard/self-roles`
- Header: "Self-roles", "Role panels members can pick from.", primary **New panel** (opens the create form in a dialog, replacing the inline toggle-open card).
- Body: search field, then a list of panel cards, each with title (inline editable: pencil to edit, Save/Cancel), channel chip, entry list (emoji, button label, role chip via `ResolvedRole`, remove icon button with confirm-less Undo toast), **Add role** (inline row: emoji, label, role select), footer actions **Post to channel / Update message** and **Delete panel** (confirm). Collapsible cards, first one expanded.
- Empty: "No self-role panels yet" + **New panel**. Error/loading/forbidden as standard. Phone: entry rows wrap, buttons full width.

### `/dashboard/templates`
- Header: "Templates" (page title was "Message Templates"; keep visible title "Message templates"), "Reusable messages and embeds used by tickets, events and schedules.", primary **New template** (opens a small dialog: Name with the "letters, digits, - and _" rule and inline validation, Category optional; **Create and edit** goes to the editor).
- Card **All templates** with a category tab strip (All, each category, Uncategorized), search, and a table: Name (mono link), Category badge, Version, Updated, row `...` menu (Edit, Delete with confirm). Sortable by name/updated.
- Empty: "No templates yet". Phone: list rows.

### `/dashboard/templates/:templateId`
- Breadcrumb `Templates / name`. Header: template name, "Unsaved changes" warning badge when dirty, category and version in the description, actions **Discard** (ghost) and **Save changes** (primary, disabled when clean, loading "Saving").
- Two columns (`1fr / 440px`, preview sticky, stacks on phone with the preview after the form): left **Details** (Name, Category, Message text with placeholder chips `{user}`, `{server}`, `{member_count}` that insert on click) and **Embeds** (count badge "1 of 10", **Add embed**; each embed is a collapsible group: Title, Description, Colour swatches + hex input, Fields list (Field name, Field value, Inline checkbox, move up/down, remove), Footer, Remove embed). Right: **Preview** card rendering the message like Discord (bot avatar, APP badge, text with placeholders resolved to sample values, embed via `EmbedPreview`) that updates as you type, and **Send to channel** card (channel select, **Send test message**, hint that it sends the last saved version).
- Unsaved-change guard on navigation. Error on save: toast plus field errors. Loading: two-column skeleton. Not found: error state.

### `/dashboard/qotd`
- Header: "Question of the Day", "Generate, schedule and review daily questions.", primary **Generate question** (jumps to Generate tab and runs it when the tab is open).
- Tabs (existing): **Generate & Post**, **Schedule**, **History**, **Fallback pool**.
  - Generate & Post: card with generate options, result card showing the question in a quote block with **Regenerate** (secondary) and **Post to channel** (primary, needs channel; success toast "Question of the Day posted" with Undo not offered).
  - Schedule: **Daily schedule** card (enabled switch, time, timezone, channel, mode) with sticky save bar.
  - History: table (Date, Question, Source badge AI/Fallback, Posted by, Status).
  - Fallback pool: list with search, **Show all/Show fewer**, add/remove.
- Empty/loading/error per tab; Ollama-unavailable warning banner on Generate. Phone: tabs scroll.

### `/dashboard/permissions` (staff only)
- Header: "Permissions" + "Staff only" accent badge with lock icon, description "Choose what each Discord role can do in the dashboard and the bot.", primary **Grant permission** (opens a dialog: pick role and permission; same underlying `useAddGrant`).
- Info banner: default staff roles always have every permission and appear as non-editable rows.
- Card **Role permission matrix**: role column (colour dot from the role, name, "Default staff" badge on default staff roles) + one column per permission using the existing short labels (Moderate, Warnings, Tickets, Templates, Self-roles, Verification, Audit log, Grants, plus any new `VALID_PERMISSIONS` entry rendered by raw string). Cells are checkboxes (checked shows a check icon). Toggle saves immediately (optimistic, rollback on error) with a toast; granting **Grants** (`manage_permissions`) or any permission to a non-default-staff role that has none opens the confirm dialog (existing behaviour). Column headers have tooltips with the full permission string. Sticky first column and header row; horizontal scroll inside the card on small screens.
- Empty: "No roles found". Loading: matrix skeleton. Forbidden: 403. Phone: horizontal scroll with sticky role column, or a per-role accordion listing toggles (preferred under 640px).

### `/dashboard/settings` (staff only)
- Header: "Settings" + "Staff only" badge, description "Bot behaviour you can change without touching the server files."
- Layout: left vertical group list (Verification, Logging, Tickets, Music, Permissions [view-only, lock icon]) 200px, right the active group card (title, description, fields). `?tab=` continues to drive the selection (the palette deep-links). On phone the group list becomes a horizontal tab strip.
- Fields render from the schema (existing `SettingFieldMeta`): text/ID inputs (mono for IDs, with format validation), switches, number inputs, selects. Each field shows a **Live** (success) or **Restart** (warning) note per the schema `live` flag. `owner_id` stays read-only with a lock icon and "View only" hint.
- Sticky save bar at the bottom of the content: "N unsaved changes", Discard, **Save changes** (disabled when clean). Successful save toast "Settings saved". Server-side field errors map to the field.
- Loading: card skeleton with field skeletons. Error and 403 as standard.

### `/dashboard/diagnostics` (staff only)
- Header: "Diagnostics" + "Staff only" badge, description "Health, resource use and internals of the bot and dashboard processes.", right side: search field (`/` focuses), "Updated Ns ago" with pulsing dot, **refresh interval segmented control** (Paused, 5s, 15s; live topic still pushes about every 2 s, this controls the polling fallback and freeze) and **Refresh now**. **As built (2026-10-14):** the header has no actions; the "Updated ... (live)" text and the Pause/Resume, Refresh now, Copy JSON and Download buttons were removed at the owner's request (the page always follows live pushes). Search and Collapse all / Expand all live in the sticky nav; the error state keeps "Try again".
- **Logs:** one panel per log file (bot, dashboard, tunnel, supervisor, pot server, kicks, bot startup), each with its own minimize/expand chevron button in its header row (`aria-expanded` + `aria-controls`, label includes the log name; collapsed set remembered per viewer in `localStorage["kb-diag-logs-collapsed"]`, try/catch). A collapsed panel keeps its header row: label, mono file name, size and age, warn/error badges. Open tails auto-scroll to the newest line unless the viewer has scrolled up.
- Sticky section nav (existing sections: Overview, Live charts, Process, Children, System, Discord, Database, Services, Cookies, Config, Logs, Activity, Dashboard) as underline tabs with scroll-spy (`IntersectionObserver`, unchanged logic). Each section is a titled block; `Section` keeps its error handling ("Section missing from the response").
- **Overview:** health summary badges ("1 warning", "9 healthy"), a two-column checklist card (icon + label + detail + level badge; ok check, warn triangle, bad x-circle, na help-circle; "unknown" when the source section failed, never a false green; rows link to their section), and a six-tile key metrics row (PID, Uptime, CPU, RSS, Loop lag with tone, Asyncio tasks).
- **Live charts:** 4-column grid of sparkline cards (Bot CPU, Bot RAM (RSS), Event-loop lag, Gateway latency, Bot threads, Bot handles, Net upload, Net download, Dashboard CPU, Dashboard RAM); latency-sensitive series in chart-2, others chart-1; latest value in the card header; `aria-label` per chart.
- **Tables** (threads, tasks, open files, sockets, children, GC types, and so on): compact density default, sortable headers with `aria-sort`, filter box, "Show N of M" pager with Show all, **Copy CSV**. Mono for ids and paths. Long command lines truncate with a copy button and expand on click.
- **Search results** (existing): card listing path (mono, primary) and value (mono), grouped by section.
- States: loading skeletons per section; a failed section shows an inline error card and does not block others; bot unreachable shows the danger banner and dashboard-process checks only (existing behaviour); offline shows last snapshot with the "Offline, polling" banner. Phone: nav scrolls, sparkline grid 2 columns, tables scroll horizontally.

### `/dashboard/audit` (`view_audit`; built 2026-10-14)
- Header "Audit log" + one-line description, no actions. One card: filter bar (Action select with counts, Actor ID, Target ID, From/To dates, ghost "Clear filters"), a live hint row while the socket is up, a column header on desktop, then the rows and a footer ("N entries shown" + secondary "Load more").
- **Filters in the URL** (`?action=&actor=&target=&from=&to=`): the pattern for read-only list pages whose filtered views are worth linking; id fields apply after a short pause once valid and show field errors via `Field`.
- Rows: desktop grid (time, action badge, actor, target + one-line detail preview, chevron); under 768px a stacked block with the chevron pinned bottom-right. Expanding shows key/value detail (or pretty JSON in a `bg-muted` `pre`), mono ids with copy buttons, and secondary "This action/actor/target" filter buttons. New live rows get a 6 s `bg-primary-soft` highlight and an `aria-live` announcement.
- Action badge colour is derived from the verb (delete/ban/revoke = destructive, warn/timeout = warning, create/grant/added = success, edit/update/changed = info); the label is always printed.
- States: list skeleton, error with Retry (+status link on 0/502/503), two empty states (filters vs nothing yet), inline 403.

### 403 (`Forbidden403`, shared)
- Rendered inside the shell for permission failures (nav stays), full-page (no shell) for `not_a_member`. Centred: 52px danger-soft lock tile, badge "403, Access denied", h1 (24/32), explanation (existing copy), a mono chip "Required permission: manage_tickets" with copy button (forbidden only), buttons **Back to Overview** (secondary; **Back to home** for not_a_member) and **Sign in with another account** (ghost, `/api/auth/login`).

### Global overlays
- **Profile menu:** avatar + name + "Staff" role label; theme segmented control; Keyboard shortcuts (`?`); Service status (external link icon); Sign out.
- **Command palette** and **shortcuts dialog** as in section 5.
- **Toasts** as in 6.7.

---

## 8. shadcn/ui inventory

**Restyle existing** (`src/components/ui/`): `button` (variants/sizes per 6.1, radius 8, add `destructive-outline`, `lg` 44), `badge` (variants + dot/icon slot, add `accent`), `card` (radius 12, padding, header `bordered` prop, footer), `input`, `textarea`, `select`, `label` (new sizes and focus), `switch` (36x20), `tabs` (underline, count chips, roving focus), `table` (density, sort header, sticky header option), `dialog`, `alert-dialog` (type-to-confirm variant), `toast` (info variant, action, positions, pause on hover), `skeleton` (shimmer), `avatar` (sizes, initials tint), `copy-button`.

**Add:**
| Component | Purpose |
|---|---|
| `dropdown-menu` | profile menu, row menus, timeout duration |
| `tooltip` | icon buttons, collapsed sidebar |
| `popover` | filters, colour picker |
| `sheet` | phone navigation drawer |
| `command` (cmdk) | command palette |
| `breadcrumb` | header breadcrumb |
| `checkbox` | permissions matrix, inline embed field |
| `toggle-group` / `segmented` | density, theme, feed filter, refresh interval |
| `alert` | banners (offline, bot offline, notices) |
| `separator` | menus and cards |
| `progress` | signal bars in stats |
| `scroll-area` | palette results, long menus |
| `field` (project component) | label + hint + error wiring |
| `page-header`, `empty-state`, `stat-card`, `chart-card`, `kbd`, `live-pill` (project components) | shared patterns |

All are available from the installed `radix-ui` package and `shadcn` CLI (`cmdk` is one new dependency); `@fontsource-variable/geist` and `@fontsource-variable/geist-mono` are the only other additions.

**Layout components to add/rework:** `AppShell` (four-group sidebar, collapsible rail, header with breadcrumb, phone top bar + drawer + bottom nav), `navLinks.ts` (new `group` union), `ProfileMenu` (theme control), `ThemeContext`, `PageHeader`, `Breadcrumbs`.

---

## 9. Accessibility checklist

- Text contrast at least 4.5:1, control edges and focus rings at least 3:1 (see 3.3). Never convey status by colour alone: pair with icon, dot plus label, or text.
- Every interactive element is a real `<button>`, `<a href>`, `<input>` with a `<label>`. No `role="button"` on divs. Icon-only buttons have `aria-label`.
- Visible focus ring on all interactive elements; logical tab order; skip link ("Skip to content") as the first focusable element in `AppShell`.
- Landmarks: `<header>`, `<nav aria-label="Main">`, `<main id="content">`. Breadcrumb `<nav aria-label="Breadcrumb">`. Current page has `aria-current="page"`.
- Dialogs and drawers trap focus, restore it on close, close on Esc. Confirm dialogs put initial focus on Cancel.
- Live regions: toasts (`role=status|alert`), new catches (`aria-live=polite`), offline banner (`role=status`), form errors linked with `aria-describedby`.
- Tables use `<th scope>`, sortable headers use buttons with `aria-sort`. Tabs follow the WAI-ARIA tabs pattern (Radix).
- Charts have `role="img"` with a text alternative stating total and peak; the same figures exist as text elsewhere on the page.
- Respect `prefers-reduced-motion` (3.7) and `prefers-color-scheme` (3.2). Support browser zoom to 200 percent and text spacing overrides without clipping (use `min-w-0`, wrapping, no fixed heights on text containers).
- Touch targets at least 44x44 on phone.

---

## 10. Implementation checklist (incremental, safe order)

Ship each step on its own; the app must build and work after every step. Nothing below changes routes, API modules, permission gates or live hooks. Run `npm run build` and `npm run lint` in `server/frontend` after each step, and smoke-test login, Overview live stats, Honeypot live feed, and a 403 page.

1. **Tokens (no visual restructuring yet).**
   - Replace the token block in `globals.css` (3.1), add the `@theme inline` additions and type scale, drop the forced `color-scheme: dark`.
   - Add the `<html>` pre-paint theme script, `ThemeContext`, fonts (`@fontsource-variable/*`).
   - Grep for `text-destructive` and `bg-destructive/10` used as text/soft-bg and switch to `text-danger` / `bg-danger-soft`. Grep for hard-coded hex or `text-white` in pages and replace with tokens (the embed colour bar hexes in `CatchCard`/`EmbedPreview` stay).
   - Result: whole app re-themes in light by default with all existing layouts. Check both themes.
2. **Theme toggle.** Add the segmented control to `ProfileMenu`, the palette action, and confirm `prefers-color-scheme` behaviour and persistence. Verify charts (they already read CSS variables; confirm every Recharts colour prop uses `var(--...)`).
3. **Primitives restyle.** Update `button`, `badge`, `card`, `input`, `textarea`, `select`, `label`, `switch`, `tabs`, `table`, `dialog`, `alert-dialog`, `toast`, `skeleton`, `avatar`. Add new primitives (section 8): `field`, `alert`, `dropdown-menu`, `tooltip`, `popover`, `checkbox`, `segmented`, `separator`, `progress`, `scroll-area`, `sheet`, `breadcrumb`. Visual only; do not change props that existing pages pass (add new props with defaults).
4. **Shared patterns.** `PageHeader`, `EmptyState`, `StatCard`, `ChartCard`, `Kbd`, `LivePill`, skeleton compositions. Add `useBreadcrumbs`, `useHotkeys`, density and sidebar-collapsed `localStorage` hooks (all try/catch).
5. **Shell.** Rebuild `AppShell` and `navLinks.ts` (four groups, badges from already-loaded data, collapsible rail, header breadcrumb + palette trigger + live pill, phone top bar + drawer + bottom nav, skip link). `LiveIndicator` and `StatusIndicator` keep their hooks; only their rendering moves (header pill + sidebar footer). Restyle `CommandPalette` (grouping, recents, action items, `Ctrl/Cmd+K` unchanged). Verify: permission-filtered nav for a limited user; direct visit to a forbidden route still shows the inline 403 inside the shell; websocket still connects once (check the network tab).
6. **Pages, one per PR, in this order** (lowest risk and highest visibility first):
   1. Overview (`DashboardPage`, `QuickActions`), including the activity feed composed from existing hooks.
   2. Honeypot (`HoneypotPage`, `CatchCard`, `SetupCard`, `SettingsPanel`, `AppealsQueue`, `StatsDialog`). Keep `useHoneypotLive()` mounted once at page level.
   3. Moderation.
   4. Tickets list, detail, panel builder. Keep `useTicketsLive()` at the list page.
   5. Diagnostics (largest; restyle `widgets.tsx`/`sections.tsx` first, then the page chrome).
   6. Templates list and editor (unsaved-changes guard, preview).
   7. Settings, Permissions.
   8. Self-roles, QOTD.
   9. Public pages: `/appeal` (verify it still has no shell and no auth calls), `/` Home, `/status`, `Forbidden403`.
7. **Polish.** Undo toasts (section 11 list), optimistic updates, keyboard shortcuts and the shortcuts dialog, table density persistence, `aria` audit, reduced-motion audit, 200 percent zoom check, phone pass at 390 and tablet at 820.
8. **Verification.** Contrast re-check for any token you changed, axe or Lighthouse accessibility run on Overview, Moderation, Honeypot, Appeal in both themes, manual keyboard-only pass, websocket reconnect test (stop and start the dashboard process, confirm Live to Reconnecting to Offline to Live transitions and banners), a limited-permission user pass.

---

## 11. Specific improvements over today's UI

| Area | Today | Redesign |
|---|---|---|
| Theme | Dark only | Light default, dark polished, system aware, toggle in profile menu |
| Nav | Two groups; community tools mixed with moderation | Four groups, badges for open tickets and pending appeals, collapsible rail, phone drawer + bottom nav |
| Page headers | h1 sizes vary (xl vs 2xl), actions placed ad hoc | One `PageHeader` with description and primary action |
| Feedback | Toast on most actions, no undo | Undo toast for Timeout, Remove timeout, Claim/Close ticket, self-role entry removal, honeypot enable toggle; confirm dialogs for Ban, Kick, Delete, remove warning, Deny/Approve appeal, granting Grants |
| Confirmations | Generic alert dialogs | Named target + consequence + reason; type-to-confirm for delete ticket and manage_permissions grant |
| Validation | Errors mostly on submit/toast | Inline messages with the fix; Discord ID length check; counters |
| Tables | Basic, no persistence | Sort, sticky filters, density persisted, "Show more" for live lists, row-as-link |
| Loading | Spinners/plain text in places | Layout-matched skeletons |
| Overview | Stat tiles + two lists + quick action chips | Header actions, unified live activity feed, quick actions + bot status rail |
| Honeypot | Stats hidden in a dialog only | At-a-glance stats and pending appeals in a rail on the Catches tab, dialog for detail |
| Moderation | Three stacked cards, long page | Two-column lookup + profile header with action bar, tabs for histories |
| Offline/bot-offline | Sidebar label only | Banners + disabled actions with reasons (a header pill shipped too, removed 2026-10-14; the sidebar footer row remains) |
| Public appeal | Single column, helper collapsed | Branded top bar, helper opens on invalid ID, example ID row, phone-first sizing |
| Keyboard | Ctrl+K only | Shortcuts (`g` navigation, `/`, `[`, `?`) and a shortcuts dialog |

**What is safe to undo or optimise optimistically** (only with existing endpoints; do not add any): Honeypot enabled switch (optimistic, rollback on error); timeout, then Undo = call the existing untimeout; Remove timeout, then Undo = call the existing timeout with the previous duration if known (otherwise no Undo); Claim/Close ticket use the existing reverse action only if one exists in `api/tickets.ts`, otherwise omit Undo and keep the immediate action; self-role entry removal Undo re-adds through the create call if the payload is still known. If a reverse call does not exist, show a plain success toast, never a fake Undo. Approve/Deny appeal, Ban, Kick, Delete and permission grants are never optimistic and never "undoable" (they message people or are destructive).

---

## 12. Open questions for the product owner

1. **Accent colour:** indigo (`#3B4BD6`) was chosen as calm and distinct from Discord blurple. Prefer a different hue (teal, blue)? Only `--primary*`, `--ring`, `--sidebar-primary/accent` and `--chart-1` change.
2. **Verification page:** the IA reserves a slot, but no route exists. Is a Verification page planned?
3. **Notifications bell:** shown in the mockup as a reserved slot. Hide it until there is something to notify, or define what it should show (pending appeals, new tickets)?
4. **Overview activity feed:** OK to compose it client-side from existing sources (warnings, tickets, honeypot catches, unbans), or should the backend expose a single audit feed (the `audit` live topic already exists) later?
5. **Font hosting:** self-host Geist via npm (recommended) or load from Google Fonts?
6. **Ticket panels list:** the mockup shows a Panels card on the Tickets page; confirm an existing endpoint exposes panels, otherwise it is omitted.

---

## 13. Implementation status

Implemented in `server/frontend` (all routes, API hooks, permission gates, the live layer and the stay-logged-in logic are unchanged).

**Done:** tokens and light/dark themes with no-flash script, `ThemeContext`, profile-menu theme control, Geist and Geist Mono via npm; shell (four-group sidebar with count badges, collapsible rail, header with breadcrumbs, Ctrl/Cmd+K palette with Recent/Pages/Settings/Actions, live pill, phone drawer + bottom nav, skip link, connection banners, shortcuts dialog and `useHotkeys`); primitives (button variants and `loading`, badge, card, field, alert, table with `SortableHead` and density, tabs, segmented, checkbox, switch, dropdown-menu, tooltip, sheet, progress, kbd, toast with Undo action and max 3); patterns (PageHeader, states/skeletons, StatCard, ChartCard, SaveBar, ConfirmDialog); every page restyled: Overview (unified client-side activity feed from warnings, open tickets and honeypot catches, quick actions rail, bot status), Moderation (two-column lookup, member header, action bar, tabbed tables, Undo for Warn/Timeout, confirm for Kick/Ban/remove warning), Honeypot (setup + how-it-works, status card with optimistic enable, rail with at-a-glance and pending appeals, stats dialog with themed charts, settings with sticky save bar), Tickets (table with sort, density, panels card), ticket detail (details/actions rail, type-to-confirm delete), panel builder (sticky preview), Templates (dialog, table, row menu) and editor (preview, placeholder chips, unsaved guard), Self-roles (dialog, collapsible cards, Undo on entry removal), QOTD, Permissions (checkbox matrix with labels for the 14 `VALID_PERMISSIONS`, grant dialog, type GRANT for the sensitive grants: `ban_members`, `kick_members`, `moderate_members`, `manage_messages_bulk`, `verify_members`, `manage_autorole`, `manage_reaction_roles`, `view_audit`, `staff_base`; there is no `manage_permissions`), Settings (group list, Live/Restart notes, Discord ID validation, save bar), Diagnostics (page chrome, health checklist, metric tiles, section nav tabs, sortable/filterable tables with Copy CSV, chart palette), public Appeal, Home, Status, 403.

**Deviations:** Diagnostics has no header refresh controls at all (neither the spec's Paused/5s/15s control nor the former Pause/Resume, Refresh now, Copy JSON and Download buttons, removed 2026-10-14). The header live pill was also removed on 2026-10-14 (sidebar footer + banners only). The palette was restyled in place (no `cmdk` dependency). The unsaved-changes guard is the shared `useUnsavedChangesGuard` (6.14), covering links, palette, hotkeys, Back/Forward, reload and tab close (BrowserRouter has no route blockers, so it is hand-built). Nested toggle groups are not used inside menus (the theme control in the profile menu is plain radio buttons). Undo is offered only where a real inverse exists: Warn (remove warning), Timeout (untimeout), self-role entry removal (re-add, lands at the end of the list). Claim/Close ticket and Remove timeout show plain toasts (no reverse endpoint). The Discord ID rule (17 to 20 digits) applies to warn-a-user, Settings IDs and the honeypot log channel; the public appeal keeps 15 to 22.

**Skipped (needs backend work):** notifications bell (omitted by decision); Verification page (none exists); member joined date and role badges on the Moderation member header (no member-detail endpoint; timeout state and warning count are derived from existing data); template Version column (not in the API); QOTD "Posted by" and "Status" history columns (not in the API); the "Unban" button is always shown because banned state is not exposed.

**Verification performed:** `npm run build` (zero TypeScript errors), `npm run lint` (no new warning categories), backend `pytest`, and visual review against a throwaway mock API of Overview, Moderation, Honeypot, Tickets, ticket detail, Templates editor, Settings, Permissions, Self-roles, QOTD, Diagnostics, Status and Appeal (light), Overview/Honeypot/Moderation and the palette (dark), and Overview/Tickets/Honeypot plus the drawer at phone width (390). Not visually checked: Template list, panel builder, ticket detail and Diagnostics in dark or at phone width, tablet width, 200 percent zoom, an automated axe/Lighthouse run, and the offline/reconnect transitions against a real server.
