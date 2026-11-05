# Kick-Bot Technical Documentation

> Reference documentation for the Discord bot + moderation dashboard in this repository. Written for engineers who need to install, extend, deploy, or debug the system.
>
> Last verified against the code: 2026-09-29. Where this file and the code disagree, the code wins. The honeypot feature (migration 25) was being built by other work at the same time; section 19 records what had landed when this was written. `docs/honeypot_contract.md` is the design contract for it.

---

## 1. Project Overview

**Purpose.** A Discord moderation/community-management bot for a single guild ("Gamer Girls HQ", guild ID `1530172123228930200`, hardcoded as `ALLOWED_GUILD_ID` in `main.py` and as the `DASHBOARD_GUILD_ID` default in `server/config.py`; the bot leaves any other guild on `on_ready`), paired with a web dashboard that lets staff manage the bot without touching Discord slash commands directly.

**Major features.**
- Moderation: warnings, kicks, bans, timeouts, bulk purge (by count or time window), automatic spam detection (including user-installed app spam), a 7-day moderation message log in its own file, native-action logging
- Ticket system: panel-based support/verification tickets with claiming, archiving, chat logging, auto-purge
- Verification: role-swap flow for new members, usable via command or ticket button
- Templates: reusable, versioned message/embed templates rendered with placeholders, usable across tickets/events/schedules/reaction-roles
- Event automation: bind templates to member-join/leave/boost/role-change events
- Reaction roles: self-serve role panels
- Scheduled sends: cron-based recurring template posts
- Autorole: automatic role grants on join/boost/role-add triggers
- QOTD: AI-generated (local Ollama) or fallback "Question of the Day" with staff approval workflow
- Birthdays: self-service birthday/timezone tracking, automatic role + announcement on the day, bulk import/export
- Music: voice-channel playback via yt-dlp (with a bundled PO-token companion server, section 18)
- Honeypot: a public trap channel that automatically bans/softbans/kicks hacked accounts and spam bots that post in it, with a public ban-appeal flow (section 19)
- Fortnite item shop: daily auto-post of the shop (fortnite-api.com), `!shop`, `/fortnite` config and member wishlists (section 24)
- Server provisioning: `setup_server.py` reconciler that creates the guild's roles/categories/channels (section 17)
- Fine-grained permission system layered on top of (and independent from) Discord's native permissions
- A React dashboard (`dash.idlesys.xyz`) covering moderation, tickets, templates, permissions, QOTD, self-roles (live role list via the `roles` topic, section 22), Discord role management (staff-only Roles page, section 23), the Fortnite shop, settings, honeypot, a hidden owner-only Debug page (section 25), and a public status page

**High-level architecture:**

```
┌────────────────────────────────────────────┐        ┌───────────────────────────────┐
│         Bot process (main.py)               │        │   Dashboard process           │
│                                              │        │   (server/app.py)             │
│  ┌────────────────────────────────────────┐  │        │                               │
│  │ discord.py Client/Bot                   │  │        │  - Discord OAuth2 login       │
│  │  - live cache: guilds, members, roles   │  │  HTTP  │  - sessions (cookie + DB)     │
│  │  - 27 cogs loaded from a hardcoded list │  │◄───────┤  - CSRF protection            │
│  ├────────────────────────────────────────┤  │ Bearer │  - serves React dist/ (SPA)   │
│  │ internal_api (FastAPI, embedded,        │  │ token  │  - server/routes/*.py         │
│  │  same event loop, 127.0.0.1:8766)       │  │        │  - internal_client.py (httpx) │
│  └────────────────────────────────────────┘  │        └───────────────────────────────┘
│                                              │                       ▲
│  bot.db (SQLite, aiosqlite, raw SQL)        │                       │ HTTPS via
└────────────────────────────────────────────┘                       │ Cloudflare Tunnel
                                                                       │
                                                              React dashboard (browser)
                                                              server/dashboard.db (SQLAlchemy
                                                              + Alembic — sessions only)
```

Two OS processes you start yourself (plus the PO-token Node server the bot spawns as a child for music, section 18), three logical layers. The bot process embeds `internal_api` — a FastAPI app sharing the bot's event loop — so dashboard requests can read/write the *live* discord.py cache (member lists, roles, presence) with zero extra Discord API round-trips. The dashboard process is fully separate, internet-facing, and has no direct database or Discord-cache access of its own beyond session storage; every piece of guild data it shows comes from calling `internal_api` over local HTTP with a shared-secret bearer token.

---

## 2. Architecture

### Discord.py bot process (`main.py`)

Single entry point. Before anything else it forces UTF-8 on stdout/stderr and enables `faulthandler` (all threads, to stderr), so a native crash (e.g. exit code `0xC0000005`) leaves the Python stack of every thread in `logs/supervisor/bot.log`. On startup it:
1. Connects to `bot.db` (SQLite via `aiosqlite`) and runs all pending migrations automatically (see §7).
2. Constructs a single shared `PermissionService` and attaches it as `bot.permission_service`.
3. Attaches a custom `EventBus` (`core/event_bus.py`) as `bot.event_bus`.
4. Loads 27 cogs (including `cogs.honeypot`) from a hardcoded list, in order, via `bot.load_extension()`.
5. Builds the embedded `internal_api` FastAPI app, attaches the live `bot` object to `app.state.bot`.
6. Runs `bot.start()` and `internal_server.serve()` concurrently on the same `asyncio` event loop via `asyncio.gather()`.

```python
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.presences = True
bot = create_bot(intents)   # core/bot_factory.py
```

`core/bot_factory.create_bot` builds `commands.Bot(command_prefix="!", intents=..., allowed_mentions=DEFAULT_ALLOWED_MENTIONS, tree_cls=GuildOnlyCommandTree)`:
- **Global `allowed_mentions`:** `everyone=False, roles=True, users=True, replied_user=True`. No send pings `@everyone`/`@here` unless it passes its own `allowed_mentions`. The only deliberate opt-in is the birthday announcement's `%e` placeholder (`cogs/birthdays.py`; it opts in only when the saved template itself contains `%e`, and a message containing `%e` can only be SAVED by full staff = `OWNER_ID`/`STAFF_ROLE_IDS`, on Discord and the dashboard, `services/birthday_service.everyone_block_reason`; the announcement sends with `roles=False`, so role mentions typed into it never ping; built and tested 2026-10-28, NOT live until the bot and dashboard restart (the frontend page is in the built `dist/` and shows an error state until then)). Template posts (`ManagedMessageService`) also pass `everyone=False` explicitly; scheduled sends and event-binding posts rely on the global default. Music sends nothing that can ping (see `cogs/music.py`).
- **Guild-only slash commands:** the tree's default `allowed_contexts` is guild-only (commands are hidden in DMs after the next sync), and `GuildOnlyCommandTree.interaction_check` refuses any app-command interaction without a guild with an ephemeral "This command only works in the server." (autocomplete gets an empty list) instead of the cog crashing on `interaction.guild is None`.
- **Prefix-command errors:** `cogs/diagnostics_collector.py` registers `on_command_error`, which switches off discord.py's default handler, so that listener calls `core/command_errors.handle_command_error`: `CommandNotFound` ignored; a command/cog with its own error handler is left to it; user errors (missing/bad argument, check failure, cooldown, DM-only) get a short no-ping reply; anything else logs the full traceback (logger `commands.errors`) and replies "Something went wrong running that command." The diagnostics counters still count every error.

There is **no hot-reload** — cogs are static Python modules imported once. Adding a feature means writing a new cog and appending it to the `COGS` list, then restarting the process.

Other startup behavior worth knowing:
- **Single guild.** `on_ready` calls `guild.leave()` on any guild whose ID is not `ALLOWED_GUILD_ID`, read from `GUILD_ID` in `.env` (required since 2026-10-30; `main()` exits with a clear message when it is missing; this deployment: `1530172123228930200`).
- **Slash commands are synced globally** (`bot.tree.sync()`) on every `on_ready`, including reconnects. Global sync can take a while to propagate in the Discord client.
- **Prefix is `!`** (the old `$` prefix and the old "fng" server are gone). Slash commands are used for configuration; prefix commands are used for moderation (`!ban`, `!kick`, ...), `!verify`, and all of music. Prefix commands are `hidden=True` except music.
- `main.py` starts the PO-token companion server (`services/pot_server_service.py`, section 18) once from `on_ready` and stops it on shutdown.
- If a cog raises during `load_extension`, the error is logged (`FAILED to load cog ...`) and startup continues with the remaining cogs. Check the log after a restart.

### Internal FastAPI API (`internal_api/`)

Embedded inside the bot process (not a separate service). Binds to `127.0.0.1:8766` by default. Every route except the unauthenticated `GET /health` requires an `Authorization: Bearer <INTERNAL_API_SECRET>` header, checked with `hmac.compare_digest` (401 if the header is missing/malformed, 403 if the secret is wrong). The full route list is in section 9. Routes reach the live bot via `request.app.state.bot`, so guild/member/role lookups are direct cache reads (`bot.get_guild()`, `guild.get_member()`), not database queries. This process does **no** user-level authorization — it trusts anyone holding the shared secret. All actual "is this Discord user allowed to do this" logic lives in the dashboard process, one layer up.

### Dashboard FastAPI server (`server/`)

A genuinely separate process (`uvicorn server.app:app --host 127.0.0.1 --port 8080`). Handles:
- Discord OAuth2 login/callback/logout (`server/auth/oauth.py`)
- Server-side sessions backed by its own SQLite DB (`server/dashboard.db`, SQLAlchemy + Alembic)
- CSRF origin-check middleware
- Serving the built React SPA (`server/frontend/dist/`) same-origin (a catch-all route falls back to `index.html` for client-side routes; it is registered last, so new routers must be included before it)
- A public `GET /api/status` (probes the internal API's `/health`; on a connection error it reports `detail: "unreachable"`, never the raw httpx error) and `GET /health`
- `server/routes/*.py` — thin, permission-gated proxies that call into `internal_api` via `server/services/internal_client.py` (a plain `httpx.AsyncClient`). One deliberate exception: `server/routes/supervisor.py` calls `supervisor.py`'s loopback control API directly (section 26)

### React frontend (`server/frontend/`)

React 19 + Vite 8 + Tailwind v4 + Radix UI + `@tanstack/react-query`. REST plus one shared live-update WebSocket per tab (`src/live/*`, section 22); live queries fall back to their old polling while the socket is down. Auth state is derived entirely from one `GET /api/me` call; there is no JWT, only an itsdangerous-signed opaque session-ID cookie.

### Database layers

Two independent SQLite databases, two independent migration systems (see §7 for full detail):
- **`bot.db`** — raw SQL via `aiosqlite`, hand-rolled migration runner, migrations 1-31 (25-26, 28-31 `honeypot`, see section 19; 27 `fortnite_shop`, see section 24). 32 tables through migration 26 (29 before honeypot), 34 with migration 27 (`fortnite_shop_config`, `fortnite_wishlist`).
- **`server/dashboard.db`** — SQLAlchemy 2.0 async ORM + Alembic, two tables (`sessions`, `error_reports`).

### How data flows through the system

**Read path (e.g. dashboard loads ticket list):**
Browser → React Query → `GET /api/tickets` (dashboard process) → session check (`require_permission`) → `internal_client.py` (httpx, Bearer token) → `GET /guilds/{id}/tickets` (internal_api, embedded in bot process) → `TicketRepository` → `bot.db` → live-cache enrichment where needed → JSON response (with all snowflakes stringified, see §15) → back up the chain.

**Write path (e.g. dashboard saves a setting):**
Browser → `PUT /api/settings` (dashboard process, `require_staff`) → `internal_client.py` → `PUT /guilds/{id}/settings` (internal_api) → `SettingsService.update()` validates (422 per-field on failure), writes `guild_settings`, writes a `settings_update` audit row and calls `invalidate_cache()`. There is **no push/event mechanism** back to the bot: the two live ticket settings are picked up via a 5-second TTL cache on next use, everything else is applied onto `config` by `main.py` at the next bot start, before cogs load (see §6).

**Discord-originated path (e.g. member joins):**
Discord gateway → `cogs/gateway.py`'s `on_member_join` listener → publishes `EventBus` event `"member_join"` → subscribers (`services/event_service.py`, autorole service) react → template rendered via `core/renderer.py` → message sent to Discord.

---

## 3. Repository Structure

### `cogs/` — Discord.py Cog classes (one module per feature; see §10)
**Purpose:** One module per feature area. Thin — parse Discord input, check permissions, delegate to `services/`.
**Important files:** see §10 for full per-cog documentation. `gateway.py` is the central event-translation bridge; `moderation.py`, `tickets.py`, `verify.py` are the core moderation trio; `music.py` is the only prefix-command-only cog (its player state lives on the cog; the dashboard drives it, section 28). `permissions.py` is dead legacy code (not a cog, not loaded).
**Interacts with:** `services/` (business logic), `core/` (rendering, placeholders, event bus), `database/repositories/` (indirectly, via services).

### `core/` — cross-cutting infrastructure
**Purpose:** Shared plumbing used by multiple cogs/services, not feature-specific.
**Important files:**
- `event_bus.py` — minimal async pub/sub (`subscribe`/`publish`), independent of discord.py's own event dispatch.
- `sync_queue.py` — debounced (2s) worker queue that batches template-edit-driven message resyncs.
- `placeholder_engine.py` + `placeholders/` — `{user}`, `{server}`, `{ticketnumber}`, etc. substitution registry, 106 forms with `{name:arg}` and `{name|fallback}` syntax and a metadata catalogue (section 14 "Template placeholders") (`placeholders/__init__.py` is an empty/unused stub — registration actually happens in `placeholder_engine.py`).
- `renderer.py` — turns a DB template row + `RenderContext` into a real `discord.Embed`.
- `component_builder.py` — turns DB component rows into real `discord.ui.View`/buttons/selects.
- `validator.py` — template content/URL validation.
- `logging_setup.py` — UTF-8-safe structured logging setup, called before any other import in `main.py`.
- `bot_factory.py` — `create_bot(intents)`: the `commands.Bot` with prefix `!`, the global `allowed_mentions` and the guild-only `GuildOnlyCommandTree` (§2).
- `command_errors.py` — `handle_command_error`, the prefix-command error handler called from `cogs/diagnostics_collector.py` (§2).
**Interacts with:** consumed by nearly every cog that sends a rendered message (tickets, events, welcome, boost, verify, managed messages, scheduled sends, reaction roles).

### `services/` — business logic layer
**Purpose:** DB-aware business logic, independent of Discord's API shape. Cogs call into these; these call into `database/repositories/`.
**Important files:** `permission_service.py` (the permission system, §8), `settings_service.py` (config bridge, §6), `moderation_service.py`, `ticket_service.py`, `warning_service.py`, `qotd_service.py`, `event_service.py`, `scheduler_service.py`, `autorole_service.py`, `reaction_role_service.py`, `template_service.py`, `audit_service.py`, `log_router.py` (routes log events to configured channels by category), `pot_server_service.py` (spawns the PO-token Node server for music, §18), `honeypot_service.py` (§19), `spam_detector.py` (pure in-memory spam rules for `cogs/anti_spam.py`), `message_source.py` (pure helpers: who really posted a message — app reply invoker, webhook, bot — and its text including embeds; shared by the honeypot, anti-spam and the message log), `message_log_service.py` (the moderation message log store, its own SQLite file), `fortnite_shop_service.py` (Fortnite item shop fetch/parse/embeds/daily post), `debug_actions_service.py` (owner-only Debug page test actions), `self_role_policy.py` (who may make a role self-assignable and the click/autorole re-check, §10 `cogs/reaction_roles.py`).
**Interacts with:** `database/repositories/` for persistence; `core/renderer.py` for message rendering; consumed by `cogs/` and by `internal_api/routes/`.

### `database/` — persistence layer
**Purpose:** SQLite access, migrations, and the repository pattern.
- `connection.py` — thin async `Database` wrapper around `aiosqlite` (`fetchone`/`fetchall`/`execute`/`execute_returning_id`/`executescript`/`transaction`).
- `migrations/runner.py` + `migrations/sql/*.sql` — hand-rolled migration system (§7).
- `repositories/` — one repository class per domain (`honeypot_repo.py` is new with migration 25), each hand-writing its own SQL. `base.py` is nearly empty (just stores `self._db`) — no generic ORM/CRUD magic.
**Interacts with:** consumed exclusively by `services/` (cogs never touch repositories directly, with one documented exception in `template_commands.py`'s rollback/import flows).

### `internal_api/` — embedded dashboard-facing API
**Purpose:** FastAPI app run inside the bot process, exposing guild-scoped REST endpoints backed by the live discord.py cache + `bot.db`.
**Important files:** `app.py` (app factory + router registration), `auth.py` (shared-secret bearer auth), `deps.py` (dependency helpers resolving bot/guild/member from the live cache), `snowflakes.py` (ID precision-loss fix, §15), `routes/*.py` (one module per feature area; `honeypot.py` is new, §19). `GET /health` is defined in `app.py` and is the only unauthenticated route (FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled in `create_app` so they don't expose the route map without the bearer; 2026-10-28, NOT live until the bot restart).
**Interacts with:** called exclusively by `server/services/internal_client.py`; reads/writes `bot.db` via the same `services/`+`database/repositories/` layers cogs use.

### `server/` — dashboard backend (separate process)
**Purpose:** Internet-facing FastAPI app: OAuth, sessions, CSRF, static SPA hosting, and a thin permission-gated proxy to `internal_api`.
**Important files:** `app.py` (app factory, router + static mount registration), `config.py` (its own env config, separate from root `config.py`), `csrf.py`, `auth/oauth.py` (OAuth2 flow), `auth/session.py` (session creation/signing), `permissions/guards.py` (`require_permission`/`require_staff` dependencies), `services/internal_client.py` (httpx client to `internal_api`), `routes/*.py` (one module per feature area, including `status.py` and, for honeypot, `honeypot.py` + `appeals.py`), `db/` (SQLAlchemy models + engine), `alembic/` (its own migration chain).
**Interacts with:** `internal_api` over HTTP (never touches `bot.db` directly); serves `server/frontend/dist/` statically. Exception: `/api/supervisor/*` talks to `supervisor.py`'s control API on `127.0.0.1:8767` directly, so restarts work while the bot is down (section 26).

### `server/frontend/` — React dashboard SPA
**Purpose:** The actual UI staff use. See §9 for full detail.
**Important files:** `src/app/routes.tsx` (route tree), `src/app/RequireAuth.tsx` (auth guard), `src/context/AuthContext.tsx`, `src/api/*.ts` (one module per feature, react-query hooks), `src/pages/*/`, `src/components/layout/` (AppShell, CommandPalette, QuickActions, ProfileMenu, Sidebar, ShortcutsDialog, breadcrumbs, useHotkeys, navLinks.ts, commands.ts), `src/components/patterns/` (PageHeader, states, SaveBar, ConfirmDialog, LivePill...), `src/context/ThemeContext.tsx`.
**Interacts with:** `server/routes/*.py` only, via same-origin relative fetches (`credentials: "include"`).

### `setup_server/` — guild provisioning reconciler
**Purpose:** Idempotent CLI tool (`setup_server.py` at repo root is the entry point) that creates the guild's roles, categories, channels, and permission overwrites from the declarative data in `setup_server/config.py`. Full details and limitations in section 17.
**Important files:** `main.py` (orchestrator/CLI), `config.py` (desired state), `steps/roles.py`, `steps/categories.py`, `steps/channels.py`, `steps/permissions.py`, `validate.py` (drift report), `state.py` (persists `server_state.json`), `discord_client.py` (its own minimal login).
**Interacts with:** Discord API directly with its own client (not through the bot process; do not run it while you expect exact control of channel permissions, see section 17).

### `pot_server/` — PO-token provider (vendored)
**Purpose:** A vendored copy of `bgutil-ytdlp-pot-provider` (`pot_server/server/` is the Node HTTP server, `pot_server/plugin/` the yt-dlp plugin). The bot spawns `pot_server/server/build/main.js` on startup so YouTube playback works for gated videos. See section 18.

### `install_scripts/` — first-time setup helpers
**Purpose:** Numbered per-OS scripts (`windows/`, `macos/`, `linux/`) that check for Python/Node/ffmpeg, create `venv/`, install both requirements files, run `alembic upgrade head`, and build the frontend. `install_scripts/windows/05-install-autostart.bat` drops Startup-folder shortcuts for `run_bot.bat` and `run_dashboard.bat`. `install_scripts/windows/07-install-sv.ps1` (run by `sv install`) optionally adds a Desktop shortcut to the `sv.bat` menu and puts the repo on the user `Path`. See `install_scripts/README.md`. These scripts do not build `pot_server/` and do not start the tunnel.

### Root launch scripts
`run_bot.bat`, `run_dashboard.bat`, `start_all.bat` (bot + dashboard + Cloudflare tunnel, each in its own window), and `server/start_dashboard.bat` / `server/start_tunnel.bat`. Alternative: `supervisor.py` (all three as children, auto-restart, alerts, control API; sections 11 and 26), controlled with `sv.bat` (menu + short commands for the running supervisor; section 26). All use `venv\Scripts\python.exe` and must be run from (or `cd` to) the repo root; they do that themselves.

### `build_scripts/` — legacy PyInstaller packaging (bot only, not the current deployment)
**Purpose:** `build.bat` freezes `main.py` into `dist\Bot\main\main.exe` with a hardcoded `--hidden-import` list; `package.bat` + `_package_helper.py` zip it; `start.bat` runs the exe. This was used to hand a frozen bot to someone else's machine (see `archive/README.txt`).
**Caveat:** the hidden-import list is stale. It predates the moderation/logging/welcome/boost/verify/info cogs, `settings_service`, `warning_service`, `pot_server_service`, `honeypot_service`, and several repositories, and it does not bundle `pot_server/`, `server/`, or Node/ffmpeg. The scripts also resolve `ROOT` to their own folder (`%~dp0`), so they expect to sit at the repo root next to `venv\`, `main.py`, `.env`, and `bot.db`; run from `build_scripts/` as they are now, the venv preflight fails. Copy them to the repo root first if you ever need a frozen build. Treat the frozen build as unverified for the current code; the live deployment runs from the venv (section 11).

### `archive/`, `ui/`
`archive/` (local only, git-ignored, not in the repository) holds old snapshots. Nothing in the code imports from it. `ui/` (`confirm.py`, `embed_editor.py`, `paginator.py`) is live: shared discord.py View helpers used by the template editor and paginated commands.

### `tests/` — automated tests
**Purpose:** Small pytest suite. `tests/conftest.py` only enables the `pytest_asyncio` plugin. `tests/integration/` has `test_guild_repo.py` (3 async `GuildRepository` tests) with a `conftest.py` that builds an in-memory `Database` from `001_initial.sql`. `tests/unit/` holds the tests added with the honeypot and PO-token work: `test_honeypot_service.py`, `test_honeypot_repo.py`, `test_honeypot_appeals.py`, `test_honeypot_stats.py`, `test_honeypot_api.py` (internal API), `test_dashboard_honeypot_routes.py` (dashboard backend + public appeal routes), `test_honeypot_support.py` (shared fakes, no tests), and `test_pot_server_service.py`. Later suites cover diagnostics, live updates (`test_live_*.py`), music, the `roles` live topic (`test_role_live_service.py`), the Roles page (`test_role_admin_*.py`), the Fortnite shop (`test_fortnite_shop.py`) and the Debug page (`test_debug_*.py`). See section 20 for the full coverage list, how to run them and the known issues.
**Interacts with:** `services/honeypot_service.py`, `services/pot_server_service.py`, `database/repositories/` (guild and honeypot repos), `internal_api/`, `server/routes/`.

---

## 4. Requirements

### Bot and dashboard backend (Python)

- **Python 3.13** (developed and tested on 3.13; `audioop-lts` in `requirements.txt` exists because 3.13 removed `audioop`, and the code uses 3.10+ syntax).
- **Dependencies:** pinned in `requirements.txt` (bot, plus test and maintenance tools: pytest, pytest-asyncio, pip-audit; PyInstaller only for the legacy `build_scripts/`) and `server/requirements.txt` (dashboard backend: FastAPI, uvicorn, httpx, SQLAlchemy + Alembic, itsdangerous, slowapi, aiofiles, psutil, websockets). Both are installed into the same venv. The pins are the source of truth; this document does not repeat version numbers.
- **System tools:** FFmpeg on `PATH` (music playback), Node.js (the PO-token server and yt-dlp's JavaScript challenges), optionally Ollama (QOTD) and `cloudflared` (public access).
- **Discord application:** a bot with the privileged intents Server Members, Message Content and Presence (`main.py` turns all three on), plus an OAuth2 client ID/secret for dashboard and appeal sign-in (README "Create the Discord application").

### Dashboard frontend (Node)

- **Node.js 22+** (the PO-token server's `engines` asks for >=22; the frontend builds on the same version).
- **Dependencies:** `server/frontend/package.json` + `package-lock.json` (React 19, React Router, TanStack Query, Radix UI / shadcn components, Tailwind CSS 4, Recharts, Lucide; dev: Vite, TypeScript, oxlint). Install with `npm ci`.
- **Build:** `npm run build` runs `tsc -b && vite build`, producing `server/frontend/dist/` (served statically by `server/app.py`). `npm run dev` starts a Vite dev server on port 5173 that proxies `/api` to `127.0.0.1:8080`.

### Infrastructure

- **OS:** currently Windows (see §11). The Python code is OS-agnostic (`MUSIC_NODE_PATH` now auto-detects `node` via `shutil.which`); only the launch `.bat` scripts and the Windows-only Startup shortcut installer are Windows-specific.
- **Ports:** `8766` (internal API, loopback only), `8080` (dashboard backend), `4416` (PO-token server, loopback only, spawned by the bot), `8767` (supervisor control API, loopback only, only when running under `supervisor.py`; section 26), `5173` (frontend dev server, dev only). No port is required to be open externally except whatever the tunnel/reverse-proxy forwards.
- **Cloudflare Tunnel:** current public exposure mechanism (`cloudflared tunnel run idlesys`, forwarding `dash.idlesys.xyz` → `localhost:8080`). Requires a `cloudflared` install and an existing tunnel/DNS configuration in the Cloudflare dashboard; the tunnel config lives outside the repo (`~/.cloudflared/config.yml`).

---

## 5. Installation Guide

These steps assume Windows with the existing `.bat` scripts; see §16 for what changes on Mac/Linux. `install_scripts/` automates steps 2, 3, 5 (dashboard DB), and 6 for Windows/macOS/Linux (`install_scripts/README.md`); it does not build `pot_server/` or set up the tunnel.

1. **Clone the repository.**

2. **Create a Python virtual environment** at the repo root:
   ```
   python -m venv venv
   ```

3. **Install dependencies** into that venv:
   ```
   venv\Scripts\pip install -r requirements.txt
   venv\Scripts\pip install -r server\requirements.txt
   ```

4. **Configure `.env`** at the repo root. Minimum required variables (the process raises `RuntimeError` at import time if any required var is missing — see §6 for the full table):
   ```
   GUILD_ID=...                       # your server's ID; the bot leaves every other server
   DISCORD_TOKEN=...
   INTERNAL_API_SECRET=...            # any long random string, shared between both processes
   DISCORD_CLIENT_ID=...
   DISCORD_CLIENT_SECRET=...
   SESSION_COOKIE_SECRET=...          # any long random string
   ```
   Plus `OWNER_ID` and `STAFF_ROLE_IDS`, and whichever role/channel/category IDs and OAuth redirect URIs match your server and deployment (README section "Configure `.env`").

5. **Run database migrations:**
   - **Bot DB (`bot.db`) — fully automatic.** Simply starting the bot (`python main.py`) creates a fresh `bot.db` and applies every migration in `MIGRATIONS` (`main.py`) in order before the bot logs in. No manual step needed.
   - **Dashboard DB (`server/dashboard.db`) — manual, one-time.** From the repo root:
     ```
     venv\Scripts\python.exe -m alembic -c server\alembic.ini upgrade head
     ```
     (Must be run from the repo root, not from inside `server/` — `alembic.ini`'s `script_location` is relative to CWD, though the actual DB path is resolved absolutely regardless.)

6. **Build the frontend:**
   ```
   cd server\frontend
   npm ci
   npm run build
   cd ..\..
   ```
   This produces `server/frontend/dist/`, which `server/app.py` serves statically — no separate frontend server needed in production.

7. **Build the PO-token server** (music only, optional but recommended):
   ```
   cd pot_server\server
   npm ci
   npx tsc
   cd ..\..
   ```

8. **Start services.** Each runs in its own console window; there is no process supervisor unless you use `supervisor.py` (see Run section).
   ```
   run_bot.bat            :: venv\Scripts\python.exe main.py  (bot + internal API :8766 + PO-token server :4416)
   run_dashboard.bat      :: venv\Scripts\python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8080
   start_all.bat          :: bot, then (after 5s) dashboard, then the Cloudflare tunnel, in three windows
   ```
   `install_scripts\windows\05-install-autostart.bat` adds Startup-folder shortcuts for `run_bot.bat` and `run_dashboard.bat` (not `start_all.bat`, and not the tunnel). Start the **bot before the dashboard**: the dashboard only shows live data while the bot's internal API is up.

   For public exposure only:
   ```
   server\start_tunnel.bat    :: cloudflared tunnel run idlesys  (dash.idlesys.xyz -> localhost:8080)
   ```

9. **Provision the guild** (only for a fresh/empty server): `venv\Scripts\python.exe setup_server.py --check` then without `--check`. See section 17 before running it against a server people have hand-edited.

---

## 6. Configuration

### `config.py` (bot process, repo root)

Loaded once at import time via `python-dotenv`. Values are read into module-level constants — most cogs import these constants directly rather than re-reading the environment. Raises `RuntimeError` immediately if `DISCORD_TOKEN` or `INTERNAL_API_SECRET` is empty.

### `.env` variables

| Variable | Read by | Default | Required? | Description |
|---|---|---|---|---|
| `DISCORD_TOKEN` | bot | `""` | **Yes** | Discord bot token |
| `DB_PATH` | bot | `bot.db` | No | Path to the bot's SQLite database |
| `LOG_LEVEL` | bot | `INFO` | No | Logging level |
| `DEBUG` | bot | `false` | No | Debug mode toggle |
| `UNVERIFIED_ROLE_ID` | bot | `0` | No | Role assigned to unverified members |
| `VERIFIED_ROLE_ID` | bot | `0` | No | Role assigned once verified |
| `UNVERIFIED_CATEGORY_ID` | bot | `0` | No | Category gating where `!verify` may run |
| `LOG_CHANNEL_ID` | bot | `0` | No | Moderation-log channel |
| `JOIN_LEAVE_CHANNEL_ID` | bot | `0` | No | Join/leave/ban-log channel |
| `CHECK_INTERVAL_MINUTES` | bot | `10` | No | Auto-kick scan interval |
| `AUTO_KICK` | bot | `true` | No | Enable the 7-day unverified auto-kick loop |
| `VERIFY_TEMPLATE_NAME` | bot | `verify` | No | Template used for the verification message |
| `VERIFY_TICKET_WELCOME_TEMPLATE_NAME` | bot | `verify-ticket-welcome` | No | Template for verify-ticket welcome messages |
| `TICKET_CHAT_LOGGING_ENABLED` | bot | `false` | No | Plain-text ticket chat capture (live-reloadable) |
| `TICKET_ARCHIVE_PURGE_DAYS` | bot | `30` | No | Days before archived tickets are purged (live-reloadable) |
| `ANTISPAM_ENABLED` | bot | `true` | No | Automatic spam detection on/off (`cogs/anti_spam.py`; live-reloadable, like every `ANTISPAM_*`) |
| `ANTISPAM_FLOOD_MESSAGES` / `ANTISPAM_FLOOD_SECONDS` | bot | `6` / `5` | No | Flood rule: this many messages from one member within this many seconds (0 messages = rule off) |
| `ANTISPAM_DUPLICATE_MESSAGES` / `ANTISPAM_DUPLICATE_SECONDS` | bot | `5` / `30` | No | Duplicate rule: the same text this many times within this many seconds, across channels (0 = off) |
| `ANTISPAM_MENTION_LIMIT` | bot | `6` | No | One message mentioning this many distinct users/roles is spam (0 = off) |
| `ANTISPAM_TIMEOUT_MINUTES` | bot | `10` | No | Timeout for a detected spammer (0 = delete only) |
| `MESSAGE_LOG_ENABLED` | bot | `true` | No | Moderation message log (`cogs/message_log.py`); `false` = the cog is not added |
| `MESSAGE_LOG_PATH` | bot | `message_log.db` | No | The message log's own SQLite file (not `bot.db`; sensitive, covered by `*.db` in `.gitignore`) |
| `MESSAGE_LOG_RETENTION_DAYS` | bot | `7` | No | Days a stored message is kept (1 to 30); pruned daily and at startup |
| `USER_CACHE_ENABLED` | bot | `true` | No | User cache (`cogs/user_cache.py`): remembered profiles of departed users; `false` = the cog is not added and resolve answers null for them again |
| `USER_CACHE_PATH` | bot | `user_cache.db` | No | The user cache's own SQLite file (not `bot.db`; sensitive, in `.gitignore`) |
| `USER_CACHE_RETENTION_DAYS` | bot | `365` | No | Days a departed user is kept (0 = forever; users still banned are never pruned); pruned daily and at startup |
| `OWNER_ID` | bot + dashboard | `0` | No | Bot/server owner Discord user ID — always treated as staff. The dashboard (`server/config.py`) also reads it for `require_owner`, the gate of the hidden owner-only Debug page (0 hides that page for everyone) |
| `STAFF_ROLE_IDS` | bot | `""` | No | Comma-separated default-staff role IDs |
| `INTERNAL_API_HOST` | bot + dashboard | `127.0.0.1` | No | Bind host for the embedded internal API |
| `INTERNAL_API_PORT` | bot + dashboard | `8766` | No | Bind port for the embedded internal API |
| `INTERNAL_API_SECRET` | bot + dashboard | `""` | **Yes (both)** | Shared bearer secret between the two processes |
| `DISCORD_CLIENT_ID` | dashboard | `""` | **Yes (dashboard)** | Discord OAuth2 client ID |
| `DISCORD_CLIENT_SECRET` | dashboard | `""` | **Yes (dashboard)** | Discord OAuth2 client secret |
| `MUSIC_COOKIES_FILE` | bot | `cookies.txt` | No | yt-dlp cookies file path |
| `MUSIC_NODE_PATH` | bot | auto: `shutil.which("node")` | No | Node.js binary for yt-dlp-ejs and for spawning the PO-token server. Blank/unset auto-detects from `PATH`; only set to override |
| `MUSIC_DEFAULT_SEARCH` | bot | `ytsearch7` | No | Default yt-dlp search prefix |
| `MUSIC_ALLOW_PLAYLISTS` | bot | `true` | No | Whether playlist URLs are allowed |
| `MUSIC_DEFAULT_VOLUME` | bot | `50` | No | Default playback volume (%) |
| `MUSIC_INACTIVITY_TIMEOUT` | bot | `600` | No | Seconds idle before auto-disconnect |
| `MUSIC_PLAYER_CLIENTS` | bot | `tv_simply,tv,web,web_safari` | No | Comma-separated yt-dlp YouTube player clients, tried in order |
| `MUSIC_POT_SERVER_ENABLED` | bot | `true` | No | Spawn/supervise the PO-token server from the bot process |
| `MUSIC_POT_SERVER_PORT` | bot | `4416` | No | Port the PO-token server listens on (loopback) |
| `MUSIC_POT_SERVER_SCRIPT` | bot | `pot_server/server/build/main.js` | No | Path to the built PO-token server entry point (relative paths resolve against the project root) |
| `MUSIC_POT_SERVER_HOST` | bot | `127.0.0.1` | No | Interface the PO-token server binds to (and yt-dlp connects to) |
| `MUSIC_POT_SERVER_NODE` | bot | blank | No | Node binary for the PO-token server only; blank falls back to `MUSIC_NODE_PATH` / `PATH` |
| `MUSIC_POT_SERVER_LOG` | bot | `logs/pot_server.log` | No | Server stdout/stderr log (appended, rotated at about 1 MB); a `.pid` file sits next to it |
| `MUSIC_POT_SERVER_WATCHDOG` | bot | `true` | No | Ping the server and restart it with exponential backoff if it stops responding |
| `MUSIC_POT_SERVER_HEALTH_INTERVAL` | bot | `30` | No | Watchdog ping interval, seconds |
| `MUSIC_POT_SERVER_MAX_RESTARTS` | bot | `5` | No | Consecutive failed restarts before the watchdog gives up |
| `MUSIC_EXTRACT_TIMEOUT` | bot | `90` | No | Seconds before a yt-dlp extraction is abandoned |
| `MUSIC_YTDLP_SOCKET_TIMEOUT` | bot | `20` | No | yt-dlp per-socket network timeout, seconds |
| `MUSIC_EJS_REMOTE_COMPONENTS` | bot | `true` | No | Pass `remote_components ejs:github` to yt-dlp |
| `MUSIC_AUTOPLAY_DEFAULT` | bot | `false` | No | Boot default for `!autoplay` |
| `MUSIC_AUTOPLAY_MAX_CONSECUTIVE` | bot | `10` | No | Max autoplay tracks in a row |
| `MUSIC_AUTOPLAY_HISTORY` | bot | `25` | No | Recently played video ids autoplay will not repeat |
| `MUSIC_COOKIE_LISTENER_ENABLED` | bot | `true` | No | Run the cookie-extension listener ([musicbot-cookie-sync](https://github.com/KeiraOMG0/musicbot-cookie-sync) API) |
| `MUSIC_COOKIE_LISTENER_SECRET` | bot | blank | No | Optional hardening: when set, `X-Cookie-Secret` is required; when blank, a browser-extension `Origin` is required |
| `MUSIC_COOKIE_LISTENER_EXTENSION_IDS` | bot | blank | No | Comma list of extension ids allowed when no secret is set |
| `MUSIC_COOKIE_LISTENER_HOST` / `_PORT` | bot | `127.0.0.1` / `8999` | No | Listener bind address (keep loopback) and port; 8999 is the extension's default, and only one program can hold it |
| `MUSIC_COOKIE_LISTENER_MAX_BYTES` / `_RATE_PER_MIN` | bot | `524288` / `30` | No | Request body cap and per-minute request limit |
| `MUSIC_DEBUGURL_MASK_PARAMS` | bot | blank | No | Extra query params `!debugurl` masks (comma list) |
| `MUSIC_DEBUGURL_BLOCKED` | bot | blank | No | Domains/keywords `!debugurl` refuses (comma list) |
| `APPEAL_BASE_URL` | bot | `https://dash.idlesys.xyz` | No | Public dashboard URL used to build the ban-appeal link in honeypot DMs (section 19) |
| `SUPERVISOR_WEBHOOK_URL` | `supervisor.py` | blank | No | Discord webhook for crash alerts (red, with ping) and info notices (requested restarts, blue, no ping). Blank = no alerts. Also the fallback for the weekly dependency audit |
| `DEPENDENCY_AUDIT_WEBHOOK_URL` | bot (`services/dependency_audit_service.py`) | blank (= `SUPERVISOR_WEBHOOK_URL`) | No | Webhook for the weekly dependency audit report |
| `DEPENDENCY_AUDIT_MIN_SEVERITY` | bot | `moderate` | No | Lowest npm severity that is reported (`critical`, `high`, `moderate`, `low`, `info`) |
| `DEPENDENCY_AUDIT_IGNORE` | bot | blank | No | Comma-separated package names never reported (e.g. a CLI-only advisory with no fix) |
| `SUPERVISOR_PING` | `supervisor.py` | `OWNER_ID` | No | Who crash alerts ping: a user id, or `&ROLEID` |
| `SUPERVISOR_TAIL_LINES` | `supervisor.py` | `15` | No | Output lines included in a crash alert |
| `SUPERVISOR_CONTROL_PORT` | `supervisor.py` + bot + dashboard | `8767` | No | Port of the supervisor's control API (always bound to `127.0.0.1`; section 26) |
| `SUPERVISOR_CONTROL_SECRET` | `supervisor.py` + bot + dashboard | falls back to `INTERNAL_API_SECRET` | No | Bearer secret of the control API. With neither set the supervisor runs without the control API |
| `SUPERVISOR_RESTART_COOLDOWN` | `supervisor.py` | `30` | No | Seconds before the same service can be restarted on request again (429 otherwise) |
| `FORTNITE_API_KEY` | bot | blank | No | Optional fortnite-api.com key, sent as the `Authorization` header. The `/v2/shop` endpoint works without one (Fortnite item shop section) |
| `FORTNITE_SHOP_LANGUAGE` | bot | `en` | No | `language` query param for item names |
| `FORTNITE_SHOP_POST_DELAY_MINUTES` | bot | `5` | No | Minutes after the 00:00 UTC shop rotation before the daily auto-post fires |
| `SETUP_GUILD_ID` | `setup_server.py` only | unset | No | Target guild when the bot is in more than one guild; auto-detected otherwise. The bot itself never reads it |
| `OLLAMA_URL` | bot (QOTD, `os.getenv`) | `http://localhost:11434` | No | Ollama base URL |
| `OLLAMA_MODEL` | bot (QOTD service) | `llama3.2:3b` | No | Default Ollama model (also selectable via `/qotd setmodel`) |
| `OLLAMA_EXE` | bot (QOTD cog) | unset | No | Path to the `ollama` executable (tried first when the QOTD service locates Ollama to launch it) |

QOTD timing note: each Ollama attempt has a 60s timeout (3 attempts, `keep_alive` 30m so the model stays loaded). The dashboard no longer waits on one long request: `POST /api/qotd/generate` starts a bot-side job and the page polls it (section 10, `cogs/qotd.py`), with the whole dashboard generation bounded at 150 s before it falls back (the old blocking call with a 200 s HTTP timeout could exceed Cloudflare's 100 s limit). Large models (e.g. `phi4:14b`) are slow on a cold load; prefer `llama3.2:3b` / `llama3.1:8b` for QOTD. The QOTD page's Generate tab shows an elapsed-seconds timer and an easing progress bar (`GenerateProgress` in `QotdPage.tsx`) while generating; it is indeterminate (caps at ~95%) because the real duration is unknown. Frontend built and typechecked; not yet verified live (requires bot + dashboard restart for the timeout fixes).
| `DISCORD_OAUTH_REDIRECT_URI` | dashboard | `http://localhost:8080/api/auth/callback` | No | Must match the Discord Developer Portal registration |
| `APPEAL_OAUTH_REDIRECT_URI` | dashboard | origin of `DISCORD_OAUTH_REDIRECT_URI` + `/api/public/appeal-auth/callback` | No | Redirect of the public appeal sign-in (section 19). Both `https://dash.idlesys.xyz/api/public/appeal-auth/callback` and `http://localhost:8080/api/public/appeal-auth/callback` were registered in the Developer Portal on 2026-10-28 |
| `DASHBOARD_HOST` | dashboard | `127.0.0.1` | No | Dashboard bind host |
| `DASHBOARD_PORT` | dashboard | `8080` | No | Dashboard bind port |
| `FRONTEND_BASE_URL` | dashboard | `http://localhost:8080` | No | Redirect target after OAuth callback |
| `SESSION_COOKIE_SECRET` | dashboard | `""` | **Yes (dashboard)** | Signs the opaque session-ID cookie |
| `GUILD_ID` | both | none | **Yes** | The one Discord server the bot works in (it leaves any other) and the dashboard is scoped to. Replaced the hardcoded ID on 2026-10-30 |
| `DASHBOARD_GUILD_ID` | dashboard | `GUILD_ID` | No | Overrides `GUILD_ID` for the dashboard only (normally unset) |
| `DASHBOARD_COOKIE_SECURE` | dashboard | `auto` | No | Secure flag on the session and OAuth-state cookies: `auto` (default, also when unset or unrecognised) sets it when the request came over HTTPS (`request.url.scheme`, or `X-Forwarded-Proto: https` from a loopback peer such as cloudflared) so `https://dash.idlesys.xyz` gets Secure cookies and `http://localhost` still works; `true` always; `false` never (`server/auth/oauth.py` `cookie_secure()`) |
| `DASHBOARD_API_DOCS` | dashboard | `false` | No | `true` re-enables FastAPI's `/docs`, `/redoc`, `/openapi.json` (they list every route, the hidden owner-only ones included); local development only |
| `DASHBOARD_SESSION_MAX_AGE_SECONDS` | dashboard | `2592000` (30 days) | No | Session sliding expiry (section 22) |
| `DASHBOARD_SESSION_TOUCH_INTERVAL_SECONDS` | dashboard | `300` | No | `touch_session` write throttle |
| `DASHBOARD_LIVE_ENABLED` | dashboard | `true` | No | Run the dashboard-to-bot upstream websocket client |
| `DASHBOARD_WS_REVALIDATE_SECONDS` | dashboard | `60` | No | Session and permission re-check per browser socket |
| `DASHBOARD_WS_BACKOFF_CAP_SECONDS` | dashboard | `15` | No | Upstream reconnect backoff cap |

`OLLAMA_*` are read directly with `os.getenv` in `cogs/qotd.py` / `services/qotd_service.py`, and `SETUP_GUILD_ID` in `setup_server/main.py`, not via either `config.py`. `.env.example` lists every variable above with placeholders/defaults only; keep real values out of it. Integer/boolean music-server values that fail to parse fall back to their default with a logged warning. The dashboard backend (`server/config.py`) raises `RuntimeError` at import if any of `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `INTERNAL_API_SECRET`, `SESSION_COOKIE_SECRET` is empty; the bot's `config.py` raises for `DISCORD_TOKEN` and `INTERNAL_API_SECRET`.

### Guild settings (`guild_settings` DB table + `services/settings_service.py`)

`SETTINGS_SCHEMA` is an explicit, typed allowlist of dashboard-editable fields (never credentials). `SettingsService` is the authority for validation; `server/routes/settings.py` mirrors the type/range rules (`_FIELD_RULES`, kept in sync by `test_settings_dashboard_routes.py`) so obvious mistakes fail before reaching the bot.

**How a saved value takes effect.**

| Mode | Behaviour |
|---|---|
| Live | `ticket_chat_logging_enabled`, `ticket_archive_purge_days`: `cogs/tickets.py` and `internal_api/routes/tickets.py` read them through `SettingsService.get_effective()` with a 5-second TTL cache that every save invalidates. Every `antispam_*` field: `cogs/anti_spam.py` reads them the same way (cached together as one `SpamRules` under the `antispam_rules` key). Applies within seconds. |
| On restart | Every other editable field. `main.py` calls `SettingsService.apply_to_config(ALLOWED_GUILD_ID)` after migrations and **before any cog is loaded**; it sets the matching `config.<ATTR>` to each valid saved value and logs `Settings: applied N dashboard override(s): key=value, ...`. Cogs keep reading `config.X` (most copy it into a module constant at import), so they see the saved values. The running process keeps its startup value until the next restart. |
| Read-only | `owner_id`: shown (`config.OWNER_ID`), never writable through the dashboard (422) and never applied from the DB, even if a stale row exists. |

Startup apply only checks type/range (the gateway is not connected yet); a malformed stored row is skipped with a warning and the `.env` value stays. `IMPORT_TIME_COPIES` lists the modules that copy settings at import (`cogs.kick`, `cogs.verify`, `cogs.moderation`, `services.ticket_service`); `apply_to_config` warns if any is already imported, and `tests/unit/test_settings_service.py` runs `main.py`'s pre-apply imports in a fresh interpreter to assert none is. Keep it that way: don't import those modules at the top of `main.py` or from anything `internal_api.app` imports at module level. The `.env` values are snapshotted before the apply, so "reset to default" and `default` still mean the `.env` value afterwards.

**Validation (all-or-nothing; nothing is written if any field fails).**

| Setting | Type | Rule | Mode |
|---|---|---|---|
| `unverified_role_id`, `verified_role_id` | snowflake (role) | exists in the guild, not @everyone, not managed, below the bot's top role, bot has Manage Roles; the two must differ | restart |
| `unverified_category_id` | snowflake (category) | exists and is a category | restart |
| `log_channel_id`, `join_leave_channel_id` | snowflake (text channel) | exists, is a text/news channel, bot has View Channel + Send Messages + Embed Links | restart |
| `check_interval_minutes` | int | 1 to 1440 | restart |
| `auto_kick` | bool | strict | restart |
| `verify_template_name`, `verify_ticket_welcome_template_name` | str (template) | 1 to 100 chars, a template with that name exists in `message_templates` | restart |
| `ticket_chat_logging_enabled` | bool | strict | live |
| `ticket_archive_purge_days` | int | 1 to 3650 (0 or less would purge every archived ticket) | live |
| `owner_id` | snowflake (user) | read-only | never |
| `music_default_search` | str | `ytsearchN` or `scsearchN`, N = 1 to 99 | restart |
| `music_allow_playlists` | bool | strict | restart |
| `music_default_volume` | int | 0 to 100 | restart |
| `music_inactivity_timeout` | int | 30 to 86400 seconds | restart |
| `antispam_enabled` | bool | strict | live |
| `antispam_flood_messages` | int | 0 (off) or 3 to 50 | live |
| `antispam_flood_seconds` | int | 2 to 60 | live |
| `antispam_duplicate_messages` | int | 0 (off) or 2 to 50 | live |
| `antispam_duplicate_seconds` | int | 5 to 600 | live |
| `antispam_mention_limit` | int | 0 (off) or 2 to 50 | live |
| `antispam_timeout_minutes` | int | 0 (delete only) to 40320 (28 days, `MAX_TIMEOUT_MINUTES`) | live |

"0 (off) or N to 50" is `OFF_OR_MIN` in `services/settings_service.py`, applied by `parse_value` (so stored rows too; a flood threshold of 1 would make every message spam) and mirrored as `off_or_min` in `_FIELD_RULES`. Strict bool means only `true`/`false`, `1`/`0` (JSON or strings, case-insensitive); the same parser reads stored rows, so a stored `"false"` is false and anything unparseable (e.g. `"yes"`) falls back to the `.env` default and is flagged `invalid_stored` in the GET response. Unknown keys are rejected ("Unknown setting."), not ignored. `null` on write resets a field to its `.env` default (the row is deleted). Writing the value already in effect is a no-op (no row, no audit).

**API.** `GET /api/settings` (and the internal `GET /guilds/{id}/settings`) returns `values`, `schema` (type, kind, min/max/pattern, `live`, `read_only`), `fields` (per key: `saved`, `running` = what the bot is using, `default`, `source` db/default/env, `pending_restart`, `invalid_stored`, `warning` when a saved ID or template no longer resolves), `pending_restart` (keys), `guild_available`, `applied_at` (epoch seconds when this bot process ran `apply_to_config()`, i.e. its start; null on a bot that predates the field; added 2026-10-28), and `options` (roles with `assignable`, categories, text channels with `bot_can_post`, template names) for the pickers. Snowflakes are strings. `PUT` takes `{values: {key: value|null}}` and returns the same report plus `changed`. Errors: 422 `{"error": "validation_failed", "message", "fields": {key: message}}`; 503 `{"error": "guild_unavailable"}` when an ID needs checking but the guild is not cached yet; 400 for an empty body; the dashboard maps a bot 403 to 409 `discord_forbidden` and only an unreachable bot to 502 `internal_api_unavailable`.

**Audit.** Every save that changes something writes one `audit_log` row: action `settings_update`, actor = the session user (`updated_by`, set by the dashboard from the session, never the body), `target_type` `settings`, detail `{key: {old, new}}` (plus `reset: true` for resets; snowflakes as strings). No secret is ever in the schema, so none can be logged.

**Page** (`/dashboard/settings`, `require_staff`): role/category/channel/template pickers built from `options` (roles the bot can't assign and channels it can't post in are disabled; a saved ID that no longer exists shows as "Unknown"), plain ID inputs with a resolved-name preview when the guild isn't loaded, number inputs with min/max, inline per-field errors (client-side mirror plus the bot's 422 `fields`), "Reset to default", Live / Applies on restart / View only / Pending restart badges, and a "N saved changes apply after a restart" banner with a **Restart bot now** button that calls `POST /api/supervisor/restart` (`useSupervisorRestart`, target `bot`) after a confirm, then polls `GET /api/settings` until nothing is pending or `applied_at` changes (up to 3 minutes). Pending changes survive reloads because they are derived from the bot's `saved` vs `running` values, not from browser state. **Save & restart bot** (2026-10-28) appears in the save bar whenever the draft touches a restart-required field: after a confirm it saves, then (if the saved report has anything pending) requests the restart in one go. Both restart paths send the reason `Settings changed by <username>: key1, key2` (capped at 300 chars; the supervisor also records `requested_by` from the session). Gates unchanged: the page is `require_staff`, the restart route `require_staff_even_if_bot_down`. Without supervisor.py the button reports that the bot must be restarted by hand. *State: built, frontend built (live now); the `applied_at` field needs a bot restart, until then the page falls back to the pending check.*

(`music_cookies_file`, `music_node_path`, `music_player_clients`, `STAFF_ROLE_IDS` (the trust anchor behind `require_staff`), and the `MUSIC_POT_SERVER_*` / `MUSIC_EXTRACT_*` values are **not** in `SETTINGS_SCHEMA`; they remain config/`.env`-only, never dashboard-editable.)

---

## 7. Database Documentation

### Two entirely separate databases

| | `bot.db` | `server/dashboard.db` |
|---|---|---|
| Access | Raw SQL via `aiosqlite` | SQLAlchemy 2.0 async ORM |
| Migrations | Hand-rolled runner, numbered `.sql` files | Real Alembic |
| Tracking table | `schema_migrations` | `alembic_version` |
| Tables | 29 through migration 24 (incl. `schema_migrations`), 32 with honeypot (migration 25) | 1 (`sessions`) |
| Owner process | Bot process only | Dashboard process only |

They are deliberately never cross-accessed — the dashboard backend reaches bot data exclusively through `internal_api` over HTTP, never by opening `bot.db` directly. `server/alembic/env.py`'s docstring states explicitly this Alembic setup "must never be pointed at bot.db."

### Bot DB migrations — how they're created and run

`main.py` builds a manifest list of `(version, name, sql_path)` tuples (`001_initial.sql` through `024_guild_settings.sql`, plus `025_honeypot.sql` registered as `(25, "honeypot", ...)`) and calls `run_migrations(db, MIGRATIONS)` **unconditionally on every startup**, before the bot logs in. The runner (`database/migrations/runner.py`):
1. Creates `schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)` if missing.
2. Reads already-applied versions into a set.
3. Sorts the manifest by version, skips any already applied.
4. For each new migration: reads the `.sql` file, runs it via `executescript()`, then inserts a `schema_migrations` row.

**Caveats (worth knowing before writing a new migration):**
- Each migration file runs through `executescript()` (which commits implicitly) and the `schema_migrations` row is inserted afterwards, so a failure partway through a file can leave some DDL applied without the version recorded. There is no atomic per-file transaction.
- `CREATE TABLE/INDEX IF NOT EXISTS` statements are safe to re-run, but **`ALTER TABLE ... ADD COLUMN` is not** (migrations 10, 11, 13, 14, 16, 19, 21, 23, 26 use it; 27 is CREATE-only and re-runnable). If one of those fails after the column was added, the retry on next start fails with "duplicate column name" and the bot cannot start until you fix `bot.db` by hand. Migration 15 (table rebuild) is likewise not re-runnable.
- There is no content-hash check: if a previously-applied migration file's contents change, the runner will not notice, so never edit an applied migration; add a new one.
- `run_migrations` is called before cogs load; a broken migration stops startup entirely.

**Creating a new migration:** add a new `NNN_description.sql` file under `database/migrations/sql/`, then append `(NNN, "description", path)` to the `MIGRATIONS` list in `main.py`. New migrations take effect on the next bot restart; no separate command.

### Migration list (bot.db)

| # | File | What it adds |
|---|---|---|
| 1 | `001_initial.sql` | `schema_migrations`, `guild_config` (legacy) |
| 2 | `002_templates.sql` | `message_templates`, `template_embeds`, `embed_fields`, `template_versions`, `audit_log` |
| 3 | `003_events.sql` | `event_bindings`, `event_binding_templates` |
| 4 | `004_managed.sql` | `managed_messages`, `template_components` |
| 5 | `005_tickets.sql` | `ticket_panels`, `ticket_panel_roles`, `tickets` |
| 6 | `006_advanced.sql` | `permission_grants`, `scheduled_sends`, `autorole_rules` |
| 7 | `007_reaction_roles.sql` | `reaction_role_panels`, `reaction_role_entries` |
| 8 | `008_seed_events.sql` | seeds welcome/boost templates and event bindings via `INSERT OR IGNORE`, hardcoded to the OLD guild (`1278422221613830178`) and old channel IDs, so on the current guild it inserts rows nothing references (see caveats) |
| 9 | `009_qotd.sql` | `qotd_config`, `qotd_history` |
| 10 | `010_qotd_role.sql` | `qotd_config.ping_role_id` |
| 11 | `011_qotd_staff_ping.sql` | `qotd_config.staff_ping_role_id` |
| 12 | `012_birthdays.sql` | `birthday_settings`, `birthdays` |
| 13 | `013_birthday_settings_v2.sql` | `birthday_settings.add_only`, `private_confirms` |
| 14 | `014_birthday_ping_user.sql` | `birthday_settings.ping_user` |
| 15 | `015_birthday_nullable_tz.sql` | rebuilds `birthday_settings` (via `birthday_settings_new`) so timezone is nullable |
| 16 | `016_qotd_approval_channel.sql` | `qotd_config.approval_channel_id` |
| 17 | `017_welcome_boost.sql` | `welcome_config`, `boost_config` |
| 18 | `018_warnings.sql` | `warnings` |
| 19 | `019_ticket_panel_purpose.sql` | `ticket_panels.purpose` (`support`/`verify`) |
| 20 | `020_ticket_panel_opened_by_index.sql` | index on tickets by opener |
| 21 | `021_event_bindings_audience.sql` | `event_bindings.audience` (default `channel`; only `channel` is used today) |
| 22 | `022_mod_actions.sql` | `mod_actions` |
| 23 | `023_ticket_claim_chatlog_purge.sql` | `tickets.control_message_id`, `tickets.archived_at`, `ticket_messages` |
| 24 | `024_guild_settings.sql` | `guild_settings` |
| 25 | `025_honeypot.sql` | `honeypot_config`, `honeypot_events`, `honeypot_appeals` (section 19) |
| 26 | `026_honeypot_appeal_verified.sql` | `honeypot_appeals.verified` (0 = filed without the DM token) |
| 27 | `027_fortnite_shop.sql` | `fortnite_shop_config` (daily post config + `last_posted_date` dedupe key + last error), `fortnite_wishlist` (per-member item watches, query stored lower-case, unique per user) |
| 28 | `028_honeypot_via_app.sql` | `honeypot_events.via_app_id`, `via_app_name`: the app that posted a caught message for the member (NULL for a normal message) |
| 29 | `029_honeypot_scramble.sql` | `honeypot_config.scramble_enabled` (default 0), `scramble_interval_hours` (default 6), `last_scrambled_at` (ISO UTC, NULL = never): periodic name scrambling (section 19) |
| 30 | `030_honeypot_appeal_outcomes.sql` | `honeypot_config.rejoin_invite_url`, `invite_in_removal_dm`; `honeypot_appeals.outcome_dm_sent`, `rejoined_at`, `verified_via` (backfilled `dm_link` for verified rows). From 30 on, `honeypot_appeals.token` holds `sha256:<hex>` of the link token; older raw tokens are hashed by the bot at startup (`HoneypotService.hash_legacy_tokens`, SQLite has no SHA-256) |
| 31 | `031_honeypot_test_cases.sql` | `honeypot_events.is_test`, `honeypot_appeals.is_test` (default 0): Debug-page appeal-flow test cases, kept out of the feed, stats and counts (section 25) |

(Column-level detail lives in the SQL files, which are the source of truth.)

### Dashboard DB migrations — Alembic

Real Alembic setup at `server/alembic/` (`alembic.ini` at `server/alembic.ini`, `script_location = server/alembic`). Must be invoked from the **repo root**, referencing the ini's location explicitly:
```
venv\Scripts\python.exe -m alembic -c server\alembic.ini revision --autogenerate -m "description"
venv\Scripts\python.exe -m alembic -c server\alembic.ini upgrade head
venv\Scripts\python.exe -m alembic -c server\alembic.ini downgrade -1
```
`env.py` overrides the ini's placeholder connection string with the real path computed from `server/config.py`, and imports `server/db/models.py` so Alembic's autogenerate can see the `Session` model.

Revisions: `77e8789ecc98` (create `sessions`), `c7e2a9d4b1f3` (2026-10-30: new table `error_reports` for the Debug page's Errors tab, additive; applied to the live `server/dashboard.db` on 2026-10-30, so the live DB is at head), `b3c1d2e4f5a6` (2026-10-14: the three Discord OAuth token columns become nullable via batch mode and every existing value is set to NULL; downgrade refills placeholders). Built and unit-tested against a temp DB; the live `server/dashboard.db` is still at `77e8789ecc98` until `alembic upgrade head` is run. Run it BEFORE restarting the dashboard onto the new code: the new code inserts NULL tokens, which the old NOT NULL columns reject (login would fail; existing sessions keep working).

### Backup process

**None exists.** No automated backup script, cron job, or documentation was found anywhere in the repo. Both databases are plain SQLite files on disk with no snapshot/rotation/export tooling. The only thing resembling a "backup" is the manual, staff-initiated template JSON export feature (`/template export`), which is unrelated to database backup. **This is a real gap** — see §16.

### Important tables (bot.db)

**Templates / rendering** — the central hub subsystem many features build on:

| Table | Key columns | Relationships |
|---|---|---|
| `message_templates` | `id` (PK), `guild_id`, `name`, `description`, `category`, `content` | Unique `(guild_id, name)`. Referenced by nearly every other feature table below. |
| `template_embeds` | `id` (PK), `template_id` (FK → message_templates, CASCADE), `position`, `title`, `description`, `color`, `footer_*`, `author_*`, `image_url`, `timestamp_mode` | |
| `embed_fields` | `id` (PK), `embed_id` (FK → template_embeds, CASCADE), `name`, `value`, `inline`, `position` | |
| `template_versions` | `id` (PK), `template_id` (FK, CASCADE), `version_number`, `snapshot_json`, `changed_by`, `change_summary` | Powers `/template history` and `/template rollback` |
| `template_components` | `id` (PK), `template_id` (FK, CASCADE), `component_type`, `label`, `style`, `custom_id`, `options_json`, `row_index` | Buttons/selects attached to a template |
| `managed_messages` | `id` (PK), `guild_id`, `channel_id`, `message_id` (UNIQUE), `template_id` (FK, CASCADE), `last_synced_at`, `sync_failed` | Tracks live messages kept in sync via `SyncQueue` |

**Tickets:**

| Table | Key columns | Relationships |
|---|---|---|
| `ticket_panels` | `id` (PK), `guild_id`, `channel_id`, `message_id`, `template_id` (FK → message_templates, SET NULL), `open_category_id`, `closed_category_id`, `transcript_channel_id`, `max_open_per_user`, `naming_pattern`, `purpose` (`support`\|`verify`) | |
| `ticket_panel_roles` | `id` (PK), `panel_id` (FK, CASCADE), `role_id`, `role_type` | Unique `(panel_id, role_id)` |
| `tickets` | `id` (PK), `guild_id`, `panel_id` (FK, CASCADE), `channel_id` (UNIQUE), `opened_by`, `claimed_by`, `status` (`open`\|`closed`\|`archived`), `ticket_number`, `control_message_id`, `archived_at` | Unique `(guild_id, panel_id, ticket_number)` |
| `ticket_messages` | `id` (PK), `ticket_id` (FK → tickets, CASCADE), `author_id`, `content`, `sent_at` | Opt-in plain-text chat log, text-only, never attachments |

**Moderation:**

| Table | Key columns | Notes |
|---|---|---|
| `warnings` | `id` (PK), `guild_id`, `user_id`, `moderator_id`, `reason` | System of record — dashboard add/remove UI keys off `warnings.id` |
| `mod_actions` | `id` (PK), `guild_id`, `user_id`, `moderator_id`, `action_type` (`kick`\|`ban`\|`unban`\|`timeout`\|`untimeout`\|`warn`\|`warning_removed`), `reason`, `duration_minutes`, `expires_at`, `extra_data` (JSON) | Unified typed history for dashboard queries; does not replace `audit_log` or `warnings` |
| `audit_log` | `id` (PK, autoincrement), `guild_id`, `actor_id` (0 = unknown), `action`, `target_type`, `target_id`, `detail_json`, `created_at` (naive UTC `YYYY-MM-DD HH:MM:SS`) | Generic append-only trail (index `idx_audit_guild` on `(guild_id, created_at)`); every moderation action writes here too. Read by the dashboard Audit log page and `/bot audit`; rows older than 365 days are pruned (see "Audit log") |

**Permissions / automation:**

| Table | Key columns | Notes |
|---|---|---|
| `permission_grants` | `id` (PK), `guild_id`, `role_id`, `permission`, `granted_by` | Unique `(guild_id, role_id, permission)` — see §8 |
| `scheduled_sends` | `id` (PK), `guild_id`, `channel_id`, `template_id` (FK, CASCADE), `cron_expr`, `next_run_at`, `enabled` | |
| `autorole_rules` | `id` (PK), `guild_id`, `trigger_type` (`join`\|`boost`\|`role_add`), `trigger_role_id`, `role_id`, `delay_seconds` | Unique `(guild_id, trigger_type, trigger_role_id, role_id)` |
| `event_bindings` | `id` (PK), `guild_id`, `event_type`, `channel_id`, `send_dm`, `audience` | Unique `(guild_id, event_type)` |
| `event_binding_templates` | `id` (PK), `binding_id` (FK, CASCADE), `template_id` (FK, CASCADE), `position` | Unique `(binding_id, template_id)` |

**Honeypot (migration 25):** `honeypot_config` (one row per guild), `honeypot_events` (one row per catch), `honeypot_appeals` (ban appeals with a unique token). See section 19.

**Other features:** `reaction_role_panels`/`reaction_role_entries`, `qotd_config`/`qotd_history`, `birthday_settings`/`birthdays`, `welcome_config`/`boost_config`, `guild_config` (legacy, mostly dead columns), `guild_settings` (the dashboard settings bridge, composite PK `(guild_id, key)`).

Full column-level schema for every table is preserved in the migration files themselves (`database/migrations/sql/001_initial.sql` through `025_honeypot.sql`) — this section summarizes; the SQL files are the source of truth.

### `server/dashboard.db` — SQLAlchemy models

Two models: **`ErrorReport`** (table `error_reports`, Alembic `c7e2a9d4b1f3`: browser crash reports and unhandled backend exceptions for the owner-only Errors tab; `source`, `kind`, `fingerprint` (indexed), `name`, `message`, `stack`, `component_stack`, `path`, `user_agent`, `user_id`, `username`, `created_at` (indexed); scrubbed, no IP, 30 days, see section 25) and **`Session`** (table `sessions`) — `session_id` (PK, String(64)), `discord_user_id` (BigInteger, indexed), `discord_username`, `discord_avatar`, `discord_access_token`, `discord_refresh_token`, `token_expires_at` (all three LEGACY and always NULL: the OAuth access token is used only inside the callback and never stored; Alembic revision `b3c1d2e4f5a6` made them nullable and nulled existing values), `is_guild_member` (cached at login, not re-checked per request), `cached_role_ids` (JSON), `created_at`, `last_seen_at`.

---

## 8. Permission System

This bot **completely bypasses native Discord permissions.** A repo-wide grep confirms zero uses of `guild_permissions`, `ADMINISTRATOR`, `MANAGE_GUILD`, or `commands.has_permissions` anywhere in `cogs/`/`services/`. Discord's built-in permission bits play no role in any authorization decision — everything runs through this bot-owned system instead.

### Live grants and role order (set 2026-10-30, applied through the internal API, audited as the owner)

Discord order of the staff roles, top to bottom: Owners, Kick-Bot (the bot), Bot, **Admin, Sr. Mod, Mod, Staff** (it used to be Staff, Mod, Sr. Mod, Admin, which made a Mod outrank an Admin in the moderation rank check `services/hierarchy.py`, since that compares top-role positions). The bot permission ladder is cumulative:

| Role | Bot permissions |
|---|---|
| Owners, Admin | everything (in `STAFF_ROLE_IDS`, staff bypass; Admin's 14 redundant grants were removed) |
| Sr. Mod | Mod + `kick_members`, `ban_members`, `manage_templates`, `manage_schedules` |
| Mod | Staff + `moderate_members` (warn/timeout), `manage_messages_bulk` (purge), `manage_tickets`, `view_audit`, `manage_events`, `manage_qotd`, `manage_reaction_roles`, `manage_birthdays` |
| Staff | `staff_base`, `verify_members`, `control_music` |

`manage_autorole` stays staff-only (it hands out roles automatically). Change grants on the Permissions page; this table records the state set on 2026-10-30, not a rule the code enforces.

### Permission vocabulary

A fixed, flat set of strings — `VALID_PERMISSIONS` in `services/permission_service.py`:

```python
VALID_PERMISSIONS = {
    "manage_templates", "manage_events", "manage_tickets", "manage_schedules",
    "manage_autorole", "manage_reaction_roles", "manage_qotd", "view_audit",
    "kick_members", "staff_base", "verify_members", "moderate_members",
    "ban_members", "manage_messages_bulk", "manage_birthdays",
}
```

No enum class, no built-in hierarchy. `manage_birthdays` (2026-10-28) gates the birthday management commands (`/config`, `/override`, `/birthdays export|import`) and the dashboard Birthdays page; before it those commands used `staff_base`, so roles that only hold `staff_base` must be granted `manage_birthdays` to keep using them. Other agents may add entries; `VALID_PERMISSIONS` is the source of truth.

**`control_music`** (added 2026-10-28, Music page, section 28): gates VIEWING and CONTROLLING the music player on the dashboard (`/dashboard/music`, `/api/music/*`, live topic `music`). The Discord music commands stay open to every member; staff bypass as everywhere. The staff half of `!musicstatus` and `!debugurl` stay staff-only on the dashboard (`require_staff`).

### Permission grants

Stored in `permission_grants(guild_id, role_id, permission, granted_by, granted_at)`, unique on `(guild_id, role_id, permission)`. CRUD lives in `PermissionRepository` (`grant`, `revoke`, `list_grants`, `get_role_permissions`, `has_permission`). This is exactly the data the dashboard's Permissions Matrix page edits.

**Example:** granting `manage_tickets` to a role via the dashboard performs `POST /api/permissions/grants` → `server/routes/permissions.py` (session check) → `internal_client.py` → `POST /guilds/{id}/permission-grants` → `internal_api/routes/permissions.py` → `PermissionRepository.grant(guild_id, role_id, "manage_tickets", granted_by)` → one row in `permission_grants`.

**Audit rows.** `PermissionService.grant`/`revoke` (the one path for the `/bot permissions` slash commands, the Permissions page and the Roles page) write an `audit_log` row when constructed with `audit=` (done in `main.py`): action `permission_grant` / `permission_revoke`, `target_type` `role`, `target_id` the role, detail `{permission}` (plus `unchanged: true` on a re-grant). Actor = `granted_by` for grants and the `actor_id` argument for revokes; the dashboard passes the session user. A revoke without `actor_id` records actor `0` ("Unknown" on the page); every current caller passes it (the dashboard Permissions page, the `/bot permissions revoke` slash command, and the Roles page's permission sync and role delete in `services/role_admin_service.py`). An invalid grant or a revoke of a grant that did not exist writes nothing; an audit failure is logged and never fails the change.

### Staff bypass

Two distinct, deliberately separated trust tiers:

1. **`DEFAULT_STAFF_ROLE_IDS`** (= `config.STAFF_ROLE_IDS`) plus `OWNER_ID`. `is_staff(member)` returns `True` immediately if the member is the owner or holds any default-staff role — this **bypasses the grants table entirely**, no DB query needed.
2. **Fine-grained grants** (`permission_grants` table) — additive on top, checked only if the member isn't already staff.

The dashboard marks each role `"is_default_staff": role.id in DEFAULT_STAFF_ROLE_IDS` so the Permissions Matrix can visually distinguish these rows.

**Why the split exists — self-escalation prevention.** The grants-management page itself (`/bot permissions grant|revoke|list` and the dashboard's Permissions page) is gated on `is_staff_interaction()`, **never** on `has_permission()`. If it were gated on a revocable fine-grained permission, a role holding that permission could grant itself (or any role) unlimited further permissions — a real self-escalation vulnerability. `is_staff()` cannot be escalated via the grants table at all; it is a hard floor defined only by `config.py`/`.env`.

### Dashboard authorization flow

Every dashboard request that needs authorization goes through `server/permissions/guards.py`:
- **`require_permission(perm)`** — calls `internal_client.check_permission(guild_id, discord_user_id, perm)` → `POST /guilds/{id}/permissions/check` (internal_api) → `internal_api/deps.py` fetches the **live** `discord.Member` object (never trusts `session.cached_role_ids`) → `bot.permission_service.has_permission(member, perm)`. This re-derivation on every single request means a role change in Discord takes effect on the dashboard immediately, with no session refresh needed.
- **`require_any_bot_permission(perms)`** (2026-10-28) — passes if the user holds ANY of `perms` (staff hold all), in ONE `check_permissions_bulk` call; same member-gone 403 `not_a_member` / bot-down 502 rules as `require_permission`, no cached fallback. Used for read-only metadata every page needs: `/api/resolve` and `GET /api/user-cache/{id}` pass the full list (`server/routes/me.py` `_ALL_PERMISSIONS`). `require_any_permission(*perms)` (2026-10-28) checks a few permissions one by one (stops at the first grant; 403 lists them in `any_of`); used by `GET /api/moderation/members/search` and `/history/{id}` (moderate/kick/ban).
- **`require_staff`** — the stricter variant, calling `is_staff()` instead. Used for the Permissions page and the Settings page (since Settings can change security-relevant config, gating it on a grantable permission would be circular).
- **`require_owner`** — only for the hidden Debug page (`server/routes/debug.py`), `POST /api/supervisor/rotate-logs` and `POST /api/supervisor/update-deps`. Compares the session's Discord user ID with `OWNER_ID` from the root `.env` (no bot round trip) and answers **404 `{"error":"not_found"}`** to everyone else, including staff and logged-out visitors, so the page does not appear to exist. `/api/me` exposes the same check as `is_owner`. The 404 is the same whatever the method or body: the debug router uses it as a router-level dependency, hidden POST routes read their body raw and validate it only after the check (`guards.owner_body`; FastAPI would otherwise parse a declared body first and 422 for everyone), and `server/app.py` registers, LAST, an all-methods `/api/{rest:path}` catch-all that answers every unmatched `/api/*` request (unknown path, wrong method, trailing slash) with the same bare 404, so there is no 405/`Allow` header or 307 redirect to give a hidden route away.
- **Bot answers vs bot down.** `guards.bot_unreachable(exc)` is true only for no HTTP answer, a 5xx, or 404 "Guild ... not found" (guild not cached yet); `guards.member_gone(exc)` is 404 "Member ... not found" (left/kicked/banned since login; `internal_client.check_permission`/`check_permissions_bulk`/`check_is_staff` keep the internal API's `detail` for this). `require_permission` answers member-gone with 403 `not_a_member` and `require_staff` with 403 `forbidden` (both `from` the `InternalAPIError`), never the "bot offline" 502; `/api/me` answers member-gone with 403 `not_a_member` and keeps the transient 502 for everything else. Cached-role fallbacks (`guards.cached_staff`, diagnostics' `require_staff_even_if_bot_down`, the live hub) apply only when `bot_unreachable`.

**End-to-end check flow (`PermissionService.has_permission`):**
```python
def has_permission(member, permission):
    if is_staff(member):          # owner or default-staff role → always True
        return True
    role_ids = [r.id for r in member.roles]
    return repo.has_permission(guild.id, role_ids, permission)  # SQL IN query
```

### Known quirks in the permission wiring

- `server/routes/me.py` keeps its own copy of the 14 permission strings (`_ALL_PERMISSIONS`) because `server/` may not import the bot's packages. Adding a permission means editing that list too (see section 14), or `/api/me` will never report it. `tests/unit/test_permission_list_parity.py` fails when `_ALL_PERMISSIONS`, the `/bot permissions` choices or the frontend `PERMISSION_LABELS` keys (`PermissionsPage.tsx`, parsed as text) drift from `VALID_PERMISSIONS`. The `/bot permissions grant|revoke` choices are built from `VALID_PERMISSIONS` (`PERMISSION_CHOICES` in `cogs/advanced_commands.py`), so they can no longer drift.
- **Self-assignable roles can't carry bot power.** A role that is in `STAFF_ROLE_IDS` or has ANY row in `permission_grants` (or carries a Discord moderator permission, see `services/hierarchy.DANGEROUS_PERMISSIONS`) can't be put on a self-role panel or an autorole rule, and is refused again at click/grant time (`services/self_role_policy.py`, section 10 `cogs/reaction_roles.py`). Otherwise a role holding `manage_reaction_roles` could make a fully-granted role self-assignable and click it.
- Fixed (frontend, needs a frontend build): the staff-only nav items (Diagnostics, Roles, Permissions, Settings), the palette's Settings-tab and "Grant permission" entries and the Overview quick action/Diagnostics link are now keyed on `/api/me` `is_staff` (the `__staff_only` pseudo-permission in `src/lib/owner.ts`, set by `AuthContext`; missing `is_staff` = false), not on the grantable `staff_base`, so a role holding only `staff_base` no longer sees links that would 403.

### Relationship to native Discord permissions

None. `ADMINISTRATOR`, `MANAGE_GUILD`, etc. are never consulted. A member with the Discord "Administrator" permission bit but no bot-level staff role or grant is treated as a regular member by this system.

### Inheritance

Flat — no permission implies another (e.g. holding `ban_members` does not imply `kick_members`). The only "inheritance" is the universal staff/owner bypass, which functions as an all-permissions override rather than a hierarchy between named permissions.

---

## 9. Dashboard

### React frontend

Router: `react-router-dom`'s `<Routes>/<Route>` (not the newer data-router API), wrapped in `<BrowserRouter>`. Route tree in `src/app/routes.tsx`. Public, shell-less routes (`/`, `/status`, `/appeal[/:token]`, `/terms`, `/hub`, `/privacy`; the newer public pages share `components/layout/PublicShell.tsx`: 56px header, centred column, `PublicFooter` with Member hub / Terms / Privacy / Status / Appeal links, also rendered at the bottom of `/status` and `/appeal`; `/` has its own Terms of Service / Privacy Policy link row) plus a protected `/dashboard` layout route wrapping `RequireAuth > AppShell > PageTransition > Suspense > Outlet`. Every nested page except `HomePage` is `React.lazy`-loaded (its own chunk).

| Route | Page | Required permission |
|---|---|---|
| `/` | `HomePage` | none (public landing/login) |
| `/status` | `StatusPage` | none (public; polls `GET /api/status` every 10s so it works while other things are down) |
| `/terms` | `TermsPage` (public Terms of Service, the URL for the Discord Developer Portal's "Terms of Service URL": `https://dash.idlesys.xyz/terms`; text in `pages/terms/termsContent.tsx` with `TERMS_LAST_UPDATED` and a `CONTACT_EXTRA` placeholder (null: no email exists, contact is a ticket or the server owner on Discord); table of contents with `#section` anchors, re-scrolled after the lazy chunk loads) | none (public, shell-less, no API calls of its own) |
| `/hub` | `HubPage` (member hub for regular members, section 31: a sign-in gate when signed out, then the person's own profile, standing, self-roles, birthdays, tickets, events, QOTD and music, or their trap-ban / ban / not-a-member state) | Discord sign-in required (`GET /api/hub`, any session; the bot decides membership live). Shell-less (`PublicShell`). Linked from `/` (members without any bot permission, and people who aren't in the server, are sent here after sign-in), `Forbidden403` and the profile menu ("Member hub") |
| `/privacy` | `PrivacyPage` (public Privacy Policy, the URL for the Discord Developer Portal's "Privacy Policy URL": `https://dash.idlesys.xyz/privacy`; same layout as `/terms` (`PublicShell`, table of contents with `#section` anchors). Text in `pages/privacy/privacyContent.tsx` with `PRIVACY_LAST_UPDATED` (2026-10-28), a `CONTACT_EXTRA` placeholder (null: no email exists; contact is a ticket or the server owner on Discord) and a retention table. Every statement was checked against the code; item -> source: message log `services/message_log_service.py` + `config.MESSAGE_LOG_RETENTION_DAYS` (7); activity log incl. deleted/edited message text (500 chars, `cogs/gateway.py`), voice/nickname/invite logging (`cogs/logging_*.py`), `services/audit_service.py` `AUDIT_RETENTION_DAYS` (365); user cache `services/user_cache_service.py` + `USER_CACHE_RETENTION_DAYS` (365 after leaving, banned users kept); warnings/mod_actions/`kicks.log`/honeypot events+appeals (no automatic deletion); tickets + `ticket_messages` (`ticket_chat_logging_enabled`, purge `TICKET_ARCHIVE_PURGE_DAYS` 30); birthdays (month/day + optional tz, removable by `/birthday remove`); `fortnite_wishlist`; dashboard sessions (`server/auth/*`, scopes `identify guilds.members.read`, token not stored, 30-day sliding); appeal sign-in (`server/routes/appeal_auth.py`, token revoked, cookies `appeal_identity` 30 min / `appeal_oauth_state` 10 min); uvicorn access log keeps client IPs (cloudflared X-Forwarded-For) in `logs/supervisor/dashboard.log`, rotated weekly, archives deleted after 28 days; QOTD uses a local Ollama model with no member data. When any of these change, update the content and `PRIVACY_LAST_UPDATED`) | none (public, shell-less, no API calls of its own) |
| `/dashboard` | `DashboardPage` (Overview) | any dashboard member (the stats call needs `staff_base`) |
| `/dashboard/moderation` | `ModerationPage` (header actions "Terms of Service" / "Privacy Policy" open `/terms` / `/privacy` in a new tab, `pages/moderation/PolicyLinks.tsx`; plain links, not disabled while the bot is offline) | any of `moderate_members` / `kick_members` / `ban_members` (2026-10-28; nav uses the `__moderation_any` pseudo-permission from `src/lib/owner.ts`). Sections and buttons follow their own gate: Recently warned, the Warnings tab, Warn and Timeout need `moderate_members` (inline note otherwise), Kick `kick_members`, Ban/Unban `ban_members`; member search and history accept any of the three; "Open profile" shows with `staff_base` |
| `/dashboard/members` | `MembersPage` (search by name, or paste any user ID incl. departed users) | `staff_base`; nav "Members" (Moderation group) |
| `/dashboard/members/:userId` | `MemberProfilePage` (richer `/userinfo`: account created, joined, boosting, roles, timeout, verified state, ban state, last known name/avatar + departure from the user cache (`useCachedUser`) for departed users; tabs Warnings / Mod actions / Honeypot; actions Warn, Timeout/Remove timeout, Kick, Ban, Unban (only when banned/unknown and not a member), Verify (channel of the Unverified category), "DM me their messages" (`!msglog`), "Forget cached profile" (departed only); live on the `moderation` topic for `moderate_members` holders, else polls 30 s) | `staff_base`; each action its own gate (warn/timeout/remove warning `moderate_members`, kick `kick_members`, ban/unban `ban_members`, verify `verify_members`, DM export + Forget `require_staff`); withheld record sections show an inline note |
| `/dashboard/message-tools` | `ModToolsPage` (Purge card = `!purge`: channel, count 1-1000 or window 30s..14d, optional user ID, confirm dialog above 100 / for any window, job polled every 1.5 s; Message log card = `!msglog`: status + "Send to my DMs" for a user, channel or deleted messages, never a download) | `manage_messages_bulk` (nav "Message tools"); the Message log card only for `is_staff` |
| `/dashboard/tickets`, `/dashboard/tickets/:ticketId`, `/dashboard/tickets/panels/new` | `TicketsPage`, `TicketDetailPage`, `TicketPanelBuilderPage` | `manage_tickets` |
| `/dashboard/permissions` | `PermissionsPage` (matrix labels synced with the 14 `VALID_PERMISSIONS`; every grant confirms, the sensitive ones need typing GRANT: `ban_members`, `kick_members`, `moderate_members`, `manage_messages_bulk`, `verify_members`, `manage_autorole`, `manage_reaction_roles`, `view_audit`, `staff_base`) | server: `require_staff`; nav link keyed on `/api/me` `is_staff` |
| `/dashboard/self-roles` | `SelfRolesPage` (live role list via the `roles` topic; "Refresh roles" manual fallback; "Deleted role" badges on entries whose role is gone and a per-panel count; integration-managed roles disabled in the picker) | `manage_reaction_roles` |
| `/dashboard/templates`, `/dashboard/templates/:templateId` | `TemplatesPage` (+ Import dialog, `ImportTemplateDialog.tsx`), `TemplateEditorPage` (+ `TemplateToolsCard.tsx`: bot-rendered preview, Export JSON download, version history with Restore = rollback; Restore is disabled while the editor has unsaved edits; `PlaceholdersPanel.tsx`: every placeholder with the bot's example value, click to insert, see "Template placeholders") | `manage_templates` |
| `/dashboard/qotd` | `QotdPage` | `manage_qotd` |
| `/dashboard/settings` | `SettingsPage` | server: `require_staff`; nav link keyed on `/api/me` `is_staff` |
| `/dashboard/roles` | `RolesPage` (Discord role manager: create/edit/reorder/duplicate/delete roles, members, internal bot grants; section 23) | server: `require_staff`; nav link ("System" group) keyed on `/api/me` `is_staff` |
| `/dashboard/birthdays` | `BirthdaysPage` (tabs: Birthdays = status tiles, recent & upcoming (-7..+14 days, server timezone), searchable/sortable full list with `ResolvedUser`, add/edit (member search, month/day incl. Feb 29, member timezone or the server's) and remove; Settings = role picker (options the self-role policy blocks for you are disabled with the reason), channel, server timezone, the four toggles, single/plural message editors with counters, placeholder help, `%e` staff-only and hardcoded-mention warnings, one SaveBar, per-field 422 errors; Tools = test announcement (up to 5 members, default you), reset today's timers (confirm dialog), export .txt/.csv/.ics (browser download), import .txt with a dry-run preview then Import. Live on `birthdays`. built and tested 2026-10-28, NOT live until the bot and dashboard restart (the frontend page is in the built `dist/` and shows an error state until then)) | `manage_birthdays`; nav link "Birthdays" in "Community" |
| `/dashboard/automation` | `AutomationPage` (`src/pages/automation/`; tabs Events, Welcome & Boost, Schedules, Autoroles, Managed messages, `?tab=` deep link; one `automation` live subscription for the page; see section 30) | per tab: `manage_events` / `staff_base` (same as the cogs) / `manage_schedules` / `manage_autorole` / `manage_templates`; a tab the viewer lacks renders an inline 403 without a request; nav link in "Community" shown with ANY of them (`AUTOMATION_ANY_PERMISSION` pseudo-permission set in `AuthContext`, `lib/owner.ts`). built and tested 2026-10-28, NOT live until the bot and dashboard restart (the page is in the built `dist/` and shows error states until then) |
| `/dashboard/fortnite` | `FortniteShopPage` (daily shop channel, on/off, ping role, Jam Tracks toggle, last posted / next post / last error, Post now; see "Fortnite item shop") | `manage_schedules`; nav link in "Community" |
| `/dashboard/music` | `MusicPage` (now playing + progress, pause/resume/skip/stop/previous, volume, loop, autoplay, add by URL or search, queue reorder/remove/shuffle/clear, voice channel join/move/leave, playlists, `!musicstatus` card; staff also get the status details and a `!debugurl` card; section 28) | `control_music`; nav link in "Community" |
| `/dashboard/audit` | `AuditLogPage` (read-only bot audit log: filters, expandable detail, "Load more", live; see "Audit log") | `view_audit`; nav link "Audit log" in "System" |
| `/admin` | redirect to `/dashboard/debug?tab=errors` (the owner's short link to the Errors tab) | none itself; the target is owner-only (404 for everyone else) |
| `/dashboard/debug` | `DebugPage` (HIDDEN owner-only test bench, tabs Actions / Lab / Errors; see "Owner-only Debug page") | server: `require_owner` (404 for everyone else); nav link ("System") only when `/api/me` `is_owner`; for non-owners the route is wrapped in `OwnerOnlyRoute`, which renders the same catch-all `NotFound404` as an unknown path (chunk not loaded, no API call, breadcrumb "Dashboard / Not found") |
| `/dashboard/*` (unknown) | `NotFound404` (catch-all, inside the shell) | any dashboard member |
| any other unknown URL | `NotFound404 standalone` (catch-all, no shell, links home) | none |

**Crash screen and error reports (crash screen 2026-10-29; reporting 2026-10-30, frontend live, backend live after the next dashboard + bot restart):** `src/components/AppErrorBoundary.tsx` wraps the whole app in `main.tsx`, outside every provider. Any render error or failed lazy-chunk import used to unmount the tree and leave a blank page (reported from a phone in Discord's in-app browser while desktop browsers loaded fine; the built assets were verified to be served correctly, so the cause was on the device). It shows "The dashboard couldn't load" with the error name and message, the first stack lines, the path and the user agent, plus Reload and Bot status buttons. **It never reloads by itself** (the one-time auto-reload of 2026-10-29 was removed at the owner's request): it sends the error to `POST /api/public/client-errors` via `src/lib/errorReport.ts` and says "This error was reported automatically"; a failed chunk (an old tab after a rebuild) gets the wording "The dashboard was updated ... press Reload". `installGlobalErrorReporting()` (called in `main.tsx`) also reports window `error`, `unhandledrejection` and Vite `vite:preloadError` events (not prevented, so the import still fails into the boundary). The reporter is dependency-free, never throws, sends each distinct error once and at most 10 per page load, skips noise (AbortError, ResizeObserver, cross-origin "Script error.", browser-extension stacks, API errors below 500 and 502/503/504), and sends the path without its query string, with `/appeal/<token>` shown as `/appeal/:token`. Reports appear on the owner-only Debug page's Errors tab (section 25).

**Caching (2026-10-30; built and tested, live after the next dashboard restart):** `server/app.py` sets `Cache-Control` on everything it serves from `dist/`: `/assets/*` (content-hashed Vite output, `_HashedAssets` subclass of `StaticFiles`) `public, max-age=31536000, immutable`, so browsers and the Cloudflare edge keep them a year; a missing asset is a plain 404 with `no-store` (never cached); `index.html` (SPA fallback and `/index.html`) `no-cache`, so every load checks for a new build and nobody keeps an old `index.html` that points at deleted chunks; other files at the dist root (favicon etc.) `public, max-age=86400`. Cloudflare compresses (brotli/gzip) at the edge, so the backend adds no compression middleware.
| `/honeypot`, `/appeal`, `/appeal/:token` | Honeypot management page and public appeal page | see section 19 for what has landed |

**Design system (redesign, `docs/DESIGN_SYSTEM.md`).** Tokens (CSS variables on `:root` light / `.dark` dark) live in `src/styles/globals.css` and are exposed to Tailwind via `@theme inline`; components use tokens, never raw colours. Fonts are Geist / Geist Mono self-hosted through npm. Theme = `localStorage["kb-theme"]` (`light|dark|system`), applied before first paint by an inline script in `index.html` and managed by `ThemeContext` (toggle in the profile menu, palette action "Switch theme"). Per-browser prefs (table density, collapsed sidebar, last tab, recent pages) are in localStorage behind try/catch (`src/lib/storage.ts`, `PrefsContext`).

**Layout and nav.** `AppShell`: skip link, sidebar (248px / 64px rail / phone drawer), 56px header (breadcrumbs, Ctrl+K palette trigger, profile menu; the "Live" pill next to the profile menu was removed on 2026-10-14), connection banners, phone bottom nav. `NAV_LINKS` (single source for sidebar, drawer, bottom nav, breadcrumbs, palette, `g` shortcuts) has four groups: overview, moderation, community, system; groups with no permitted item are not rendered. The old `LiveIndicator` / `StatusIndicator` components were replaced by `SidebarLiveRows` (sidebar footer, in `components/patterns/LivePill.tsx`; the header `LivePill` component was removed, the `LiveBanners` still announce reconnecting/offline), driven by `useLive()` / `useStatus()`; a `useBotOffline()` hook disables Discord-bound actions while the bot is unreachable. Sidebar count badges subscribe to the `tickets` / `honeypot` topics through the existing ref-counted `useLiveTopic` (same socket).

**Component inventory.** `components/ui/`: button (`destructive-outline`, `loading`), badge, card, input/textarea/select/label, field (+CharCounter), alert, table (+SortableHead), tabs (underline), segmented, checkbox, switch, dropdown-menu, tooltip, sheet, progress, kbd, toast (Undo action), dialog, alert-dialog, skeleton, avatar, copy-button. `components/patterns/`: PageHeader/PageContainer, states (empty/error/skeletons), StatCard, ChartCard/ChartTooltip, SaveBar, ConfirmDialog, DensityToggle, StaffOnlyBadge, LivePill.tsx (`SidebarLiveRows` + `useLiveView`). `components/embed/`: EmbedPreview (themed with `--embed-*`), DiscordMessage/DiscordButton.

The Vite dev server (`npm run dev`, port 5173) proxies `/api` (incl. the websocket) to `http://127.0.0.1:8080` (override with `VITE_API_TARGET`), so the dashboard backend must be running for the dev frontend to work.

**Frontend behaviour notes (2026-10-14 fixes; built, typechecked, live since the 2026-10-14 build).** Unsaved changes: ONE guard, `useUnsavedChangesGuard(dirty, message?)` in `src/hooks/useUnsavedChangesGuard.ts` (BrowserRouter has no `useBlocker`): `beforeunload`, a capture-phase click interceptor for in-app links, `guardedNavigate()` for the command palette and `g` hotkeys, and a capture-phase `popstate` listener that undoes Back/Forward (using React Router's `history.state.idx`) and asks; one styled dialog `UnsavedChangesDialog` mounted in `AppShell`; only pathname changes are guarded. `SaveBar` guards automatically; the template editor, ticket panel builder and Settings call it too. Errors: every page uses `describeApiError` (`src/lib/apiError.ts`). `apiFetch` forwards react-query's `signal` (query functions pass `({ signal })`) and re-throws an abort as the original `AbortError`, never as an `ApiError`, so the stay-logged-in rule is unchanged. Live handlers that several components share (`tickets`, `honeypot` appeal events) invalidate through `invalidateSoon` (`src/live/invalidate.ts`, 50 ms coalescing, prefix de-dupe) so one event is one refetch. `LiveCaption` (`components/patterns/LivePill.tsx`) shows "Updates live" only while the socket is connected. Tickets: list and detail poll every 30 s only while the socket is down; the open ticket's chat log polls every 10 s (the bot publishes no per-message `tickets` event); search matches opener/claimer display names and usernames via the shared resolve cache (`useResolvedUsersMap`). Name resolution (`useResolve.ts`): a failed `/api/resolve` call is no longer cached as "not found" (it rejects and retries; components show the raw id); only an explicit `null` is "(user left)", re-checked after 60 s. Bot side (`internal_api/routes/resolve.py`), `null` means `fetch_member` answered NotFound; a transient Discord error leaves the id out of the response, which the frontend retries. Overview stat tiles keep previous data (`placeholderData`, 30 min `gcTime`, partial live pushes ignored) and show skeletons only on first load. Honeypot settings keep a dirty draft through live config updates and show "Changed elsewhere" with Load latest; Deny on an appeal confirms first. The panel builder disables Create while posting and confirms before posting the same panel again or into a channel that already has one. The role members dialog clamps its page when members leave.

`requiredPermission` on `NAV_LINKS` only hides nav entries client-side for UX — it is not a security boundary; real enforcement is server-side via `require_permission`/`require_staff`.

### Authentication (frontend perspective)

`AuthContext` derives a state machine (`"loading" | "unauthenticated" | "not_a_member" | "authenticated"`) purely from one `GET /api/me` call. `RequireAuth` acts on that state: loading shows a skeleton, unauthenticated redirects to `/`, `not_a_member` renders `Forbidden403` in place, authenticated renders the page. Login is a plain `<a href="/api/auth/login">` (real navigation, not fetch, since it 302s to Discord). No global 401 interceptor — the app reactively re-derives state from the next `useMe()` call.

### Discord OAuth flow

`server/auth/oauth.py`:
1. `GET /api/auth/login[?next=/hub]` — sets a short-lived `SameSite=Lax` httpOnly `state` cookie, 302s to Discord's authorize URL. Scopes: `identify guilds.members.read`. `next` must be a key of `LOGIN_NEXT_PATHS` (today only `/hub`); it is kept as a short key in the `oauth_next` cookie (path `/api/auth`, 10 min) and the callback redirects to `FRONTEND_BASE_URL` + that path, else to `FRONTEND_BASE_URL`. Anything else is ignored (and a stale `oauth_next` cleared), so it can never be an open redirect.
2. `GET /api/auth/callback` — validates `state` (`secrets.compare_digest`), exchanges the code, fetches the user's identity + guild membership/roles, creates a server-side `Session` row, sets an httpOnly session cookie holding only `sign_session_id(session_id)` (itsdangerous-signed opaque ID — **not** a JWT, no claims client-side), 302s to `FRONTEND_BASE_URL`.
3. `POST /api/auth/logout` — deletes the session server-side (idempotent) and clears the cookie.
4. `POST /api/auth/logout-all` — "log out everywhere": deletes every session of the current Discord user, closes their live sockets (4401), clears this cookie; `{ok, sessions_ended}`. Idempotent like logout. (Backend only; no frontend button yet.)

The Discord access token is used only inside the callback (identity + member lookup) and is not stored; the refresh token is ignored. Cookies get `Secure` per `DASHBOARD_COOKIE_SECURE` (default `auto`, see the env table).

### Internal API communication

`server/services/internal_client.py` — a plain `httpx.AsyncClient` per call, base URL `http://127.0.0.1:8766`, every request carrying `Authorization: Bearer {INTERNAL_API_SECRET}`. Every `server/routes/*.py` module is a thin wrapper: check permission/staff → call the matching `internal_client` method → translate `InternalAPIError` into a `502 {"error": "internal_api_unavailable"}` if the bot process is down. **One deliberate exception:** `server/routes/supervisor.py` calls `supervisor.py`'s loopback control API directly through `server/services/supervisor_client.py` (httpx, same bearer pattern), so the restart controls work while the bot is down or hung (section 26).

### API routes (dashboard backend, `server/routes/*.py`)

All under `/api` unless noted. "staff" means `require_staff`; "session" means a logged-in guild member; "public" needs no login. Every gated route re-checks against the live bot cache on each request.

| Module | Endpoints | Gate |
|---|---|---|
| `auth/oauth.py` | `GET /api/auth/login`, `GET /api/auth/callback` (both `10/minute` slowapi limit), `POST /api/auth/logout`, `POST /api/auth/logout-all` (deletes all of the user's sessions) | public |
| `me.py` | `GET /api/me` (includes `is_owner` and `is_staff`: owner or `STAFF_ROLE_IDS`, the `require_staff` check, false when the is-staff call fails) | session (401 unauthenticated, 403 `not_a_member`, also when the bot answers "Member not found"; 502 only when the bot cannot answer) |
| `status.py` | `GET /api/status` | public |
| (app) | `GET /health` (not under `/api`) | public |
| `stats.py` | `GET /api/dashboard/stats` | perm `staff_base` |
| `stats.py` | `GET /api/dashboard/server-info` (the `/serverinfo` facts: owner, created, boosts/tier, channel/category/role/emoji counts, verification level; Overview "Server" card) | perm `staff_base` |
| `moderation.py` | `GET /api/moderation/warnings/recent?limit=1..200`, `GET /api/moderation/warnings/{user_id}`, `POST /api/moderation/warnings`, `DELETE /api/moderation/warnings/{id}?user_id=`, `GET /api/moderation/history/{user_id}`, `GET /api/moderation/members/search?q=(max 100)&limit=1..25`, `POST /api/moderation/timeout` (`duration_minutes` 1..40320), `POST /api/moderation/untimeout` | perm `moderate_members`. Actor (incl. who removed a warning) always from the session. Reasons max 512, ban `delete_message_days` 0..7 (422 otherwise). The bot's `{"error","message"}` refusals pass through with their status: 403 `hierarchy_blocked`/`actor_not_member`, 404 `member_not_found`/`warning_not_found`/`not_banned`, 409 `discord_forbidden`, 400 `discord_rejected`, 422 `invalid_input`, 503 `discord_unavailable`; 502 `internal_api_unavailable` only when the bot can't answer |
| `moderation.py` | gate changes 2026-10-28: `GET /api/moderation/members/search` and `GET /api/moderation/history/{user_id}` accept ANY of `moderate_members`/`kick_members`/`ban_members` (`require_any_permission`, so kick-only or ban-only roles can use the Moderation page; this WIDENS those two reads from `moderate_members`); kick `kick_members`, ban/unban `ban_members`, everything else unchanged | see left |
| `moderation.py` | `GET /api/moderation/purge-channels` (text channels the session user can see, `bot_can_purge`), `POST /api/moderation/purge` `{channel_id, amount, user_id?, confirm}` -> **202** job (amount = `"50"` or `"15m"`, same parser/bounds as `!purge`, mirrored as `PURGE_*` + `purge_amount_error`; 409 `confirm_required` above 100 or for a window without `confirm`), `GET /api/moderation/purge/{job_id}` | perm `manage_messages_bulk` |
| `moderation.py` | `GET /api/moderation/message-log/status`, `POST /api/moderation/message-log/export` `{kind: user|channel|deleted, user_id?, channel_id?, window?}` (the bot DMs the .txt to the SESSION user; the file is never returned over HTTP) | `require_staff` (same as `!msglog`) |
| `members.py` | `GET /api/members/search` (typeahead), `GET /api/members/{user_id}/profile` (warnings + honeypot only with `moderate_members`, history with any of moderate/kick/ban; withheld = null + listed in `restricted`), `GET /api/members/verify-channels`, `POST /api/members/{user_id}/verify` `{channel_id}` | search/profile `staff_base`; verify routes `verify_members` |
| `hub.py` | `GET /api/hub` (the session user's member hub), `POST /api/hub/self-roles` `{panel_id, role_id, assign}` (20/min per client IP); `user_id` is always the session user. Bot refusals pass through (404 `panel_not_found`/`role_not_on_panel`/`role_not_found`/`not_a_member`, 403 `role_blocked`, 409 `discord_forbidden`/`discord_error`) | any session (`require_session`; membership is decided by the bot, not the login-time flag) |
| | `POST /api/moderation/kick` | perm `kick_members` |
| | `POST /api/moderation/ban`, `POST /api/moderation/unban` | perm `ban_members` |
| `tickets.py` | `GET /api/tickets`, `GET /api/tickets/{id}`, `POST /api/tickets/{id}/close` (lock + delete channel after 30 s), `.../claim`, `.../archive` (delete channel now; no longer used by the UI), `DELETE /api/tickets/{id}`, `GET /api/tickets/{id}/messages`, `GET /api/ticket-panels` | perm `manage_tickets` |
| `ticket_panels.py` | `POST /api/ticket-panels`, `GET /api/categories`, `GET /api/ticket-panel-channels` | perm `manage_tickets`. Errors: 400 `validation_error` (problems joined into one readable `detail`), bot 404/409 passed through (422 -> 400), 502 `internal_api_unavailable` only when the bot can't answer |
| `permissions.py` | `GET /api/permissions/roles`, `GET`/`POST`/`DELETE /api/permissions/grants` | staff |
| `reaction_roles.py` | `GET`/`POST /api/reaction-roles/panels`, `GET`/`PATCH`/`DELETE /api/reaction-roles/panels/{id}`, `POST .../{id}/post`, `POST .../{id}/entries` (sends the session user as `actor_id`; a refused role is passed through as 409 `{"error": "role_not_allowed", "message"}`), `DELETE .../{id}/entries/{entry_id}`, `GET /api/channels`, `GET /api/reaction-roles/roles` (each role with `blocked_reason` for the session user) | perm `manage_reaction_roles` |
| `resolve.py` | `POST /api/resolve` (turns snowflake IDs into display names; ids deduped, max 100 per list after dedupe else 400 `invalid_request`; the bot resolves cache first, then `query_members`, then `fetch_member` at most 5 at a time; users no longer in the server come from the user cache with `departed: true`, see `cogs/user_cache.py` in section 10) | any bot permission (`require_any_bot_permission`: staff or any one grant, one check-bulk call; was `staff_base` until 2026-10-28, which 403'd Moderation/Tickets-only roles) |
| `resolve.py` | `GET /api/user-cache/{id}` (one cached profile, `{user, in_guild}`; 404 `not_cached`, 503 `user_cache_unavailable`) / `DELETE /api/user-cache/{id}` (forget it; actor = session user, audited bot-side as `user_cache_forget`) | GET: any bot permission; DELETE: `require_staff` |
| `templates.py` | `GET`/`POST /api/templates`, `GET /api/templates/channels`, `GET /api/templates/placeholders` (catalogue + examples for the session user), `GET`/`PATCH`/`DELETE /api/templates/{id}`, `PUT /api/templates/{id}/embeds`, `POST /api/templates/{id}/post`, `GET /api/templates/{id}/versions`, `POST /api/templates/{id}/rollback`, `GET /api/templates/{id}/export`, `POST /api/templates/import`, `GET /api/templates/{id}/preview` | perm `manage_templates` |
| `automation.py` | `/api/automation/*`: `GET options`; `GET events`, `PUT`/`DELETE events/{type}`, `POST events/{type}/test`; `GET greetings`, `PUT greetings/{welcome or boost}`, `POST greetings/{kind}/test`; `GET schedules`, `POST schedules/preview`, `POST schedules`, `PATCH`/`DELETE schedules/{id}`; `GET autoroles`, `GET autoroles/roles`, `POST autoroles`, `DELETE autoroles/{id}`; `GET managed`, `DELETE managed/{message_id}`, `POST managed/sync` (section 30) | events `manage_events`, greetings `staff_base`, schedules `manage_schedules`, autoroles `manage_autorole`, managed `manage_templates`, options any of the four grants or `staff_base` |
| `qotd.py` | `GET`/`PUT /api/qotd/config`, `GET /api/qotd/history`, `/models`, `/fallback-questions`, `/channels`, `/roles`, `POST /api/qotd/generate` (202, starts a job), `GET /api/qotd/generate/{job_id}`, `POST /api/qotd/post` | perm `manage_qotd` |
| `settings.py` | `GET`/`PUT /api/settings` | staff |
| `diagnostics.py` | `GET /api/diagnostics` (merged bot + dashboard dump, section 21), `POST /api/diagnostics/cache/clear` (empties the bot's diagnostics caches, then the live hub sends every Diagnostics page a full snapshot; bot down -> `502 internal_api_unavailable` with a message) | staff, with the bot-down fallback (`require_staff_even_if_bot_down`) |
| `fortnite.py` | `GET /api/fortnite/status`, `PUT /api/fortnite/config`, `POST /api/fortnite/post-now`, `GET /api/fortnite/channels`, `GET /api/fortnite/roles` | perm `manage_schedules` |
| `birthdays.py` | `GET /api/birthdays` (settings, status, entries, nearest, limits), `GET /api/birthdays/timezones`, `/roles` (with `blocked_reason` for the session user), `/channels`, `/members/search?q=`, `PUT /api/birthdays/config` (partial; `set_messages` writes both messages), `PUT`/`DELETE /api/birthdays/entries/{user_id}` (`{month, day, timezone?, clear_timezone?}`), `POST /api/birthdays/test` (`{user_ids}` 1-5), `POST /api/birthdays/reset-timers`, `GET /api/birthdays/export?format=plaintext\|csv\|ics` (`{filename, content, count}`), `POST /api/birthdays/import` (`{filename, content, dry_run}`, `.txt`, max 2 MB). Actor = session user. Bot refusals pass through as `{error, message[, fields]}` with the bot's 4xx; 503 `feature_unavailable` (cogs not loaded); 502 only when the bot can't answer. built and tested 2026-10-28, NOT live until the bot and dashboard restart (the frontend page is in the built `dist/` and shows an error state until then) | perm `manage_birthdays` |
| `debug.py` | `GET /api/debug/actions`, `POST /api/debug/run`, `GET /api/debug/fortnite/preview`, `GET /api/debug/health`, `GET /api/debug/channels` | owner (`require_owner`, 404 for everyone else) |
| `role_admin.py` | `GET`/`POST /api/roles`, `PATCH`/`DELETE /api/roles/{role_id}`, `POST /api/roles/{role_id}/move`, `POST /api/roles/{role_id}/duplicate`, `GET /api/roles/{role_id}/members`, `POST`/`DELETE /api/roles/{role_id}/members/{user_id}`, `GET /api/roles/member-search` (section 23) | staff |
| `supervisor.py` | `GET /api/supervisor/status`, `POST /api/supervisor/restart` (`{target: bot\|dashboard\|tunnel\|all, reason?}`, 202), `GET /api/supervisor/logs?service=&lines=` (1-200). Calls `supervisor.py` DIRECTLY via `server/services/supervisor_client.py`, not the internal API (section 26). Errors: `503 supervisor_unavailable`, `429 cooldown` (+`Retry-After`), `409 restart_in_progress\|not_managed\|conflict`, `502 supervisor_error` | staff, with the Diagnostics bot-down fallback (`require_staff_even_if_bot_down`: cached login roles only when the bot cannot answer; a member the bot reports as gone gets 403) |
| `supervisor.py` (rotate) | `POST /api/supervisor/rotate-logs` (`{reason?}`, 202 `{queued, message}`; the result appears in `/api/supervisor/status` `log_rotation.last_run`). Direct to the supervisor, works while the bot is down (section 26). Errors as above plus `409 rotation_in_progress` | owner (`require_owner`, 404 for everyone else, staff included) |
| `supervisor.py` (dependencies) | `POST /api/supervisor/update-deps` (`{mode: check\|apply, reason?}`, 202 `{queued, mode, message}`; progress and result in `/api/supervisor/status` `dependency_update`, which that route returns to the owner only). Direct to the supervisor, works while the bot is down; an apply that touches Python packages stops the bot and the dashboard for a few minutes (section 26 "Dependency updates"). Errors as above plus `409 update_in_progress` | owner (`require_owner`, 404 for everyone else, staff included) |
| `audit.py` | `GET /api/audit?action&actor_id&target_id&target_type&since&until&before_id&limit` (newest first, keyset cursor `before_id`, limit 1-200; bad input 422), `GET /api/audit/actions` | perm `view_audit` |
| `music.py` | `GET /api/music/state`, `/voice-channels`, `/status`, `/playlists`; `POST /api/music/control` (`{action: pause\|resume\|skip\|stop\|previous\|shuffle\|clear\|loop\|autoplay\|volume\|remove\|move\|join\|leave, value?, index?, to_index?, channel_id?}`), `POST /api/music/play` (URL, 202 job), `POST /api/music/search` (202 job), `GET /api/music/jobs/{job_id}` (play/search jobs only), `POST /api/music/search/{job_id}/pick`, `POST /api/music/playlists`, `POST /api/music/playlists/{name}/load`, `DELETE /api/music/playlists/{name}` (section 28) | perm `control_music`; actor always the session user; bot `{error, message}` 400/403/404/409/422/429 passed through, 503 `feature_unavailable`, else 502 |
| `music.py` (staff) | `GET /api/music/status/staff`, `POST /api/music/debugurl` (202 job, masked report only), `GET /api/music/debugurl/{job_id}` | staff (`require_staff`) |

Honeypot routes (`/api/honeypot/...`, the public `/api/public/appeals/...` and the public appeal sign-in `/api/public/appeal-auth/...`) are described in section 19.

### Internal API routes (`internal_api/routes/*.py`, bot process, `127.0.0.1:8766`)

All are guild-scoped (prefix `/guilds/{guild_id}`) and require the bearer secret, except `/health`. The dashboard backend is the only intended caller (through `server/services/internal_client.py`). Snowflakes in responses are stringified.

| Module | Endpoints |
|---|---|
| (app) | `GET /health` (no auth, no guild prefix) |
| `stats.py` | `GET /stats/live` |
| `permissions.py` | `POST /permissions/check`, `POST /permissions/check-bulk`, `POST /permissions/is-staff`, `GET /roles`, `GET`/`POST`/`DELETE /permission-grants` (POST 400 is `{error: "invalid_grant", message, errors}`; DELETE takes an optional `actor_id` for the audit row) |
| `moderation.py` | `GET /moderation/warnings/recent`, `GET /moderation/warnings/{user_id}`, `POST /moderation/warnings` (rank-checked; also writes the `mod_actions` warn row and the moderation-log post, like `!warn`), `DELETE /moderation/warnings/{warning_id}?moderator_id=&user_id=` (`moderator_id` required, recorded as the remover), `GET /moderation/history/{user_id}`, `POST /moderation/kick/{user_id}`, `POST /moderation/ban`, `POST /moderation/unban`, `POST /moderation/timeout/{user_id}`, `POST /moderation/untimeout/{user_id}`, `GET /moderation/members/search` |
| `member_tools.py` | `GET /members/{user_id}/profile?actor_id=&include_honeypot=` (`services/member_profile_service.build_profile`), `GET /members/verify-channels`, `POST /members/{user_id}/verify` (`services/verify_service.verify_member`, channel must be in the Unverified category: 422 `invalid_channel`; 409 `already_verified`/`verify_failed`), `GET /moderation/purge-channels?actor_id=`, `POST /moderation/purge` (job via `services/purge_service.JOBS`, 409 `confirm_required`/`purge_running`/`discord_forbidden`, 403 `channel_forbidden` when the moderator cannot view + read history there), `GET /moderation/purge/{job_id}` (404 `job_not_found` after a bot restart), `GET /message-log/status`, `POST /message-log/export` (re-checks `is_staff` on the live member: 403 `not_staff`; 404 `no_messages`; 409 `dm_failed`; 503 `message_log_unavailable`), `GET /server-info`. Errors are `{"error","message"}` |
| `member_hub.py` | `GET /hub/{user_id}` (`services/member_hub_service.MemberHubService.build`), `POST /hub/{user_id}/self-roles` `{panel_id, role_id, assign}` (`member_toggle`). Errors are `{"error","message"}` |
| `tickets.py` | `GET /tickets`, `GET /tickets/{id}`, `POST /tickets/{id}/close`, `/claim`, `/archive`, `DELETE /tickets/{id}`, `GET /tickets/{id}/messages`, `GET /ticket-panels` |
| `ticket_panels.py` | `POST /ticket-panels`, `GET /categories` |
| `reaction_roles.py` | `GET`/`POST /reaction-roles/panels`, `GET`/`PATCH`/`DELETE /reaction-roles/panels/{id}`, `POST .../{id}/post`, `POST .../{id}/entries` (`actor_id` required; 409 `role_not_allowed` / `actor_not_found`), `DELETE .../{id}/entries/{entry_id}`, `GET /reaction-roles/roles?actor_id=` (roles + `blocked_reason`), `GET /channels` |
| `resolve.py` | `POST /resolve` (+ user-cache fallback), `GET`/`DELETE /user-cache/{user_id}` (DELETE takes `?actor_id=`) |
| `templates.py` | `GET /placeholders?user_id=` (placeholder catalogue with rendered examples), `GET`/`POST /templates`, `GET`/`PATCH`/`DELETE /templates/{id}`, `POST /templates/{id}/post`, `PUT /templates/{id}/embeds`, `GET /templates/{id}/versions`, `POST /templates/{id}/rollback`, `GET /templates/{id}/export`, `POST /templates/import`, `GET /templates/{id}/preview`. PATCH, PUT embeds and rollback enqueue `bot.sync_queue` so managed messages re-render (before 2026-10-28 dashboard saves did not) |
| `automation.py` | `/automation/options`, `/automation/events[/{type}[/test]]`, `/automation/greetings[/{kind}[/test]]`, `/automation/schedules[/preview or /{id}]`, `/automation/autoroles[/roles or /{id}]`, `/automation/managed[/sync or /{message_id}]` (section 30) |
| `qotd.py` | `GET`/`PUT /qotd/config`, `GET /qotd/history`, `/qotd/models`, `/qotd/fallback-questions`, `POST /qotd/generate` (202 job), `GET /qotd/generate/{job_id}`, `POST /qotd/post` |
| `settings.py` | `GET`/`PUT /settings` |
| `diagnostics.py` | `GET /diagnostics` (full bot dump), `POST /diagnostics/cache/clear` (`{requested_by?}` -> `{cleared, entries, log_cache_cleared, at}`; logged, no audit row) |
| `honeypot.py` | `/honeypot/...` (section 19) |
| `fortnite.py` | `GET /fortnite/status`, `PUT /fortnite/config`, `POST /fortnite/post-now` |
| `birthdays.py` | `GET /birthdays`, `GET /birthdays/timezones`, `GET /birthdays/role-options?actor_id=`, `PUT /birthdays/config`, `PUT`/`DELETE /birthdays/entries/{user_id}` (DELETE takes `?actor_id=`), `POST /birthdays/test`, `POST /birthdays/reset-timers`, `GET /birthdays/export?format=&actor_id=`, `POST /birthdays/import`. Uses `bot.birthday_core` (503 if the birthday cogs are not loaded). Every mutation requires an `actor_id` that is a member (or the owner from outside: 403 `actor_not_member`), validates bot-side (Feb 29 allowed, impossible dates 422 `invalid_date`, unknown timezone 422 `invalid_timezone` (stored as the canonical IANA name), new entry for a non-member 404, config 422 `invalid_config` with `fields`: self-role policy for the role, text channel, timezone, `%e` staff-only), reschedules and writes an audit row |
| `debug.py` | `GET /debug/actions`, `POST /debug/run` (also requires `owner_id == OWNER_ID`, else 404), `GET /debug/fortnite/preview?refresh=`, `GET /debug/health` (all guild-scoped via `get_guild`, 404 for an unknown guild) |
| `role_admin.py` | `GET`/`POST /role-admin/roles`, `PATCH`/`DELETE /role-admin/roles/{role_id}`, `POST .../{role_id}/move`, `POST .../{role_id}/duplicate`, `GET .../{role_id}/members`, `POST`/`DELETE .../{role_id}/members/{user_id}` (section 23) |
| `audit.py` | `GET /audit` (same filters as the dashboard route, 422 on bad params, 400 `invalid_range`; `{entries, next_before_id}`, entries add best-effort `actor_name`/`target_name` from the bot cache), `GET /audit/actions` (`{actions: [{action, count}]}`) |
| `music.py` | `GET /music/state`, `GET /music/voice-channels?actor_id=`, `GET /music/status?staff=`, `POST /music/control`, `POST /music/play` / `/music/search` / `/music/debugurl` (202 jobs), `GET /music/jobs/{job_id}`, `POST /music/search/{job_id}/pick`, `GET`/`POST /music/playlists`, `POST /music/playlists/{name}/load`, `DELETE /music/playlists/{name}?actor_id=`. Drives the live `MusicCog` via `bot.get_cog("Music")` (503 `music_unavailable` when not loaded); refusals 4xx `{detail: {error, message}}` (section 28) |

The dashboard's `/api/channels`, `/api/templates/channels`, `/api/qotd/channels`, `/api/fortnite/channels` and `/api/debug/channels` all map onto the internal `GET /channels`; `/api/qotd/roles` and `/api/fortnite/roles` map onto `GET /roles`; `/api/reaction-roles/roles` maps onto `GET /reaction-roles/roles?actor_id=<session user>` (same fields plus `blocked_reason`).

---

## 10. Feature Modules (every cog in `main.py`'s `COGS` list)

Prefix is `!`. Slash commands sync globally on startup. Permission names refer to section 8.

### `cogs/moderation.py`
**Purpose:** Prefix-command toolkit for timeouts, bans/unbans, purge, and warnings.
**Commands:** `!timeout`, `!untimeout`, `!ban`, `!unban`, `!purge`, `!warn`, `!warnings`, `!delwarn` (all hidden prefix commands). Automatic spam handling is a separate cog, `cogs/anti_spam.py`.
**Services:** `ModerationService`, `WarningService`, `AuditService`, `TemplateService`, `TemplateRenderer`.
**Tables:** `mod_actions`, `warnings`, `audit_log`, `message_templates`/`template_embeds`/`embed_fields` (read-only, for log rendering).
**Config:** `LOG_CHANNEL_ID`, `JOIN_LEAVE_CHANNEL_ID`.
**Permissions:** `moderate_members` (timeout/untimeout/delwarn, and since 2026-10-28 also warn/warnings, which used `staff_base`, so they match the dashboard), `ban_members` (ban/unban), `manage_messages_bulk` (purge). Purge parsing, bounds and the purge call itself live in `services/purge_service.py` (re-exported by the cog), shared with the dashboard purge job.
**Rules (2026-10-14 hardening):** `!timeout`/`!untimeout`/`!ban`/`!warn` (and `!kick`) go through the rank check described under "Moderation rank checks and bounds" below and reply with the short reason when blocked. `!timeout` takes 1 to 40320 minutes (28 days); every reason is at most 512 characters; `!purge <count> [@user or ID]` removes exactly `count` messages (1 to 1000; it was 1 to 100 until 2026-10-20): it purges `before=` the command message (which used to be counted, so `!purge 5` removed 4) and deletes the command message separately; with a user filter it scans up to 2000 messages to find `count` of that user's. `!purge <duration> [@user or ID]` (`30s`, `15m`, `2h`, `1d`; 1 second to 14 days) deletes up to 1000 messages posted in that window, walking newest-first (`oldest_first=False`, `after=now-window`) so the cap keeps the most recent spam's removal. Anything over 100 messages, and every time-window purge, first posts a Confirm/Cancel prompt (`ui/confirm.py` `ConfirmView(allowed_user_id=author)`, 30 s; other users' clicks get an ephemeral refusal; timeout = cancelled); the prompt is deleted with the command message. The user filter is by ID, so it works for someone who already left or was banned. Discord bulk-deletes only messages younger than 14 days; older ones (count mode only) are deleted one call each, so they are slow. The `messages_purged` audit row's detail has `count`, `filtered_user_id`, `window_seconds`; the Discord audit-log reason is `!purge by <user> (<id>)`. `!delwarn <@user> <id>` only deletes the warning if it belongs to that user (the argument used to be ignored) and replies "not found for <@user>" otherwise; the `warning_removed` audit row records the command author. `!unban` of a user who is not banned replies so instead of a raw error.

### `cogs/kick.py`
**Purpose:** Auto-kicks members who remain Unverified for 7+ days; also a manual `!kick`.
**Commands:** `!kick`; background `check_unverified` loop (`@tasks.loop`).
**Services:** `ModerationService`, `AuditService`, `TemplateService`, `TemplateRenderer`.
**Tables:** `mod_actions`, `audit_log`, templates (read-only). Also writes a flat `kicks.log` file (not DB).
**Config:** `UNVERIFIED_ROLE_ID`, `VERIFIED_ROLE_ID`, `JOIN_LEAVE_CHANNEL_ID`, `CHECK_INTERVAL_MINUTES`, `AUTO_KICK`.
**Permissions:** `kick_members` for the manual command; the automatic loop has no permission check (acts as the bot). A manual `!kick` fails closed ("Moderation system is unavailable.") when `moderation_service` is missing (no bot.db), so a human kick is never done without the rank check (2026-10-28).
**Manual vs auto:** `!kick <@user or ID> [reason]` is rank-checked and logged with the moderator's own reason (default "Kicked by a moderator (no reason given)"), the moderator in the kick embed, and `audit_log` detail `source: "manual"`; the loop uses "Unverified for more than 7 days" and `source: "auto_unverified"`. Both still count in `kicks.log`.
**kicks.log:** format unchanged (line 1 `TOTAL_KICKS:N`, then history lines). Every write goes through one module-level `asyncio.Lock` and is atomic (temp file in the same folder, then `os.replace`), keeping every other line byte-for-byte (CRLF counter line kept). A kick number is reserved before the Discord call and released if the kick fails (only when no other kick bumped it since); a corrupt counter raises instead of restarting at 1.
**Loop robustness:** each iteration body is wrapped (any non-Discord exception for one member is logged and the next member is processed; an iteration-level exception is logged and retried at the next interval), and a `check_unverified.error` handler schedules a restart (5 s backoff growing to 60 s, gives up after 20 failures until the bot restarts) if the loop ever stops anyway.

#### Moderation rank checks and bounds (`services/moderation_service.py`)
Built and unit-tested 2026-10-14 (`tests/unit/test_moderation_hierarchy.py`, `test_moderation_routes.py`, `test_kicks_log_counter.py`, `test_logging_moderation_actor.py`); live since the 2026-10-14 17:43 restart of the bot and dashboard (frontend rebuilt).
- **Rank check:** `ModerationService.kick/ban/timeout/untimeout(..., check_hierarchy=True)` call `services/hierarchy.member_action_block_reason` (via `member_block_reason(guild, actor, target)`) BEFORE any Discord call and raise `ModerationBlocked(reason)`. Every human path passes it: the prefix commands and the internal API routes; `!warn` and the dashboard warn call `member_block_reason` directly. Bot-initiated paths (auto-kick loop, honeypot) leave the default `False`: the bot is the actor and Discord enforces the bot's own rank. Owner (`config.OWNER_ID`, read at call time, or the guild owner; `actor_is_owner`) skips only the actor rank check. Ban-by-ID / warn of a user who is not in the guild gets the identity rules only (not yourself, not the bot, not the guild owner).
- **Bounds:** `MAX_TIMEOUT_MINUTES` 40320 (Discord's 28 days), `MAX_DELETE_MESSAGE_DAYS` 7 (ban message deletion 0-7), `MAX_REASON_LENGTH` 512 (Discord's audit-log reason limit). The service raises `ModerationInputError` outside them (helpers `timeout_minutes_error`, `delete_days_error`, `reason_error`); the dashboard backend keeps its own copy of the numbers for pydantic (keep in sync).
- **Warnings:** `WarningService(repo, audit)` writes the `member_warned` / `warning_removed` `audit_log` rows itself (actor = the moderator / the remover), so `!warn`/`!delwarn` and the dashboard record them the same way. `WarningRepository.remove_warning(guild_id, warning_id, user_id=None)` returns the deleted row (or None) and, with `user_id`, only deletes a warning of that user. The `warning_removed` live event now carries `user_id`/`moderator_id`.

### `cogs/tickets.py`
**Purpose:** Full ticket-panel system — setup, posting, claiming, closing, archiving, chat logging, auto-purge, per-panel support roles, and a Verify-purpose variant.
**Commands:** `/ticket setup|post|list|delete`, `/ticket role add|remove|list` (`/ticket setup` no longer has a `closed_category` option). Persistent button views: `TicketPanelView`, `TicketControlView`, `TicketVerifyControlView` (the old `TicketClosedView` Archive button is gone). Background loops: `finish_closes_loop` (2 min, restart recovery for the post-close delete), `stale_sweep_loop` (hourly check, acts weekly), `purge_archived_tickets_loop` (24h).
**Services:** `TicketService`, `PermissionService`, `AuditService`, `SettingsService`, `TemplateRenderer`.
**Tables:** `ticket_panels`, `ticket_panel_roles`, `tickets`, `ticket_messages`, `guild_settings`, `audit_log`, templates.
**Config:** `TICKET_CHAT_LOGGING_ENABLED`, `TICKET_ARCHIVE_PURGE_DAYS` (both live-reloadable), `VERIFY_TEMPLATE_NAME`.
**Permissions:** `manage_tickets` (setup/post/list/delete/role management/claim/**close**), `verify_members` (Verify & Close button). Close is staff only since 2026-10-17 (the opener gets "Only staff can close tickets."), because closing now deletes the channel.
**Lifecycle rules (2026-10-14; built and unit-tested in `tests/unit/test_ticket_lifecycle.py`, live after a bot restart):**
- *Open race:* `TicketService.open_ticket` holds one `asyncio.Lock` per (guild, panel) around "count open tickets, draw the number, create the channel, insert the row", so two quick clicks cannot both pass the max-open check (the old orphan-channel race) and two members cannot draw the same `ticket_number` (UNIQUE per guild+panel). The lock is per panel rather than per opener because of that numbering constraint. If the row insert fails after the channel was created, the channel is deleted.
- *Close = delete (2026-10-17; built and unit-tested, live after the next bot restart):* one staff action replaces the old Close + Archive pair. `TicketService.close_ticket` moves `open -> closed` atomically (`TicketRepository.transition_status`), locks the opener out at once (view/read history kept, send/react/attach denied), posts the closed message (template `ticket-closed` or the default) plus a "This channel will be deleted in 30 seconds." embed with no buttons, posts the transcript if configured, then starts an in-process timer (`CLOSE_DELETE_DELAY_SECONDS` = 30) that calls `archive_ticket`. `archive_ticket` moves `open|closed -> archived` atomically and **always deletes the channel** (the record and chat log stay on the dashboard). The old rule that locked the channel in place when the panel had no closed category (`archive_deletes_channel`) is gone, and `closed_category_id` is no longer used (the column stays; the panel builder and `/ticket setup` no longer offer it). Verify & Close verifies, then calls the same `close_ticket`. Departed-member auto-archive deletes the channel immediately.
- *Restart safety:* the delay is timed from `tickets.closed_at`, so `finish_overdue_closes` (run by `finish_closes_loop` right after startup and every 2 minutes) archives any ticket still `closed` past its 30 s window, e.g. after a crash or restart inside it. The atomic transitions mean the timer and the sweep can never both act.
- *Weekly stale sweep:* `run_weekly_stale_sweep` (checked hourly by `stale_sweep_loop`) runs `close_stale_open_tickets` when the newest `ticket_stale_sweep` `audit_log` row is at least 7 days old (or there is none, so the first run happens at the first bot start on this code), then writes that row (`detail.closed_ticket_ids`). It closes, as the bot, every open ticket whose newest activity (the later of `opened_at` and the channel's last message, any author) is `STALE_TICKET_DAYS` (7) or more days old, with "Closed automatically after 7 days with no activity." in the notice; an open ticket whose channel was deleted by hand is archived directly.
- *Delete / purge:* `delete_ticket` (dashboard Delete on an archived ticket) and the purge job also delete a channel that still exists (archived in place under the old rule, or a delete that failed), best-effort, before removing the rows. Tickets archived before 2026-10-17 were locked in place, so their channels remain until this purge.
- *Purge guard:* `purge_archived_tickets_loop` skips the run with a warning when `ticket_archive_purge_days` is below 1 or not a whole number (`parse_purge_days`); `purge_old_archived_tickets` refuses the same values as a last line. The Settings page validates on write; this is the second line.
- *Audit:* `TicketService` (constructed with the cog's `AuditService`) writes `ticket_closed`, `ticket_archived` (detail `channel: deleted`; older rows may say `locked`) and `ticket_deleted` rows naming the actor (Discord user, dashboard session user, or the bot for departed-member auto-archive, restart recovery and the stale sweep; the in-process delayed delete names the closer). The weekly sweep also writes one `ticket_stale_sweep` row per run. There is no reopen action in the code, so no reopen row.
- *Dashboard errors:* the internal ticket routes answer `{"error": "ticket_not_found"|"ticket_action_refused"|"actor_not_found", "message": ...}` and `server/routes/tickets.py` passes 400/404/409 through (Discord 403 becomes 409); 502 `internal_api_unavailable` now only means the bot could not be reached. `DELETE /api/tickets/{id}` sends the session user as `?actor_user_id=` for the audit row.

### `cogs/verify.py`
**Purpose:** Manual verification — swaps Unverified→Verified role, sends a templated message. Core logic lives in `services/verify_service.verify_member()` (2026-10-28; reads the config IDs at call time; re-exported as `cogs.verify.verify_member`), shared by `!verify`, the ticket Verify & Close button and the dashboard profile Verify button (audit detail `source: "dashboard"`).
**Commands:** `!verify`.
**Services:** `TemplateService`, `TemplateRenderer`, `AuditService`.
**Tables:** templates (read-only), `audit_log`.
**Config:** `UNVERIFIED_ROLE_ID`, `VERIFIED_ROLE_ID`, `VERIFY_TEMPLATE_NAME`, `UNVERIFIED_CATEGORY_ID` (gates where `!verify` may run).
**Permissions:** `verify_members`.

### `cogs/birthdays.py`
**Purpose:** Core birthday feature — self-service birthday/timezone set/view, upcoming-birthdays list, and the scheduler that assigns a role + posts an announcement at local midnight. Defines the shared `BirthdayCore`/`BirthdayService` other birthday cogs depend on.
**Commands:** `/birthday get|show-nearest|set date|set timezone|remove`.
**Services:** `BirthdayService`.
**Tables:** `birthday_settings`, `birthdays`.
**Config:** none from `config.py` — fully guild-scoped DB data.
**Permissions:** none — all self-service. The shared `BirthdayCore.manage_check` (bot permission `manage_birthdays`, `MANAGE_PERMISSION`; staff bypass) gates the sibling cogs.
**Mentions:** the announcement is the one send that opts back in to `@everyone` (the bot-wide default blocks it, section 2): only when the saved template contains the `%e` placeholder; otherwise `everyone=False`. A message containing `%e` can only be saved by full staff (OWNER_ID / STAFF_ROLE_IDS; `services/birthday_service.everyone_block_reason`, the `/config announce set-message` modal and `PUT /birthdays/config`; an unchanged staff-written `%e` message survives a non-staff edit of the other message). Role mentions in the template never ping (`roles=False`, 2026-10-28; before that `roles=True`), users still do.
**Birthday role safety (2026-10-28, permission review):** the role is handed to whoever registers today's date, so it follows the self-role policy (`services/self_role_policy.py`): `/config birthday-role` and the dashboard refuse it via `birthday_role_block_reason` (not @everyone/managed, below the bot and the configuring member's top role (owner exempt), no moderator permissions, not a `STAFF_ROLE_IDS` role or a role with any `permission_grants` row), and `_fire_birthday` re-checks `grant_block_reason` before `add_roles` (logs and skips when blocked).
**Shared logic (2026-10-28, for the dashboard):** `services/birthday_service.py` now also holds the recent/upcoming grouping (`nearest_birthdays`, used by `/birthday show-nearest`; Feb 29 shows on Feb 28 in non-leap years), the export builders (`build_export`; CSV no longer crashes on a member who left, ICS `DTSTART` is a valid 8-digit date (was 9 digits for Jan-Sep), text is RFC 5545-escaped and folded by octets, `DTSTAMP` added), the import parser (`parse_import_text`, which now skips impossible dates such as Feb-30 instead of storing them, and reports skipped lines), `preview_import`, `clear_timezone`, `role_status`/`channel_status`, and publishes the live `birthdays` topic from every mutation. `BirthdayCore` gained `manage_check`, `audit` (best-effort `AuditService.log`), `send_test_announcement`, `scheduled_count`, `cancel_scheduled`; `reset_timers` returns the number reset. built and tested 2026-10-28, NOT live until the bot and dashboard restart (the frontend page is in the built `dist/` and shows an error state until then).
**Date rules (2026-10-14; pure helpers in `services/birthday_service.py`, unit-tested in `tests/unit/test_birthday_dates.py`, live after a bot restart):** everything is computed in the member's local calendar (their timezone, else the guild's; an unparseable stored timezone falls back to the guild's). `birthdays.last_celebrated` now stores the LOCAL year of the occurrence that was announced (it used to be the UTC year at firing time), so a Jan 1 birthday in a UTC+ zone (starts Dec 31 UTC) or a Dec 31 birthday in a UTC- zone (ends Jan 1 UTC) is one occurrence and is never announced twice. The hourly role cleanup removes the birthday role as soon as it is no longer the member's birthday locally, with no `last_celebrated` check (that check kept the role on for a year across the year boundary); members holding the role with no birthday on file are left alone. **Feb 29 birthdays are celebrated on Feb 28 in non-leap years** (scheduling, the "is it today" check, role cleanup and `/config announce timers-reset` all use that rule). The scheduling window now also fetches yesterday's (UTC) birthdays for UTC- zones, and the per-birthday timer re-sleeps if the event loop wakes a few ms before local midnight. Existing rows keep their old UTC-year value; the only effect is that an occurrence already announced across a year boundary while this code was deployed could be announced once more.

### `cogs/birthday_config.py`
**Purpose:** Staff configuration for birthday role/channel/messages/toggles.
**Commands:** `/config check|add-only|private-confirms|birthday-role|set-timezone|announce help|set-channel|set-message|set-ping|ping-user|test|timers-reset`.
**Services:** reuses `BirthdayCore.svc`.
**Tables:** `birthday_settings`, `birthdays` (read for counts).
**Permissions:** `manage_birthdays` on every command (2026-10-28; was `staff_base` via `is_any_staff_interaction`). Saving a message with `%e` additionally needs full staff; the birthday role follows the self-role policy (see `cogs/birthdays.py`). Every change writes an audit row (`birthday_config_update`, `birthday_test_announcement`, `birthday_timers_reset`; `detail.source` = `discord` or `dashboard`). `/config set-timezone` with no value now also reschedules.
**Dashboard:** `/dashboard/birthdays` Settings and Tools tabs (built and tested 2026-10-28, NOT live until the bot and dashboard restart (the frontend page is in the built `dist/` and shows an error state until then)).

### `cogs/birthday_override.py`
**Purpose:** Staff override of another member's birthday/timezone, bypassing add-only restrictions.
**Commands:** `/override set-birthday|set-timezone|remove-birthday`.
**Tables:** `birthdays`.
**Permissions:** `manage_birthdays` (2026-10-28; was `staff_base`). Audited as `birthday_set` / `birthday_remove` (target = the member). Dashboard twin: add/edit/remove on `/dashboard/birthdays` (also clears a member's timezone, which the command cannot).

### `cogs/birthday_io.py`
**Purpose:** Bulk export (plaintext/CSV/ICS) and import (BirthdayBot `.txt` format) of birthday data.
**Commands:** `/birthdays export|import`.
**Tables:** `birthdays` (bulk read/write via `import_bulk`).
**Permissions:** `manage_birthdays` (2026-10-28; was `staff_base`). Audited as `birthday_export` / `birthday_import`. Same limits on the dashboard (`.txt`, 2 MB `IMPORT_MAX_BYTES`), which adds a dry-run preview (new / changed / unchanged / skipped lines with reasons).

### `cogs/qotd.py`
**Purpose:** Daily Question of the Day — Ollama-generated (with keyword/similarity validation and a 50-entry fallback list), staff-approval workflow, scheduled auto-post.
**Commands:** `/qotd setchannel|setapprovalchannel|setmodel|now|preview|regenerate|status|history|debug`. Button views: `ApprovalView`, `ModelSelectView`, `HistoryView`.
**Services:** `QotdRepository`, `AuditService`, `QotdService`.
**Tables:** `qotd_config`, `qotd_history`, `audit_log`.
**Config:** `OLLAMA_URL`/`OLLAMA_EXE`/`OLLAMA_MODEL` (env vars, not `config.py` constants).
**Permissions:** `manage_qotd` on all admin commands; `/qotd status` is open. The approval buttons (`ApprovalView.interaction_check`) and the Custom question modal (re-checked on submit) also need `manage_qotd` (2026-10-28, from the permission review; matches `POST /api/qotd/post`; built, NOT live until the bot restart).
**Daily scheduler (2026-10-14; unit-tested in `tests/unit/test_qotd_schedule.py`, live after a bot restart):** the 60 s loop queues today's approval request from the configured post time (UTC) until the end of that UTC day (`qotd_slot_due`), so a bot that was down or restarting at the exact minute catches up once later the same day (the approval embed then carries a "Late" field). It is skipped when a question was already posted today (`qotd_history`) or today's request was already sent. The persisted dedupe key is an `audit_log` row `qotd_approval_sent` (detail: date, message id, catch_up, question), written after the approval message is sent; there is no dedicated column because migrations were frozen for this change (a `qotd_config.last_approval_date` column would be cleaner). Every failure is caught per guild and the loop has an `.error` handler that restarts it: `discord.Forbidden` on the approval channel or a missing approval channel gives up for that day (it used to leave `_pending` set, which silently stopped QOTD until a restart); other send/generation errors retry after 10 minutes, up to 3 attempts a day (`DailyAttemptTracker`, in memory). **No hardcoded IDs:** the old-guild `_GUILD_ID` and the default approval channel (`1311766377740243035`, a channel in the previous guild) were removed; the approval channel comes only from `qotd_config.approval_channel_id` (set for the live guild), and without one the daily request is skipped with a warning (`/qotd setapprovalchannel` with no channel now clears it and says so; `/qotd status` shows "not set").
**Dashboard generation:** `POST /api/qotd/generate` starts a job on the bot (`GenerationJobs` on the cog, one running job per guild, kept 15 minutes in memory) and answers 202 at once; the frontend polls `GET /api/qotd/generate/{job_id}` every 2 s (`generateQotdQuestion` in `src/api/qotd.ts`), so no request comes near Cloudflare's 100 s limit. The job itself is bounded by `DASHBOARD_GENERATE_DEADLINE_SECONDS` (150 s, `generate_question(deadline_seconds=...)`), after which it returns a fallback question. A bot restart loses running jobs (poll answers 404 `job_not_found`). `POST /api/qotd/post` errors follow the dashboard contract (bot `{"error", "message"}` passed through, Discord 403 becomes 409); a post whose history row failed returns success with a `warning` instead of a 500.

### `cogs/event_commands.py`
**Purpose:** Binds message templates to Discord lifecycle events (join/leave/boost/role-add/role-remove), dispatched via `EventBus`.
**Commands:** `/event set|clear|list|test`.
**Services:** `EventService` (attached as `bot.event_service`, also used by the internal API).
**Tables:** `event_bindings`, `event_binding_templates`, templates, `audit_log`.
**Permissions:** `manage_events`.
**Audit + live (2026-10-28):** `set_binding` / `clear_binding` / a successful `test_event` write `event_binding_set` / `event_binding_clear` / `event_test` rows (`EventService(audit=...)`) and publish the `automation` live topic, for the slash command and the dashboard alike. Dashboard: Automation > Events (section 30).

### `cogs/reaction_roles.py`
**Purpose:** Self-serve reaction-role panels (button-based, not emoji-reaction-based despite the name) with optional exclusivity.
**Commands:** `/reactionrole setup|add|remove|post|list|delete`; raw component-interaction handler for role toggling.
**Services:** `ReactionRoleService`, `PermissionService`.
**Tables:** `reaction_role_panels`, `reaction_role_entries`.
**Permissions:** `manage_reaction_roles` on all commands; button clicks have no permission gate (any member may self-assign).
**Self-role safety (`services/self_role_policy.py`, over `services/hierarchy.role_grant_block_reason`):** `/reactionrole add` and the dashboard's add-entry path (`ReactionRoleService.add_entry`, which requires `guild` + `actor`) refuse @everyone, managed roles, roles at or above the bot's top role, roles with any `DANGEROUS_PERMISSIONS` flag, roles in `STAFF_ROLE_IDS` or with any `permission_grants` row, and (unless the actor is `OWNER_ID` or the guild owner) roles at or above the configuring member's top role. If the grants can't be read it fails closed. Every button click re-checks: the panel must still exist in this guild, the role must still be an entry of that panel, the click must come from the panel's current `message_id` (removed buttons on an old message no longer grant), and when granting (not removing) the role must pass the same rules without the actor part; otherwise an ephemeral refusal plus a warning log. The dashboard role picker disables roles with a `blocked_reason`; the bot-side check is what counts.

### `cogs/live_roles.py`
**Purpose:** Publishes the dashboard's live `roles` topic (section 22) so role lists on the Self-roles and Permissions pages update when a role is created, renamed, recoloured, reordered or deleted in Discord. Separate from `logging_role.py` on purpose (that one is audit logging and ignores position-only changes).
**Events:** `on_guild_role_create`, `on_guild_role_update`, `on_guild_role_delete` (other guilds ignored once the live hub knows the guild id).
**Services:** `services/role_live_service.py` `RoleEventCoalescer` (debounce/coalesce, pure helpers `merge_action` / `build_messages`), `services.live_events.publish`.
**Tables:** none. **Commands:** none. **Permissions:** none (the topic itself is gated `manage_reaction_roles` in `server/live/hub.py`).

### `cogs/managed_commands.py`
**Purpose:** Manages "managed messages" — persistent template-driven messages that can be resynced when their template changes. Owns the `SyncQueue` debounced worker.
**Commands:** `/managed list|untrack|sync`. (`/template post`, which creates a managed message, lives in `template_commands.py`.)
**Services:** `ManagedMessageService` (with `audit=`: `managed_untrack` / `managed_sync` rows via `untrack(..., actor_id=)` / `sync_now(...)`; post/sync/untrack publish the `automation` topic), wires up `bot.sync_queue`. Dashboard: Automation > Managed messages (section 30).
**Tables:** `managed_messages`, templates, `template_components`.
**Permissions:** `manage_templates`.
**Mentions:** template posts and template-sync edits (`ManagedMessageService`) send with `AllowedMentions(everyone=False, roles=True, users=True)`: `@everyone`/`@here` in a template never ping (there is no per-template opt-in flag); role and user mentions still do.

### `cogs/gateway.py`
**Purpose:** Thin bridge translating raw discord.py gateway events into `EventBus` publications, autorole triggers, and audit/live-log calls — "no business logic," per its own docstring.
**Events:** `on_member_join`, `on_member_remove`, `on_member_update` (role diffing), `on_message` (boost detection), `on_message_delete`, `on_message_edit`.
**EventBus publishes:** `member_join`, `member_leave`, `role_add`, `role_remove`, `boost`.
**Tables:** `audit_log`.
**Config:** `JOIN_LEAVE_CHANNEL_ID` (via `log_router.py`).
**Permissions:** none (no commands).

### `cogs/welcome.py` / `cogs/boost.py`
**Purpose:** Near-identical pair — configure and send a welcome/boost message on member-join or boost, template-driven with a hardcoded embed fallback.
**Commands:** `/welcome setchannel|toggle|status` / `/boost setchannel|toggle|status`.
**Services:** `WelcomeService`/`BoostService`, `TemplateService`, `AuditService`.
**Tables:** `welcome_config`/`boost_config`, templates (read-only), `audit_log`.
**Permissions:** staff-only (`is_any_staff_interaction`, i.e. `has_permission("staff_base")`) for `setchannel`/`toggle`/`status` (`status` was ungated until 2026-10-28, permission review #12). The dashboard's Automation > Welcome & Boost tab uses the same `staff_base` gate.
**Live:** `set_channel` / `set_enabled` publish the `automation` topic.
**One welcome path:** `cogs/welcome.py` `on_member_join` -> `WelcomeService.send_welcome` is the only code that greets a join (checked 2026-10-14: `cogs/verify.py`'s "Welcome in." is a DM after verification, `ticket_service`'s verify-ticket welcome is per ticket). A second greeting can only come from configuration: an `event_bindings` row for `member_join` in this guild (`/event bind`); today the only such rows belong to the OLD guild (migration 8 seed), so they never fire here.

### `cogs/logging_channel.py`, `logging_member.py`, `logging_role.py`, `logging_server.py`, `logging_voice.py`
**Purpose:** Five narrow, DB-only audit-log cogs, each covering one Discord entity type — channel create/delete/rename/topic/permission changes; member nickname changes; role create/delete/rename/permission/color changes; invite/emoji/sticker create-delete; voice join/leave/switch and server-mute/deafen. Each deliberately skips noisy sub-events (position-only reorders, self-mute/deafen).
**Commands:** none — pure event listeners.
**Services:** `AuditService` only.
**Tables:** `audit_log`.
**Permissions:** none.

### `cogs/logging_moderation.py`
**Purpose:** Captures moderation actions performed via Discord's native UI (bypassing the bot's own commands) — native bans/unbans/timeouts — resolving the actor via Discord's own audit log.
**Events:** `on_member_ban`, `on_member_unban`, `on_member_update` (timeout diffing).
**Services:** `AuditService`, `log_router`.
**Tables:** `audit_log`.
**Config:** `JOIN_LEAVE_CHANNEL_ID`, `LOG_CHANNEL_ID` (via `log_router.py`).
**Permissions:** none (requires the bot itself to hold Discord's "View Audit Log" permission for correct actor attribution).
**Actor matching (2026-10-14, unit-tested, live after a bot restart):** reads the last 10 audit-log entries of the action type and takes the first whose target is this member, created within 30 s, with a user (`pick_actor_entry`); for timeouts the entry must also carry a `timed_out_until` change (`is_timeout_change`, so a nickname edit on the same member doesn't count). No match: wait 2 s and look once more (the entry can land after the gateway event). Still none, or no View Audit Log / HTTP error: the moderator is unknown, the embed shows "Moderator: Unknown" and the `audit_log` row uses `actor_id` 0 with detail `{"actor": "unknown"}`. It used to take the newest entry of that type regardless of target or age (and fell back to the bot's id), crediting the wrong moderator.

### `cogs/advanced_commands.py`
**Purpose:** Bundles three staff-config command groups: fine-grained permission grants, an audit-log viewer, cron scheduled sends, and autorole rules.
**Commands:** `/bot permissions grant|revoke|list`, `/bot audit`, `/schedule add|remove|toggle|list`, `/autorole add|remove|list`.
**Services:** `PermissionService`, `SchedulerService` (attached as `bot.scheduler_service`), `AutoroleService` (attached to the bot as `bot.autorole_service`), both built with `audit=AuditService(...)`: `schedule_add` / `schedule_toggle` / `schedule_remove` and `autorole_add` / `autorole_remove` audit rows plus the `automation` live topic, for commands and dashboard alike. Since 2026-10-28 `toggle` is guild-scoped, cron is capped at 100 chars and labels at 100; `add_rule` refuses a delay outside 0..604800 s and drops a trigger role on non-`role_add` rules (such a rule could never fire). Dashboard: Automation > Schedules / Autoroles (section 30).
**Tables:** `permission_grants`, `audit_log`, `scheduled_sends`, `autorole_rules`, templates (joined for display names).
**Permissions:** `/bot permissions *` → `is_staff_interaction` (coarse, deliberately not a revocable grant, see §8); `/bot audit` → `view_audit`; `/schedule *` → `manage_schedules`; `/autorole *` → `manage_autorole`.
**Autorole safety:** `/autorole add` runs the same self-role check as `/reactionrole add` (`AutoroleService.add_rule(..., guild=, actor=)`, see `cogs/reaction_roles.py`), and `AutoroleService._apply` re-checks right before `add_roles` (after any delay) that the rule is still enabled in `autorole_rules` and the role still passes the click-time rules; a refusal is a warning log, nothing is granted.

### `cogs/template_commands.py`
**Purpose:** The `/template` CRUD + versioning + publishing workflow — the authoring side of the templates subsystem.
**Commands:** `/template create|edit|list|delete|preview|history|post|rollback|export|import`.
**Export/import:** `TemplateService.export_template` / `import_template` (moved out of the cog 2026-10-28, used by the cog and the dashboard; import always creates a new template, max 10 embeds, non-object input refused). Dashboard parity: Templates page Import, editor "Preview, export and history" card (history, rollback, export, bot preview).
**Services:** `TemplateService`, `TemplateRenderer`, optionally `ManagedMessageService` (for `post`).
**Tables:** `message_templates`, `template_embeds`/`embed_fields`, `template_versions`, `audit_log`, indirectly `managed_messages`/`template_components`.
**Permissions:** `manage_templates` on every command.

### `cogs/music.py`
**Purpose:** Full voice-channel music player — join/play/queue/skip/volume/loop/shuffle/history/named-playlist persistence via yt-dlp. The only prefix-command-only cog; it uses no DB-backed feature service (since 2026-10-28 it uses `services/music_control.py` for dashboard jobs, `services/live_events.py` for the `music` topic and `AuditService` for dashboard actions only — playlists persist to flat JSON files).
**Commands (all `!` prefix):** `!join [channel_id]`, `!leave`, `!play <search or url>`, `!pause`, `!resume`, `!skip` (vote-based unless requester), `!volume <n>`, `!np`, `!previous`, `!history`, `!queue`, `!shuffle`, `!loop`, `!autoplay [on|off]` (per-guild, in memory; queues related YouTube Mix tracks when the queue empties, never after skip/stop/leave/error, never repeating the last `MUSIC_AUTOPLAY_HISTORY` tracks, at most `MUSIC_AUTOPLAY_MAX_CONSECUTIVE` in a row, only while a human is in the voice channel), `!remove_queue` / `!removequeue`, `!remove <index>`, `!move <from> <to>`, `!save <name>`, `!load <name>`, `!playlists`, `!delete_playlist <name>`, and `!musicstatus` (alias `!status`; open to everyone: shows ping, bot uptime, now playing/queue, PO-token server state, uptime and restart count, cookie file presence and age, and Node/yt-dlp/plugin versions; users with `staff_base` additionally get the server URL, PID, watchdog state, last error and a short sanitised log tail; file system paths and secrets are never shown; autoplay state and the age of the last cookie-extension refresh are public), `!debugurl [<query|url>] [--full]` (bot owner or `staff_base` only; see section 18), and `!musichelp [command]` (alias `!mhelp`; also `/music help [command]` with autocomplete, ephemeral). The help text is generated at call time from the cog's registered commands (usage from each docstring's `Usage:` line or the signature, aliases, `extras={"staff_only": True}` marks), grouped by the `MUSIC_HELP_CATEGORIES` table (unlisted commands land under "Other") and chunked with `core/text_utils.split_message`. `!help` is not aliased: discord.py's default help command already owns that name. The bot has a single prefix (`!`), so no alternate music prefixes are supported.
**Services:** none of the DB-backed services; talks directly to `yt_dlp.YoutubeDL` and discord.py's voice API, and reads `bot.pot_server_service` (set in `main.py`) for `!musicstatus`.
**Tables:** none (playlists are JSON files under `playlists/`).
**Config:** `MUSIC_COOKIES_FILE`, `MUSIC_NODE_PATH`, `MUSIC_DEFAULT_SEARCH`, `MUSIC_ALLOW_PLAYLISTS`, `MUSIC_DEFAULT_VOLUME`, `MUSIC_INACTIVITY_TIMEOUT`, `MUSIC_PLAYER_CLIENTS` (all read once at import/construction, so changes need a restart). Relies on the PO-token server (section 18) for gated videos. `cookies.txt` (yt-dlp cookies) is a credential; keep it out of any shared folder or archive.
**Permissions:** none — every command is open to any user, a notable contrast with the rest of the bot.
**Dashboard (2026-10-28, section 28):** the cog also exposes `dashboard_*` methods for `internal_api/routes/music.py`. The prefix commands were refactored onto shared core methods (`_do_pause`, `_do_resume`, `_force_skip`, `_do_volume`, `_do_loop`, `_do_autoplay`, `_do_clear_queue`, `_do_shuffle`, `_do_remove`, `_do_move`, `_do_leave`, `_enqueue_url`, `_search`, `_status_inputs`, `_debugurl_refusal`) with the same replies, and every state change calls `_notify(event)` (live topic `music`; thread-safe from the player's `after` callback). `current_song` now also tracks pause time (`paused_at`/`paused_total`) for the dashboard progress bar; `!np` still shows wall-clock elapsed.

**Playlist ownership (2026-10-28):** playlist files are now `{"created_by": <user id>, "tracks": [...]}`; older files (a bare track list) still load. `!save` over an existing name and `!delete_playlist` (and the dashboard equivalents) are allowed only for whoever saved it or a `staff_base` holder (staff bypass); ownerless legacy playlists can only be overwritten or deleted by `staff_base`. An allowed overwrite keeps the original owner (a legacy file is claimed by the staff member who overwrites it). `!load` and `!playlists` are unchanged (open to all).

**Mentions:** music echoes user input and third-party titles, so `cog_before_invoke` wraps every command Context (`mute_mentions`) so its sends default to `AllowedMentions.none()` (the search-pick reply path does the same), and echoed playlist names / track titles go through `discord.utils.escape_mentions` (`safe_text`). `!load @everyone` or a title containing `@everyone`/`<@&id>` pings nobody.

### `cogs/anti_spam.py`
**Purpose:** Automatic spam detection and cleanup. Rules live in `services/spam_detector.py` (pure, in-memory, per user): **flood** (`antispam_flood_messages` messages within `antispam_flood_seconds`, any channels), **duplicate** (the same text `antispam_duplicate_messages` times within `antispam_duplicate_seconds`, across channels; compared casefolded with whitespace collapsed; image-only messages and messages starting with `!` never count as duplicates, only toward flood) and **mentions** (one message mentioning `antispam_mention_limit` distinct users/roles, the author excluded). A count of 0 turns that rule off.
**On a hit:** deletes the flagged messages (the flood window, every copy of the duplicate, or the mention message; bulk per channel, falling back to one-by-one if a bulk call fails), times the member out for `antispam_timeout_minutes` through `ModerationService.timeout` (bot as moderator, bot-initiated so no rank check; lands in `mod_actions` and the member's dashboard history; 0 = delete only; skipped if already timed out; a Forbidden is reported, not raised), writes a `spam_detected` audit row (actor = the bot; detail `rule`, `detail`, `channel_id`, `channels`, `messages_deleted`, `timeout_minutes`, `timeout_result` = `ok`/`off`/`already`/`forbidden`/`failed`/`unavailable`) and posts a "Spam Detected" embed (rule, channel(s), messages deleted, action, a 300-character markdown-escaped sample) to the moderation-log channel via `log_router.route_and_post`. The native-timeout listener also logs the timeout, as for every bot timeout. For 30 s after a hit (`COOLDOWN_SECONDS`, counted from the hit, not extended) further messages from that member are deleted silently, with no second timeout, audit row or embed.
**User-installed apps (2026-10-20):** an app reply carries `interaction_metadata` (and, on Discord, a `webhook_id`, so it is checked first via `services/message_source.app_source`). A reply from a **user-installed** app counts against the member who ran the command (its text includes the embed text; a hit deletes the app's messages and times that member out; audit detail adds `via_app_id`/`via_app_name`, the log embed a "Posted via app" field). Replies from the server's own bots answering a command are not checked.
**Never checked:** bots, plain webhooks (log webhooks legitimately post in bursts), non-default/reply message types (joins, boosts, pins), the owner and `STAFF_ROLE_IDS` (`is_staff`), the guild owner, Discord administrators (sync checks before every message), and members holding the bot permission `moderate_members` or `manage_messages_bulk` (a DB lookup made only when a rule fires, cached 60 s; such a member's history is then forgotten).
**Commands:** `!antispam` (current rules; needs `manage_messages_bulk`), `!antispam on|off` (staff only, like the Settings page; saves `antispam_enabled` through `SettingsService.update`, so it writes the same `guild_settings` row and `settings_update` audit row as a dashboard save).
**Config:** the dashboard Settings page "Anti-spam" tab (`antispam_*`, all live, §6), defaults `ANTISPAM_*` in `.env`. State is in memory only (lost on restart, fine for second-scale windows; idle users are swept every 500 messages, at most 100 remembered messages per user).
**Tables:** `audit_log`, `mod_actions` (via `ModerationService`), `guild_settings` (read, and written by `!antispam on|off`).
**Tests:** `tests/unit/test_anti_spam.py`.

### `cogs/message_log.py`
**Purpose:** Short-term moderation message log in its **own SQLite file** (`MESSAGE_LOG_PATH`, default `message_log.db` in the repo root; not `bot.db`, no migrations: `services/message_log_service.py` creates the `messages` table with IF NOT EXISTS and `PRAGMA user_version`; deleting the file is safe, it is recreated on start). Added 2026-10-20 because an app spam wave was purged with no record of what it was.
**Stored:** every guild message of type default/reply/slash/context-menu command from members, bots, webhooks and apps: ids, channel name, author name, `author_kind` (`member`/`bot`/`webhook`/`app`), for app replies `invoker_id` (the member who ran the command) and the app, the text including embed text (4000 chars), attachment names and URLs (Discord's URLs expire), `created_at`; edits set `edited_content`/`edited_at` (only when the text really changed, so link-preview edits don't count); `on_raw_message_delete` and `on_raw_bulk_message_delete` set `deleted_at`, so messages removed by a delete, `!purge`, anti-spam or the honeypot can still be read back. **Never stored:** DMs, this bot's own messages, system messages, and ticket channels (they have the separate opt-in ticket chat log; looked up with `TicketRepository.get_ticket_by_channel`, a positive answer cached for good, a negative one 60 s; a failed lookup skips the message).
**Writes and retention:** record/edit/delete only buffer an op; a 3 s loop (or 500 waiting ops) flushes them in order in one transaction (WAL mode, flushes serialised by a lock, a failed flush is logged and dropped). `prune_loop` runs at startup and every 24 h: deletes rows with `created_at` older than `MESSAGE_LOG_RETENTION_DAYS` (default 7, 1 to 30) and VACUUMs the file. Timestamps are fixed-format UTC strings (`%Y-%m-%dT%H:%M:%S.%fZ`) so they compare as text.
**Commands:** `!msglog` (staff only: owner / `STAFF_ROLE_IDS`): no argument = status (rows, deleted, oldest, file size, retention); `!msglog <@user|user ID> [30m|6h|2d]` (author OR the member who ran an app), `!msglog <#channel|channel ID|here> [dur]` (a bare ID is a channel if the server has one, else a user), `!msglog deleted [dur]`. Default window 24 h, capped at the retention; at most 5000 rows (the newest), oldest first. The export is a .txt file sent **by DM only** (it contains other members' messages); closed DMs get a reply saying so, never a channel post.
**Dashboard twin (2026-10-28):** the "Message tools" page and the member profile ("DM me their messages") call `POST /api/moderation/message-log/export` (`require_staff`, re-checked bot-side); the bot DMs the same export (`services/message_log_service.build_export` / `send_export_dm` / `parse_window`, shared with the cog). Both paths write a `message_log_exported` audit row (`detail.source` = `command` / `dashboard`). Never a channel post or a browser download.
**Config:** `MESSAGE_LOG_ENABLED`, `MESSAGE_LOG_PATH`, `MESSAGE_LOG_RETENTION_DAYS` (.env only; restart to change).
**Tests:** `tests/unit/test_message_log.py` (temp file only).

### `cogs/user_cache.py`
**Purpose:** remembers every member's last known profile and membership status so the dashboard renders people who left, were kicked or banned with a name + avatar instead of a raw ID (moderation history, audit log, ticket chat logs, honeypot catches/appeals...). Added 2026-10-28. **Own SQLite file** `USER_CACHE_PATH` (default `user_cache.db`, repo root), like the message log: not `bot.db`, no migrations (`services/user_cache_service.py` creates the `users` table with IF NOT EXISTS and `PRAGMA user_version`); deleting it is safe (the next start rebuilds members from the member list and banned users from the ban list; older departures are then only known again through `fetch_user`). Never add it to the log rotator.
**Row (one per user):** `user_id`, `username`, `global_name`, `nick`, `display_name`, `avatar_hash` / `guild_avatar_hash` / `avatar_url` (display avatar URL when last seen), `color` (top coloured role), `is_bot`, `status` (`member`/`left`/`kicked`/`banned`/`unbanned`), `status_changed_at`, `joined_at`, `first_seen`, `last_seen` (last seen as a member), `updated_at` (last profile refresh), `departed_at`, `departed_by_id` (moderator, for kicks/bans) and `departure_source` (`moderator` = a person, `bot` = this bot on its own (auto-kick, honeypot), `detected_at_startup` = noticed missing after the bot was offline, date approximate). **Never stored:** reasons, messages, roles.
**Population:** writes are buffered and flushed every 10 s (or at 500 waiting ops) in one transaction (aiosqlite worker thread; consecutive profile writes of one user coalesce). `daily_loop` (first run right after `wait_until_ready`, then every 24 h): prune, then sweep every member of the home guild (`ALLOWED_GUILD_ID`, read from `main`) in 500-row `executemany` chunks; with a fully chunked guild, rows still `member` but gone become `left` (`detected_at_startup`); then the ban list (up to 10000; banned users get a row, ones no longer banned become `unbanned`; needs Ban Members). Listeners: `on_member_join`, `on_member_update` / `on_user_update` (only when name, global name, nick, avatar, colour or bot flag changed), `on_member_remove` (`left`), `on_member_ban` (`banned`), `on_member_unban` (`unbanned`). Precedence banned > kicked > left: a `left` within 5 min of a ban/kick does not overwrite it (Discord sends remove and ban in no fixed order). **Kick detection:** after a remove a background task reads the audit log (`AuditLogAction.kick`, same `pick_actor_entry` matching as `cogs/logging_moderation.py`, one retry); a fresh entry upgrades `left` to `kicked`. When the entry's actor is the bot (every `!kick`, dashboard kick, auto-kick and honeypot kick goes through `ModerationService`), the human moderator comes from the newest `mod_actions` row of that type within 120 s (also the fallback without View Audit Log). Bans get the same attribution. Nothing in `cogs/kick.py`, `ModerationService` or the logging cogs was changed. Listeners never raise into discord.py.
**Retention:** departed users whose `departed_at` (else `updated_at`) is older than `USER_CACHE_RETENTION_DAYS` (365; 0 = forever) are deleted daily and at startup (then VACUUM); members and users still banned are never pruned.
**Resolve fallback** (`internal_api/routes/resolve.py`): an id that `fetch_member` answers NotFound for is looked up in the cache; ids with no row or a row older than 7 days are refreshed with `bot.fetch_user` (at most 10 per request, 5 at a time) and written back (a user never seen before is stored as `left` with no `departed_at`); only if that fails too the id is `null` as before. Transient errors still leave the id out. Members keep the old 4-field shape. Departed entry: `id`, `username`, `display_name`, `avatar_url`, `departed: true`, `status` (`left`/`kicked`/`banned`/`unbanned`; a stale `member` row answers `left`), `departed_at`, `status_changed_at`, `departed_by_id` (string), `departure_source`, `global_name`, `nick`, `color`, `bot`, `joined_at`, `first_seen`, `last_seen`, `cached_at` (all times ISO UTC `...Z` or null). Frontend: `<ResolvedUser>` shows them muted with a `DepartedBadge` (Left / Kicked / Banned / Unbanned, tooltip with the date and "by a moderator"/"by the bot"); the Moderation member header shows the badge too. `src/api/userCache.ts` has `useCachedUser` / `useForgetCachedUser` for a member profile page (no page of its own yet).
**Commands:** `!usercache` (staff only: owner / `STAFF_ROLE_IDS`): status (rows by status, file size, retention); `!usercache forget <@user|ID>` deletes one row now (audited `user_cache_forget`). Dashboard equivalent: `DELETE /api/user-cache/{id}` (`require_staff`, same audit row). A forgotten current member is re-added by the next member event or daily sweep.
**Config:** `USER_CACHE_ENABLED`, `USER_CACHE_PATH`, `USER_CACHE_RETENTION_DAYS` (.env only; restart to change).
**Tests:** `tests/unit/test_user_cache.py` (store with a temp file, kick attribution, internal resolve fallback + forget routes, dashboard resolve gate and forget proxy).
**State:** built and tested 2026-10-28; NOT live until the bot and the dashboard are restarted (the frontend badge is built and degrades to the old rendering until then).

### `cogs/honeypot.py`
**Purpose:** Slash-command front end and gateway listeners for the honeypot trap channel (section 19). All logic lives in `services/honeypot_service.py`; the cog and the internal API call the same service methods.
**Commands:** `/honeypot setup|status|stats|action|enable|disable|appeals|scramble` (guild-only group). Task loop `scramble_check` (every 5 min, starts after `wait_until_ready`): `HoneypotService.scramble_if_due` per guild. Listeners: `on_message` (every guild message, including bot/app/webhook ones since 2026-10-20, is offered to the service, which ignores anything outside the trap channel) and `on_guild_channel_delete` (handles the trap channel being deleted).
**Services:** `HoneypotService`, `ModerationService`.
**Tables:** `honeypot_config`, `honeypot_events`, `honeypot_appeals`, plus `mod_actions`/`audit_log` via `ModerationService`.
**Config:** `APPEAL_BASE_URL`; log channel from `honeypot_config.log_channel_id` or the moderation-log channel.
**Permissions:** `ban_members` (grant or staff) for every slash command that changes something; the read-only `/honeypot status|stats|appeals` need `moderate_members`, the same as the dashboard's honeypot read routes (2026-10-28, from the permission review; built, NOT live until the bot restart).

### `cogs/fortnite_shop.py`
**Purpose:** Fortnite item shop: daily auto-post shortly after the 00:00 UTC rotation, a `!shop` browser, and member wishlists. All logic lives in `services/fortnite_shop_service.py` (see "Fortnite item shop" at the end of this document).
**Help:** listed on the "Fortnite item shop" page of `/help` (`cogs/help_commands.py`).
**Commands:** prefix `!shop` (one item per page with `<< < > >>` buttons and a section jump menu, only the invoker can navigate, 5 min timeout, 10 s per-user cooldown). Slash group `/fortnite` (guild-only): `channel set|clear`, `enable`, `disable`, `jamtracks <on|off>`, `pingrole [role]`, `postnow` (does not count as the daily post), `status`, and for any member `wishlist add|remove|list` (max 25 items each). Loop: `_daily_loop` every 60 s (after a fetch failure, a not-yet-rotated API or a post error it waits 5 minutes before retrying).
**Services:** `FortniteShopService`.
**Tables:** `fortnite_shop_config`, `fortnite_wishlist` (migration 27).
**Config:** `FORTNITE_API_KEY` (optional), `FORTNITE_SHOP_LANGUAGE`, `FORTNITE_SHOP_POST_DELAY_MINUTES`.
**Permissions:** config commands need `manage_schedules` (grant or staff/owner); `!shop`, `/fortnite status` and the wishlist commands are open to members.

### `cogs/dependency_audit.py`
**Purpose:** weekly dependency audit (2026-10-30; built, unit-tested in `tests/unit/test_dependency_audit.py`, live after the next bot restart). `services/dependency_audit_service.py` runs `<venv python> -m pip_audit --format json` (the INSTALLED venv; `pip-audit` is pinned in `requirements.txt`), `pip list --outdated` (only to flag an outdated yt-dlp, which is what breaks music) and `npm audit --omit=dev --json` in `server/frontend` and `pot_server/server`. npm findings below `DEPENDENCY_AUDIT_MIN_SEVERITY` (default `moderate`) and packages in `DEPENDENCY_AUDIT_IGNORE` are counted as `ignored`, not reported (pip-audit has no severity, so every Python finding counts). When something needs action (a finding, an outdated yt-dlp, or a check that could not run) it posts ONE embed, in the same format as the supervisor's alerts and with no ping, to `DEPENDENCY_AUDIT_WEBHOOK_URL`, else `SUPERVISOR_WEBHOOK_URL`. A clean week posts nothing.
**Schedule:** Mondays 05:00 local (after the 04:00 log rotation), checked every 30 min, with catch-up when the last run is older than this week's slot. The last run is the newest `audit_log` row `dependency_audit_run` (detail: counts, ignored, errors, notes, source, `webhook_posted`), so restarts never skip or repeat a week.
**Commands:** `!depaudit` (staff only): run now, reply with the summary, post to the webhook when needed. One run at a time.
**Fixing what it reports (2026-11-02):** the embed's "Fix:" line points to the owner-only Diagnostics > Services > "Dependency updates" panel (or `supervisor.py update-deps apply`), which applies the non-major fixes through the supervisor, tests them, rolls back on failure and restarts what needs it (section 26 "Dependency updates"). The first report it flagged (2026-11-02: `shadcn`, `ts-morph`, `@ts-morph/common`, `minimatch` in the dashboard frontend) came from `shadcn` being listed under `dependencies`; it is only the component-generator CLI (nothing in `src/` imports it), so it was moved to `devDependencies` and the `--omit=dev` audit is clean.

### `cogs/supervisor_control.py`
**Purpose:** Restart parts of the stack from Discord through `supervisor.py`'s control API (section 26). The bot only asks; the supervisor does the stopping and starting.
**Commands:** slash group `/restart` (guild-only): `bot [reason]`, `dashboard [reason]`, `tunnel [reason]`, `all [reason]`, `status` (supervisor status as an embed). All replies are ephemeral. When the bot is not running under `supervisor.py` (or the supervisor is an old copy without the control API), it says so and nothing happens; a cooldown (429) or conflict (409) is reported with the supervisor's message.
**Self-restart:** `bot`/`all` send the request with a 5 s delay; the reply ("Restarting ... back in about Ns") goes out right after the supervisor accepted the request (so a 429/409 is never reported as "restarting") and before the kill: only the small marker write comes first, the `audit_log` row is written after the reply. A marker `logs/supervisor_restart_marker.json` (channel id, requester id, target, time; **no interaction token**) is written; on the next `on_ready` (first one only) the cog deletes it and, if it is under 10 minutes old, posts a normal, non-pinging channel message "Back online after Xs (restart requested by @user)." in the channel `/restart` was used in. Old-format markers (with a token, no channel id) are discarded. Any failure there is logged, never raised.
**Helper for bot code:** `await services.supervisor_client.request_restart(target, reason=..., requested_by="bot", source="bot")`; `get_status()`, `get_logs()`; raises `SupervisorUnavailable` (connection refused/timeout), `SupervisorCooldown` (429, `.retry_after_s`) or `SupervisorError` (`.status`, `.error`, `.message`).
**Tables:** `audit_log` (accepted requests only: action `supervisor_restart`, target_type `service`, detail `{target, reason, via: "discord", accepted, skipped}`; also reaches the live `audit` topic). Dashboard-initiated restarts are not in `audit_log` (the bot may be down); they are in `logs/supervisor/supervisor.log` and the webhook alert.
**Config:** `SUPERVISOR_CONTROL_PORT`, `SUPERVISOR_CONTROL_SECRET` (root `config.py`).
**Permissions:** staff only (`OWNER_ID` or `STAFF_ROLE_IDS`, `PermissionService.is_staff_interaction`, same check as `/bot permissions`); no new permission string.

### `cogs/help_commands.py`
**Purpose:** Three paginated help commands. Most pages are hard-coded documentation-as-code; `/help` appends a Music section (the same generator as `!musichelp`, pointing at `!musichelp` and `/music help`) and `/devhelp` appends diagnostics/honeypot/tooling pages that are derived at call time from the registered music prefix commands (`extras={"staff_only": True}` marks staff-only ones) and the honeypot slash commands.
**Commands:** `/help` (staff-only, full feature walkthrough), `/bdayhelp` (open, birthday-only subset), `/devhelp` (staff-only, technical/architecture reference; its "Diagnostics & debug commands" page lists `/restart` and its "Internal tooling" page mentions `supervisor.py` and port 8767).
**Tables:** none.
**Permissions:** `/help` and `/devhelp` staff-only (via slightly different sync/async staff checks); `/bdayhelp` open.

### `cogs/info.py`
**Purpose:** Read-only utility commands.
**Commands:** `/userinfo`, `/serverinfo`, `/avatar`, `/ping`.
**Tables:** none. **Permissions:** none.

### `cogs/permissions.py`
**Purpose:** Legacy dead code — hard-coded `is_staff`/`is_staff_ctx`/`is_staff_interaction` helpers using only `config.OWNER_ID`/`STAFF_ROLE_IDS`. **Not a `commands.Cog` subclass, has no `setup()` function, and is not in `main.py`'s load list** — cannot be loaded, referenced only by old files under `archive/`. Kept for reference; safe to ignore or delete.

---

## 11. Deployment

### Current deployment

- **Windows machine, no Docker/systemd/CI.** Services run from the project's `venv` via `.bat` scripts, each in its own console window ("this window must stay open"; closing it stops that process; no auto-restart).
- **Bot:** `run_bot.bat` (`venv\Scripts\python.exe main.py`). This one process also hosts the internal API (`127.0.0.1:8766`) and spawns the PO-token server (`:4416`).
- **Dashboard:** `run_dashboard.bat` or `server\start_dashboard.bat` (`venv\Scripts\python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8080`).
- **Public exposure:** `server\start_tunnel.bat` runs `cloudflared tunnel run idlesys`, forwarding `dash.idlesys.xyz` to `localhost:8080`. No nginx/Caddy.
- **All three at once:** `start_all.bat` (bot, wait 5s, dashboard, tunnel). It refuses to run if `.env` is missing.
- **Supervisor (`supervisor.py`):** alternative to the `.bat` launchers (do not run both). Starts bot, then (once the internal API `/health` answers) dashboard, then the tunnel; restarts any that exit (backoff 3s doubling to 120s, reset after 120s uptime); force-restarts the bot/dashboard if their port fails 3 health checks in a row (20s apart, after a 90s startup grace); refuses to start a service whose port is already taken (e.g. an old `run_bot.bat` window). Crashes post to a Discord webhook (`SUPERVISOR_WEBHOOK_URL`, ping `SUPERVISOR_PING` or `OWNER_ID`) with exit code, uptime and the last lines of output. Logs in `logs/supervisor/` (`supervisor.log`, `bot.log`, `dashboard.log`, `tunnel.log`; append-only; from supervisor v2.1 rotated weekly into `logs/archive/` together with `bot_startup.log`, see section 26 "Weekly log rotation"). `install_scripts\windows\06-install-supervisor-autostart.bat` registers a logon Task Scheduler task running it with `pythonw`. The scheduled-task installer needs admin rights (it failed with Access denied unprivileged); the autostart actually in use is a Startup-folder shortcut `kick-bot-supervisor.lnk` (pythonw supervisor.py), with the old `kick-bot*.lnk` shortcuts moved to `Startup\disabled-by-supervisor`. Webhook delivery tested; the live stack has been running under `supervisor.py` (started from the Startup shortcut, with the pre-section-26 code). **Control API, restart from Discord/dashboard and the single-instance lock: see section 26** (built and unit-tested; the running supervisor must be restarted once to load that code). Controlling it without its console: `sv.bat` in the repo root (menu, `sv help`; section 26 "sv.bat").
- **At login:** `install_scripts\windows\05-install-autostart.bat` creates Startup shortcuts for `run_bot.bat` and `run_dashboard.bat` only. The tunnel is not included; use `start_all.bat` (e.g. placed in `shell:startup` by hand) if you want it to autostart too.
- The PyInstaller build in `build_scripts/` is legacy and not how the live bot runs (section 3).

### Restart requirements (what needs a restart of what)

| Change | Restart |
|---|---|
| Any `.py` change under `cogs/`, `services/`, `core/`, `database/`, `internal_api/`, `main.py`, `config.py` | Bot (`main.py`). Also restarts the internal API and PO-token server. |
| New bot-DB migration | Bot (applied automatically at startup) |
| `.env` change | Whichever process reads it (bot and/or dashboard); nothing hot-reloads `.env`. Under `supervisor.py` with the section 26 code, a child restarted by the supervisor (`/restart`, the Diagnostics card, the CLI, or a crash restart) is started with a FRESH read of `.env`, so restarting the bot/dashboard is enough. Exceptions that need a supervisor restart: `SUPERVISOR_*` keys, and `INTERNAL_API_SECRET` while `SUPERVISOR_CONTROL_SECRET` is unset (the supervisor keeps the old control secret, so the restarted bot/dashboard would get 401 from the control API). A supervisor started before the section 26 code passed its startup copy of `.env` to every child (the live one runs the section 26 code since 2026-10-14). |
| Dashboard setting marked "live" (`ticket_chat_logging_enabled`, `ticket_archive_purge_days`, every `antispam_*` field) | None (about 5s) |
| Any other Settings-page value | Bot restart (`main.py` applies saved values before loading cogs; the Settings page shows "Pending restart" and has "Restart bot now" and "Save & restart bot" buttons) |
| `server/**/*.py` (routes, `app.py`, `config.py`) | Dashboard (uvicorn runs without `--reload`) |
| Frontend `.tsx`/`.ts` | Rebuild only (`npm run build`); no server restart, refresh the browser |
| New dashboard-DB (Alembic) migration | Run `alembic upgrade head`, then restart the dashboard |
| `setup_server/config.py` | Nothing; just re-run `setup_server.py` |
| `supervisor.py` or a `SUPERVISOR_*` `.env` key | The supervisor itself (which also restarts everything it manages; see section 26 for the exact steps) |

Restarting the bot briefly makes the dashboard return `502 internal_api_unavailable`; it recovers by itself. **Do not kill the bot casually if someone is using it;** there is no graceful drain.

### Updating

1. Pull/copy new source.
2. If `requirements.txt` or `server/requirements.txt` changed: `venv\Scripts\pip install -r requirements.txt -r server\requirements.txt`.
3. If the frontend changed: `cd server\frontend && npm install && npm run build`. The dashboard backend does not need restarting; a browser refresh picks up the new `dist/` files. Always build before deploying: the backend serves `dist/`, not the dev server.
4. Bot-side Python changed or a new bot-DB migration was added: restart the bot.
5. If a new dashboard-DB migration was added: `venv\Scripts\python.exe -m alembic -c server\alembic.ini upgrade head` from the repo root.
6. If dashboard Python changed: restart the dashboard.

### Restarting

Under `supervisor.py` (with the section 26 code loaded): `/restart bot|dashboard|tunnel|all`, Diagnostics -> "Services (supervisor.py)" on the dashboard, or `venv\Scripts\python.exe supervisor.py restart <target> [reason]` on the host (each re-reads `.env` for the restarted child; see the table above for the keys that still need a supervisor restart). Manual: close the console window (or `Ctrl+C`) and re-run the script. Unless you run `supervisor.py` (auto-restart + Discord webhook crash alerts), there is no process supervisor, so a crash needs a human to notice. `GET /api/status` (and the public `/status` page) reports whether the dashboard and the bot's internal API are up.

### Troubleshooting deployment

See section 13.

---

## 12. Maintenance

### Backups

**None currently exist — this is the top maintenance gap.** Recommended minimum: a scheduled Windows Task (or, post-Linux-migration, a cron job / systemd timer) that copies `bot.db` and `server/dashboard.db` to a separate location on a regular interval, since both are single SQLite files that can be safely copied while stopped (or via SQLite's `.backup` / `VACUUM INTO` for a live copy without stopping the process).

### Updating dependencies

Bump the pinned version in `requirements.txt` / `server/requirements.txt` / `server/frontend/package.json`, reinstall, and re-run the test suite (currently thin — see §20) plus a manual smoke test, since automated coverage won't catch most regressions today.

### Database migrations

New bot-DB migration: add a numbered `.sql` file + register it in `main.py`'s `MIGRATIONS` list (applies automatically next startup). New dashboard-DB migration: edit `server/db/models.py`, then `alembic revision --autogenerate` + `alembic upgrade head` (see §7).

### Discord API changes

Watch `discord.py`'s changelog before bumping its pinned version — this bot uses privileged intents (`members`, `message_content`, `presences`), which require corresponding toggles in the Discord Developer Portal; a fresh bot application or a token reset requires re-enabling these.

### Checking logs

The bot process logs via `core/logging_setup.py` (structured, UTF-8-safe) to stdout (under `supervisor.py`: `logs/supervisor/bot.log`) and, only when NOT run under `supervisor.py`, to `bot_startup.log` in the working directory (a `logging.FileHandler` kept open for the bot's lifetime, despite the name a full copy of the log). The supervisor sets `KICKBOT_SUPERVISED=1` in its children's environment and `setup_logging` then skips that handler, because stdout already lands in `logs/supervisor/bot.log` and the open handle blocked the file's weekly rotation (test: `tests/unit/test_logging_setup.py`). Built and unit-tested 2026-10-14; takes effect at the next supervisor restart. No centralized log aggregation exists. Under `supervisor.py` v2.1 the supervisor logs and `bot_startup.log` are rotated weekly into `logs/archive/` (gzip, kept 28 days; `bot_startup.log` immediately under the supervisor, where the bot no longer writes it; a bot started outside the supervisor holds it open, and then it waits for the next supervised bot start); `kicks.log` and `logs/pot_server.log` are never rotated by it (section 26). `audit_log` (DB table) is the closest thing to a durable, queryable action history, viewable via `/bot audit` or the dashboard.

---

## 13. Troubleshooting

**Bot won't start.**
- Check for a `RuntimeError` at the very top of the console/log output — `config.py` raises immediately if `DISCORD_TOKEN` or `INTERNAL_API_SECRET` is missing from `.env`.
- Check `bot_startup.log` in the working directory — `main.py` writes there if logging itself couldn't be configured (e.g. before `.env` is read), since a frozen `.exe` launched by double-click closes its console before an error could otherwise be read. Once logging is configured, every log line also goes there (`core/logging_setup.py`). Older content is in `logs/archive/bot_startup.<date>.log.gz` after a weekly rotation (section 26).
- Confirm privileged intents (Server Members, Message Content, Presence) are enabled in the Discord Developer Portal for this bot application — `discord.py` will fail to connect otherwise.

**Dashboard login fails or loops locally.**
- Use `http://localhost:8080`, not `http://127.0.0.1:8080`. `DISCORD_OAUTH_REDIRECT_URI` and `FRONTEND_BASE_URL` default to the `localhost` origin and the redirect URI must match the Discord Developer Portal registration exactly; the session and OAuth-state cookies are also host-scoped, so starting the flow on `127.0.0.1` and returning on `localhost` loses the state cookie. The CSRF middleware additionally rejects non-GET requests whose `Origin` is not one of `FRONTEND_BASE_URL` / `localhost:PORT` / `127.0.0.1:PORT`.
- Behind the Cloudflare tunnel, set `FRONTEND_BASE_URL` and `DISCORD_OAUTH_REDIRECT_URI` to the public `https://dash.idlesys.xyz` values and `DASHBOARD_COOKIE_SECURE=auto` (or remove the line; `auto` is the default and also keeps `http://localhost` working). `false` sends the session cookie without `Secure` even over HTTPS.

**Dashboard unavailable / "internal_api_unavailable" error in the browser.**
- This means the dashboard process is up but can't reach the bot process's embedded `internal_api` — check that `main.py` is actually running (it must be running for the dashboard to show any live data).
- Confirm `INTERNAL_API_SECRET` matches exactly between the bot's `.env` read and the dashboard's `.env` read (they read the *same* file, but double-check nothing overrides it).
- Confirm `INTERNAL_API_HOST`/`INTERNAL_API_PORT` agree between both processes (default `127.0.0.1:8766`).

**Database migration failure (bot DB).**
- Since each migration file isn't fully transactional (see §7), a failure partway through a multi-statement file can leave some DDL applied without the `schema_migrations` row recorded. On next start the runner retries the whole file. That is safe for `IF NOT EXISTS` statements but **not** for `ALTER TABLE ADD COLUMN` (duplicate column error) or migration 015's table rebuild. To recover: inspect the table with `sqlite3 bot.db`, finish or undo the partial change by hand (for 015, look for a leftover `birthday_settings_new`), and if the whole file's effects are already in place, insert the missing `schema_migrations` row manually. Back up `bot.db` first.
- Check the `.sql` file for a syntax error introduced by a recent edit; the runner does not validate SQL before executing it.

**Database migration failure (dashboard DB / Alembic).**
- Confirm you're running the command from the **repo root**, not from inside `server/` — `alembic.ini`'s `script_location` is CWD-relative.
- Confirm the `-c server\alembic.ini` flag is present; without it, Alembic looks for `alembic.ini` in the CWD and won't find it.

**Discord authentication issues (dashboard OAuth).**
- Confirm `DISCORD_OAUTH_REDIRECT_URI` exactly matches a redirect URI registered in the Discord Developer Portal for this application (including scheme/port) — Discord rejects any mismatch.
- Confirm `DISCORD_CLIENT_ID`/`DISCORD_CLIENT_SECRET` are for the same Discord application as `DISCORD_TOKEN`.
- A `"not_a_member"` state on the dashboard means the OAuth login succeeded but the logged-in Discord user isn't a member of `DASHBOARD_GUILD_ID` — expected behavior, not a bug.

**Settings not updating on the dashboard.**
- Only `ticket_chat_logging_enabled`, `ticket_archive_purge_days` and the `antispam_*` fields apply within ~5 seconds. Every other field applies at the next bot start (the page marks it "Pending restart" and shows saved vs running). After a restart, the bot log should contain `Settings: applied N dashboard override(s): ...`; a `Settings: <key>: Stored value ... is invalid` warning means a malformed row was skipped (the GET response flags it as `invalid_stored`). A `... already imported before settings were applied` warning means a cog or `ticket_service` was imported before the apply step in `main.py` (§6).

**Slash command missing or stale in Discord.**
- Commands sync globally from `on_ready`; a brand-new command can take a while to show up, and the Discord client may need a restart (Ctrl+R). Check the log for `Synced N slash command(s) globally` and for `FAILED to load cog` lines.

**Music: "Sign in to confirm you're not a bot" / 403s / no audio.**
- Confirm the PO-token server is running (`http://127.0.0.1:4416/ping`) and was built (`pot_server/server/build/main.js` exists). See section 18. Also check `cookies.txt` freshness, that `node` and `ffmpeg` are on `PATH`, and `MUSIC_PLAYER_CLIENTS`.

**A `!command` does nothing.**
- The prefix is `!`. Most prefix commands are `hidden=True` and permission-gated (they answer "no permission" or nothing if you lack the grant); the message-content intent must be enabled in the Developer Portal.

---

## 14. Development Guide

### Adding a new cog

1. Write the cog module under `cogs/`, ending with an `async def setup(bot)` function that builds its dependencies from `bot.db`/`bot.permission_service` and calls `bot.add_cog(...)`. Real example pattern (from `cogs/boost.py`):
   ```python
   async def setup(bot: commands.Bot) -> None:
       if not hasattr(bot, "db") or bot.db is None:
           log.warning("bot.db not available — cog not loaded.")
           return
       repo = SomeRepository(bot.db)
       service = SomeService(repo)
       await bot.add_cog(SomeCog(bot, service, bot.permission_service))
   ```
2. Add the module path to the `COGS` list in `main.py`.
3. Restart the bot process.
4. (Legacy frozen build only) add the module to `build_scripts/build.bat`'s hidden imports.

### Adding a service

Construct it inside the consuming cog's own `setup(bot)` (the common case), passing in whatever repositories it needs. For a bot-wide singleton every cog needs (like `PermissionService`), instead attach it once in `main.py` before cogs load, as `bot.<name>`.

### Adding a repository

Add a class under `database/repositories/`, subclassing `BaseRepository` (which just stores `self._db`). Hand-write the SQL for each method — there is no generic CRUD/ORM layer to lean on. Add a corresponding migration file if a new table is needed (see §7).

### Adding an internal API route

1. Create `internal_api/routes/<name>.py` exporting an `APIRouter` named `router`, guarded by `dependencies=[Depends(require_internal_secret)]`.
2. Import it and add `app.include_router(<name>.router)` in `internal_api/app.py`'s `create_app()`.
3. Wrap responses in `stringify_snowflakes` and add any new snowflake-holding key names to `SNOWFLAKE_KEYS`.
4. Restart the bot.

### Adding a dashboard backend route

1. Create `server/routes/<name>.py`: check `require_permission`/`require_staff` first, then call the matching method on `server/services/internal_client.py` (add one there too if it doesn't exist yet), translating `InternalAPIError` into a 502.
2. Import it and add `app.include_router(<name>.router)` in `server/app.py`, **before** the SPA-fallback catch-all route (which would otherwise shadow it).

### Adding a dashboard frontend page

1. Create `server/frontend/src/pages/<feature>/<Name>Page.tsx`.
2. Lazy-import it and add a `<Route>` under the `/dashboard` parent in `src/app/routes.tsx`.
3. Add one entry to `NAV_LINKS` in `src/components/layout/navLinks.ts` (path, label, icon, `requiredPermission`, `group`: overview | moderation | community | system) — this single array drives the sidebar, drawer, bottom nav, breadcrumbs and the command palette.
4. Follow the PageHeader pattern (`PageContainer` + `PageHeader`) with skeleton / empty / error / 403 states, and use design tokens; live pages use `useLiveTopic`.

### Adding a permission

Add the new string to `VALID_PERMISSIONS` in `services/permission_service.py` (the `/bot permissions` choices in `cogs/advanced_commands.py` are built from it), to `_ALL_PERMISSIONS` in `server/routes/me.py`, and optionally a short column label in `PERMISSION_LABELS` (`server/frontend/src/pages/permissions/PermissionsPage.tsx`). `tests/unit/test_permission_list_parity.py` fails if these drift. Discord allows at most 25 choices.

### Adding a bot-DB migration

See section 7. Never edit an applied migration; prefer `CREATE ... IF NOT EXISTS`, and remember `ALTER TABLE ADD COLUMN` cannot be safely re-run.

### Template placeholders

**Rebuilt 2026-10-30 (built and unit-tested in `tests/unit/test_placeholders.py`; the bot side and the dashboard routes go live with the next bot + dashboard restart; the editor panel is in the live frontend build but shows an error until then).** `core/placeholder_engine.py` holds 106 placeholder forms in 9 categories (Member, Server, Channel, Date & time, Moderation, Staff, Tickets, Roles, Lookups & extras). Syntax: `{name}`; `{name:argument}` for the ones that take one (`{channel:general}` / `{role:Moderator}` / `{member:name}` / `{emoji:heart}` look up by ID, exact name, case-insensitive name, then part of the name, and `{role:...}` never returns @everyone; `{joindate:R}`, `{accountage:R}`, `{servercreated:R}`, `{expires:f}`, `{time:F}` take a Discord timestamp style t/T/d/D/f/F/R; `{date:%A}` a strftime format in UTC; `{random:a;b;c}` picks one); `{name|fallback}` uses another placeholder (`{nickname|user}`) or literal text (`{reason|No reason given}`) when the value is empty. Unknown names stay as written; a placeholder that raises renders empty (or its fallback). `{mention}`, `{userid}` and `{id}` fall back to `extra["target_user"]` / `extra["target_id"]` so ban/unban logs of non-members still name the person.

Feature data comes in `RenderContext.extra`: every mod log now passes it (`services/moderation_service.py` `send_mod_log(..., extra=)` plus `log_extra=` on kick/ban/unban/timeout/untimeout; `cogs/moderation.py` `_send_mod_log(..., extra=)`; dashboard warn/delwarn and purge in `internal_api/routes/moderation.py` / `member_tools.py`): `action`, `reason` (`{reason}` = "No reason given" when the action had none, blank outside mod logs), `case_id` (the `mod_actions` id), `duration_minutes`/`expires_at` (timeouts), `delete_days` (bans), `warning_id`/`warning_count` (warn logs), `kick_number` + `source` (cogs/kick.py), `purge_count`/`purge_channel`/`purge_filter` (`services/purge_service.describe_filter`), `target_id`/`target_user`, `source` (`dashboard`/`command`/`automatic`/`button`). The verify message now gets the verifier as `actor` (`{moderator}`/`{staff}`) and `source` (`verify_member(..., source=)`: ticket button `button`, `!verify` `command`, dashboard `dashboard`). Before this, the four `mod-*-log` templates in the live guild had no way to show the reason (the built-in fallback embed does).

Every placeholder is registered with metadata: `register_placeholder(name, fn, category=, description=, where=, example=)` / `register_arg_placeholder(name, fn(ctx, arg), category=, description=, example=, where=)`; `catalogue()` lists them and `sample_context(guild, member, channel)` builds example data (the viewer as member AND actor, ticket 42, an example reason/duration/case and so on, `extra["sample"] = True`). That one source feeds: internal `GET /guilds/{id}/placeholders?user_id=` (each entry plus an `example` rendered by the real functions, capped at 300 chars) -> dashboard `GET /api/templates/placeholders` (`manage_templates`, session user as the sample, registered above `/templates/{template_id}`); the template editor's **Placeholders** panel (`src/pages/templates/PlaceholdersPanel.tsx`, hook + preview resolver in `src/api/placeholders.ts`: search, category chips, click inserts at the cursor of the last focused text field via `execCommand("insertText")` with a native-setter fallback, so the field's own onChange runs; the live preview resolves every text and URL field with the bot's example values, replacing the old three hardcoded samples, one of which (`{member_count}`) never existed); Discord `/template placeholders [category]` (overview of the syntax + counts, or one category as ephemeral embeds, `manage_templates`); `/template preview`, the embed editor's Preview button and the bot-side `GET .../templates/{id}/preview?user_id=` (the dashboard passes the session user) now render with `sample_context` instead of blanks; the help page's Placeholders section lists the favourites. `core/renderer.py` clips every rendered text to Discord's limits (content 2000, title 256, description 4096, field name 256 / value 1024, footer 2048, author 256), since placeholders such as `{roles}` can grow text past what was validated at save time.

To add one: call `register_placeholder` (or `register_arg_placeholder`) in `core/placeholder_engine.py` with a category from `CATEGORIES`, a description and `where`; the catalogue, the editor panel, `/template placeholders` and the tests' "every entry has metadata and renders" checks pick it up. Put new feature data into `RenderContext.extra` at the call site, never by importing feature code into the engine.

---

## 15. Security Notes

**Performance notes (2026-10-30; built, tested, live).**
- **Pooled loopback clients:** `server/services/http_pool.py`. Creating an `httpx.AsyncClient` builds a TLS context (~280 ms on this Windows host, even for plain-http loopback URLs). The dashboard used to create one per internal call, so a typical request (permission check + data) spent ~560 ms on that while the bot answered in 1-5 ms. `internal_client._client()` and `supervisor_client._client()` now borrow ONE pooled client per event loop, which is never closed after a call and is closed in the app lifespan. Measured: `/api/status` locally 248 ms -> 5 ms; through Cloudflare ~430-730 ms -> ~170 ms, which is now just the network. No responses are cached, so permission checks stay live (a removed role or grant applies on the next request); `tests/unit/test_http_pool.py` pins both.
- **Frontend:** `src/app/pageLoaders.ts` holds the dashboard pages' dynamic imports. `routes.tsx` lazy-loads them, and AppShell calls `preloadDashboardPages()` once after sign-in (idle callback; skipped with Save-Data), so page switches never wait for a chunk (assets are immutable-cached). The owner-only Debug page is not preloaded. React Query `staleTime` is 15 s globally (`app/providers.tsx`): returning to a page within 15 s reuses its data; mutations still invalidate and live topics push changes.
- **Browser tab titles:** `<page> · Keystone` (AppShell, from `navItemForPath`). The profile menu shows the build time (`__BUILD_TIME__`, Vite `define`, typed in `src/build-info.d.ts`) and a "Source code" link (`src/lib/project.ts` `SOURCE_CODE_URL`, shared with `/hub`).

**Security audit 2026-10-30:** the full report (credential inventory, endpoint matrix, findings for this deployment) is kept privately, outside the repository. What it changed is listed here; built and tested (`tests/unit/test_security_audit.py`); live after the next bot + dashboard restart. Mechanisms it added:
- `server/app.py` `SecurityHeadersMiddleware`: `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy` (camera/mic/geolocation off), HSTS 180 days only on HTTPS (no includeSubDomains, so other subdomains of the same domain are unaffected). No script CSP yet.
- `server/app.py` `_AccessLogScrubber` on the `uvicorn.access` logger: OAuth `code`/`state`/`token` query values and appeal-link tokens in paths are replaced with `<redacted>` before the line is written (`scrub_access_path`).
- `server/services/housekeeping.py`: at dashboard start and every 6 h, deletes sessions idle longer than `SESSION_MAX_AGE_SECONDS` (they used to stay until their cookie came back) and prunes error reports. Gated by `DASHBOARD_HOUSEKEEPING` (default true; `tests/conftest.py` sets it false because the job uses the REAL engine). Because sessions are now pruned, Diagnostics > Dashboard no longer shows a per-user session count (it read like a lifetime login counter but only ever counted live sessions): `by_user` is `{user_id, username, last_seen}`, rendered with `ResolvedUser` (avatar + display name + link) plus `@username`.
- `server/auth/oauth.py` `revoke_token`: the dashboard login revokes its Discord access token right after the identity + membership reads (the appeal sign-in already did).
- `cogs/music.py` `url_block_reason`: URLs given to `!play` / the dashboard are only handed to yt-dlp when they are http(s) to hosts that resolve to public addresses only (SSRF: loopback, LAN, link-local, CGNAT, reserved refused; YouTube hosts skip DNS). Residual: redirects followed by yt-dlp, DNS rebinding.
- `database/repositories/ticket_repo.py` `_PURGE_WHERE`: the archived-ticket purge counts the earlier of `closed_at`/`archived_at`, so legacy rows with `archived_at` NULL or backfilled are purged too.
- Route-walk regression tests: every dashboard `/api` route must answer an anonymous request with 401 (owner-only: the hidden 404) unless listed in the test's `PUBLIC` set, and every internal route must answer 401 without the bearer. A new route needs a guard, or an entry in `PUBLIC` with a reason.

### Secrets

All secrets live in a single root `.env` (never commit it; `.env.example` holds placeholders only). `cookies.txt` (yt-dlp/YouTube cookies), `bot.db`, `server/dashboard.db` (session rows; Discord OAuth tokens are no longer stored, and the old ones are nulled by Alembic `b3c1d2e4f5a6`), `message_log.db`, `user_cache.db` (who left / was kicked / banned, and by which moderator) and `bot_startup.log` also contain sensitive data. A root `.gitignore` covers `.env`, `*.db`, logs, `cookies.txt`, `archive/config.json`, `*.zip` and `*.7z`, and since 2026-10-30 the project is the public git repository github.com/KeiraOMG0/keystone ("Keystone", formerly kick-bot): before every commit check `git status` for anything new that holds secrets or member data, and extend `.gitignore` first. The single-guild guard (`ALLOWED_GUILD_ID`) is enforced by the bot leaving other guilds, not by the internal API. read by both `config.py` (bot) and `server/config.py` (dashboard, which resolves the path independently since it may run from a different CWD). Neither `config.py` file is ever exposed to the dashboard frontend — `SETTINGS_SCHEMA` is an explicit allowlist that structurally cannot include `DISCORD_TOKEN`, `INTERNAL_API_SECRET`, `DISCORD_CLIENT_SECRET`, or `SESSION_COOKIE_SECRET`.

### Permission boundaries

See §8 in full. Key point: the permission-grants page and the Settings page are both gated on `is_staff()` (a hard, config-defined floor), never on a revocable fine-grained permission — this specifically prevents self-escalation.

### Internal API authentication

`internal_api` binds to loopback only by default and validates `Authorization: Bearer <INTERNAL_API_SECRET>` via `hmac.compare_digest` (constant-time comparison, avoiding timing side-channels). Only `GET /health` is unauthenticated. It performs **zero** user-level authorization itself — anyone holding the secret can call any endpoint for any guild ID. This is a deliberate trust boundary: `internal_api` trusts the dashboard process to have already authorized the specific user/action before calling it.

### Database safety

No SQL injection surface was found in the reviewed repositories — all queries use parameterized placeholders (`?`), never string interpolation, throughout `database/repositories/`. No automated backup exists (§7/§12 — a real gap to close). Migrations are not fully atomic per-file (§7) — rely on idempotent DDL, not runner-enforced transactions.

### Snowflake handling

Discord IDs are 64-bit and exceed JavaScript's `Number.MAX_SAFE_INTEGER` (2^53−1); a bare JSON number silently loses precision the moment a browser's `JSON.parse` reads it, and this is unfixable client-side. Fix: `internal_api/snowflakes.py`'s `stringify_snowflakes()` recursively walks every API response and converts any value under a known-snowflake key (`guild_id`, `channel_id`, `role_id`, `user_id`, `moderator_id`, and ~20 more) to a string before it leaves the API. DB autoincrement IDs (`id`, `ticket_id`, `warning_id`, etc.) are deliberately excluded and remain numeric. **Convention for new code:** any new field holding a Discord snowflake must be added to `SNOWFLAKE_KEYS` and must never be wrapped in `Number()` on the frontend.

---

## 16. Future Improvements

- **Docker migration.** No Dockerfile/compose exists for this project (`pot_server/server/Dockerfile` is upstream's, for the PO-token server alone). Containerizing the bot + dashboard would fix the "no process supervision, console window must stay open" problem directly, and make the Linux-portability work below nearly free.
- **Better testing.** `tests/integration/` currently has exactly 3 tests. The permission system, ticket lifecycle, and settings live-reload logic are the highest-value places to add coverage given their complexity and security relevance.
- **Automated deployment / CI.** No CI pipeline exists. Even a minimal GitHub Actions workflow running `tsc -b`, the existing pytest suite, and a frontend build on every push would catch a meaningful class of regressions today.
- **Backups.** No automated backup process exists for either SQLite database — see §12 for a concrete minimum recommendation.
- **Additional monitoring.** No centralized logging, uptime checks, or alerting exist. At minimum, a process-liveness check (e.g. Cloudflare Tunnel's own health check, or a simple external ping to `/health`) would catch a silent crash faster than "someone notices the dashboard is down."
- **Cross-platform / cloud hosting.** See below — the path to a Linux cloud host is short, but not yet done.

### Path to Linux hosting (minimal-change assessment)

The bot and dashboard are pure Python with no Windows API dependency — `aiosqlite`, `fastapi`, `uvicorn`, and `discord.py` are all fully cross-platform. Concretely, moving to a Linux host requires:

1. **`MUSIC_NODE_PATH`** is already auto-detected via `shutil.which("node")` (no Windows default any more); only set it if `node` is not on `PATH`. The PO-token server's `subprocess.Popen` call and the QOTD Ollama launcher are cross-platform (the latter uses `CREATE_NO_WINDOW` only when present).
2. **Install system dependencies** Linux-side: `ffmpeg` (discord.py voice playback shells out to it) and Node.js (yt-dlp-ejs).
3. **Replace the `.bat` launch scripts** with `.sh` equivalents, or — better, since it also fixes the "no process supervision" gap — real `systemd` unit files for the bot and dashboard processes, with `Restart=on-failure`.
4. **Cloudflare Tunnel** (`cloudflared`) has native Linux builds; the tunnel config (`~/.cloudflared/config.yml`) is portable with only path adjustments.
5. Everything else — migrations, the SQLite databases, the FastAPI apps, the built React `dist/` — is already OS-agnostic and needs no changes.

In short: a few shell/systemd scripts and two system packages (`ffmpeg`, `node`) installed on the target host. No architectural changes are required.

### `install_scripts/` (already exists)

`install_scripts/{windows,macos,linux}` already implement the cross-platform installer (tools check, venv + both requirements files, `.env` from `.env.example`, `alembic upgrade head`, frontend build). Remaining gaps: it does not build `pot_server/` (`npm ci && npx tsc`), does not install/configure `cloudflared`, and on macOS/Linux there are no equivalents of the run scripts or of the Windows autostart installer (a `systemd` unit per process is the natural next step). See `install_scripts/README.md`.

---

## 17. Server provisioning (`setup_server`)

`python setup_server.py [--check] [--dry-run]` (run from the repo root, with the venv) provisions a Discord guild to match `setup_server/config.py`, the single source of truth for roles, categories, channels, and permission overwrites (referenced by name, never ID). It logs in with its own `discord.Client` using `DISCORD_TOKEN` (it does not talk to the running bot), so the bot may be running or stopped.

**Target guild.** `SETUP_GUILD_ID` if set; otherwise the bot's only guild; if the bot is in several guilds and the variable is unset, it aborts. `setup_server/server_state.json` records the guild ID plus name-to-ID maps for roles/categories/channels; if it was built for a different guild the tool refuses to run until you delete the file. The file is a rebuildable lockfile, not a secret.

**Flow (apply mode).** `ensure_roles` -> `ensure_categories` -> `ensure_channels` -> `apply_permissions`, saving state after each step, then prints the actions taken and the `.env` lines it can derive (`UNVERIFIED_ROLE_ID`, `VERIFIED_ROLE_ID`, `LOG_CHANNEL_ID` from the `moderation-log` channel). Paste those into `.env` yourself and restart the bot.

**Modes.** `--check` prints a drift report (missing roles/categories/channels, and `server_state.json` entries pointing at deleted objects) and exits non-zero on drift. `--dry-run` is an alias of `--check` (Discord has no simulated writes).

**What it defines today:** staff tiers (Admin, Sr. Mod, Mod, Staff), Verified/Unverified, ping roles (QOTD, Announcement, Event, Giveaway, Tournament), game/region/timezone/colour self-roles with section divider roles, and the categories Staff, Unverified, Verification, Verify Tickets, Help, Information, Gamer Girls Hub, Zero Build, Build, Miscellaneous, Voice with their channels. It does not know about the honeypot category/channel; that is created by `/honeypot setup` or the dashboard.

**Limitations and gotchas (read before running it on a server people have edited by hand):**
- **Create-only for roles.** Existing roles are matched by exact name and never updated (colour, hoist, permissions, position); roles not in the config are never touched or deleted.
- **Names are matched, not verified.** `--check` only tests presence by name (channels by name + category name). It does not detect permission drift, moved channels, or renamed objects (a renamed object looks "missing" and would be recreated).
- **Permissions are overwritten.** `apply_permissions` rewrites the overwrites of every category and channel listed in the config to match it exactly, and for channels with no overwrites in the config it calls `sync_permissions=True` against their category. Any overwrite that was hand-edited in Discord on a listed channel or category will be reverted. `--check` will not warn you about this.
- **State keyed by name.** `channels` in `server_state.json` is a flat name-to-ID map, so two channels sharing a name in different categories would collide.
- Because it does not clean up, removing something from `config.py` leaves it in Discord.
- The bot's own migration-independent settings (role/channel IDs in `.env` or the dashboard Settings page) are not updated automatically.

---

## 18. Music and the PO-token server

`cogs/music.py` plays audio with yt-dlp + ffmpeg (prefix commands, section 10). YouTube increasingly requires a proof-of-origin (PO) token, so the repo vendors `bgutil-ytdlp-pot-provider` in `pot_server/` (`server/` = Node HTTP server, `plugin/` = yt-dlp plugin; `bgutil-ytdlp-pot-provider` is also in `requirements.txt`).

- **Build once:** `cd pot_server\server && npm ci && npx tsc` produces `pot_server/server/build/main.js` (the default `MUSIC_POT_SERVER_SCRIPT`).
- **Lifecycle:** `main.py` creates one `PotServerService` (`services/pot_server_service.py`), exposes it as `bot.pot_server_service`, starts it from `on_ready`, and stops it on shutdown. It listens on `MUSIC_POT_SERVER_HOST:MUSIC_POT_SERVER_PORT` (default `127.0.0.1:4416`, `/ping` health endpoint). If a healthy instance is already listening it is adopted rather than restarted.
- **Supervision:** an optional watchdog (`MUSIC_POT_SERVER_WATCHDOG`) pings every `MUSIC_POT_SERVER_HEALTH_INTERVAL` seconds and restarts a dead server with exponential backoff, giving up after `MUSIC_POT_SERVER_MAX_RESTARTS` consecutive failures. Output goes to `logs/pot_server.log` (rotated at about 1 MB) with a `.pid` file beside it; the service only ever kills a PID it spawned or one named by the PID file whose command line still contains the script path (never by port or process name).
- **Diagnostics:** `!musicstatus` (public) reports server state, uptime, restarts, cookie file age, and Node/yt-dlp/plugin versions; staff (`staff_base`) also see URL, PID, watchdog, last error and a sanitised log tail (paths reduced to file names, secret-like values redacted).
- **Cookie-extension listener:** `services/cookie_listener_service.py` (aiohttp), started/stopped by the music cog's `cog_load`/`cog_unload` and exposed as `bot.cookie_listener_service` (`snapshot()` gives `last_update_at`, `accepted_count`, `rejected_count`, `updates_total`, `last_error`, `auth_mode`, `bind_pending`). **On by default** and speaks the [musicbot-cookie-sync](https://github.com/KeiraOMG0/musicbot-cookie-sync) extension's protocol (`GET /needs-refresh` -> `{"needsRefresh": bool}`, `POST /cookies` with Netscape text -> `ok`, default port 8999), so the stock extension needs no edits. Auth: with `MUSIC_COOKIE_LISTENER_SECRET` set, `X-Cookie-Secret` is required (constant-time compare); with it blank, a POST must carry a `chrome-extension://`/`moz-extension://` `Origin` (optionally limited by `MUSIC_COOKIE_LISTENER_EXTENSION_IDS`; the first id seen is logged). `GET /needs-refresh` and preflight are open without Origin or secret, because Chrome sends no Origin on the extension's poll and the reply leaks only `{"needsRefresh": bool}`; a web-page Origin is still refused. Rejection logs are rate-limited, and `!musicstatus` shows `Extension last seen: <age> ago`. Also: loopback bind, size cap, rate limit, youtube/google/googlevideo domain filter, atomic write with a `.bak`, no value logging. **Port conflicts:** only one program can hold 8999. If the port is busy the cog logs one warning, stays loaded and retries the bind in the background (backoff up to 60 s). Either free the port, or give this bot a different `MUSIC_COOKIE_LISTENER_PORT` (the extension talks to one server at a time; a second extension copy pointed at the other port feeds the other bot). See `docs/COOKIE_EXTENSION.md`. When yt-dlp fails with a cookie/age error and the listener is running, the cog asks the extension for fresh cookies and retries once.
- **`!debugurl`:** resolves through the same `_extract_info` (timeout wrapper) as playback and reports extractor, id, title, format/codec/bitrate/protocol, PO-token use, player clients, stream-URL expiry, a HEAD probe (YouTube/Google hosts only) and a classified error (cookies expired / pot down / age-gated / unavailable / timeout). Stream URLs are shown masked (`sig`, `lsig`, `signature`, `pot`, `key`, `token`, `auth`, `ip`, `ei`, `cookie`, anything matching secret/token/sign, plus `MUSIC_DEBUGURL_MASK_PARAMS`); `--full` DMs the unmasked URL and only to the bot owner. Inputs matching `MUSIC_DEBUGURL_BLOCKED`, or non-YouTube URLs, are refused.
- **Degrades gracefully:** if Node or the built script is missing, or `MUSIC_POT_SERVER_ENABLED=false`, the bot logs a warning and continues; only gated videos fail.
- Other tunables: `MUSIC_PLAYER_CLIENTS`, `MUSIC_EXTRACT_TIMEOUT`, `MUSIC_YTDLP_SOCKET_TIMEOUT`, `MUSIC_EJS_REMOTE_COMPONENTS`, `MUSIC_COOKIES_FILE` (see section 6). A bot restart is required for any of them.

---

## 19. Honeypot

A public trap channel that anyone can see and post in. Hacked accounts and spam bots post in every channel they find, so anyone who posts in the honeypot (other than exempt users) is punished automatically, and banned users can appeal through a public page. The design contract is `docs/honeypot_contract.md` (it is authoritative for behaviour and API shapes; this section summarises what is in the tree).

**Status when this was written (2026-09-29): the bot side, the dashboard backend, and the frontend files have all landed** (migration 25 registered in `main.py`, `services/honeypot_service.py`, `database/repositories/honeypot_repo.py`, `cogs/honeypot.py` in `COGS`, `internal_api/routes/honeypot.py`, `server/routes/honeypot.py` + `server/routes/appeals.py`, `server/frontend/src/pages/honeypot/*`, `pages/appeal/AppealPage.tsx`, `api/honeypot.ts`, `api/appeals.ts`). It is built and unit-tested, but has not been exercised live against Discord until it is deployed (bot restarted so migrations 25/26 apply); verify against the code when in doubt.

**Behaviour per catch** (`HoneypotService._catch`): record the message facts and delete the message; if `dm_before`, DM the user first (for `ban` with appeals on, the DM carries `{APPEAL_BASE_URL}/appeal/{token}` and an `invited` appeal row is created); apply the action through `ModerationService` (`ban`, `softban` = ban then unban to purge messages, or `kick`); insert a `honeypot_events` row with `action_result` `ok`/`partial`/`failed` and any errors; post a "Honeypot catch" embed to the log channel (`log_channel_id`, else the moderation-log channel); refresh the live "Caught so far" embed in the trap channel (debounced, at least 10 s between edits). Defaults: action `ban`, `dm_before` on, appeals on, `delete_message_days` 1 (0-7).

**Apps, webhooks and bots (2026-10-20, after an app spammed the trap unnoticed: the trap skipped every bot-authored message).** Checked in this order: (1) this bot's own messages (the warning and "Caught so far" embeds) are ignored; (2) an **app reply** (`interaction_metadata.user` set: a user-installed app, which needs no server authorisation while @everyone has "Use External Apps", or a server bot's slash command) is a catch of the **member who ran the command**, with the usual exemptions, DM, action and event row; the message text includes embed text, the event stores `via_app_id`/`via_app_name` (migration 28), the ban reason becomes `Honeypot: posted in #<channel> via app <name>` (still starts with `Honeypot`, so appeals work), the log embed has a "Posted via app" field and the catch object a `via_app` `{id, name}`; (3) a **webhook** message is deleted and the webhook too (`channel.webhooks()`, needs Manage Webhooks; reported if missing), and (4) a **bot** posting on its own is deleted but never kicked; for (3) and (4) there is no `honeypot_events` row (stats are about people), but one "Honeypot: webhook/bot message removed" log embed and one `honeypot_foreign_message` audit row (actor = the bot; detail `source`, `name`, `channel_id`, `webhook_deleted`, `errors`, `content`) per source per 30 s; later messages in that window are only deleted. `HoneypotService` takes an optional `audit` (the cog passes an `AuditService`).

**Exemptions:** the member behind a message (for app replies, the one who ran the command) is exempt with Administrator, Manage Messages, or Ban Members, and anyone `PermissionService.is_staff` (owner or `STAFF_ROLE_IDS`). An in-flight/cooldown guard (30 s) makes a burst from one user act exactly once; follow-up messages in the cooldown are just deleted. Only the configured guild's config row is consulted.

**Setup:** `/honeypot setup` or the dashboard "Create honeypot" card creates a category (default `Do Not Type` with a warning sign) and a text channel (default `do-not-type` with emoji) where `@everyone` can view and send, posts the warning and "Caught so far" embeds, and stores the config. It needs the bot to hold Manage Channels (missing permission yields a 502-style error); missing Ban/Kick/Manage Messages produce warnings in the response. Returns 409 `already_configured` if the configured channel still exists. If the trap channel is deleted, `on_guild_channel_delete` is handled and the status shows it as deleted; re-run setup.

**Database (migration 25):** `honeypot_config` (one row per guild: enabled, channel_id, category_id, action, dm_before, appeal_enabled, delete_message_days, log_channel_id, warning_message_id, stats_message_id), `honeypot_events` (one row per catch, content truncated to 500 chars, account-age/time-to-catch/avatar/link/invite/@everyone/attachment signals), `honeypot_appeals` (unique `token`, status `invited` -> `pending` -> `approved`/`denied`, `catch_id`, reviewer fields). Snowflake columns are INTEGER; timestamps are ISO-8601 UTC strings.

**Slash commands** (`cogs/honeypot.py`, need `ban_members` grant or staff): `/honeypot setup [category_name] [channel_name] [action]`, `status`, `stats`, `action <ban|softban|kick>`, `enable`, `disable`, `appeals` (lists pending), `scramble [mode: now|on|off] [every_hours 1-168]` (default: scramble now, 60 s budget).

**Name scrambling (2026-10-28, migration 029; built and unit-tested in `tests/unit/test_honeypot_scramble.py`, frontend typechecked and built; live since the 2026-10-28 11:11 bot + dashboard restart (migration 29 applied 15:11 UTC); turned on the same day, every 2 hours).** So spam tools cannot learn to skip the trap by name, `HoneypotService.scramble_names` renames the trap channel and its category to random gibberish, keeping the decoration around the words: leading/trailing non-word characters (`split_decoration`), e.g. `🚫┊do-not-type` -> `🚫┊xq7-vlrm`, `───⋅ Ignore ⋅───` -> `───⋅ Qvex ⋅───`. Channel words: 1-3 dash-joined groups of 2-5 chars from consonants + digits 2-9; category: 1-2 capitalised 4-6 letter words from consonants + e/y (no a/i/o/u, so it practically never spells a real word); never the same words as before; max 100 chars. The user chose gibberish over readable variants knowing that the name then no longer warns members; the warning embed inside the channel is unchanged. The category is the channel's CURRENT parent (not the stored `category_id`) and is renamed only when the trap is the only channel in it (otherwise a warning, as when the channel has no category). Schedule: `scramble_check` every 5 minutes calls `scramble_if_due`, which renames when `scramble_enabled` and `last_scrambled_at` is NULL or at least `scramble_interval_hours` (1-168, default 6) old; timed from the DB value, so restarts don't reset it; independent of the honeypot's `enabled`. A failed scheduled scramble is logged once and retried after 30 min (in memory). Manual scrambles (`/honeypot scramble`, dashboard "Scramble now") are refused with 409 `too_soon` within 10 minutes of the last one (Discord allows 2 renames of a channel per 10 minutes; discord.py would otherwise sleep on the 429); the dashboard path is bounded to 8 s (`SCRAMBLE_MANUAL_TIMEOUT`, inside the internal client's 10 s) and answers 409 `rate_limited` if Discord holds the rename longer. Channel rename refused -> nothing changes (403 Forbidden = Manage Channels; other HTTP errors 502); category rename failed after the channel succeeded -> a warning, still recorded. Each scramble writes `last_scrambled_at`, a `honeypot_scramble` audit row (trigger, old/new names, warnings; actor = the user, or the bot for scheduled ones) and publishes `config_changed` on the `honeypot` live topic. Rename reasons in Discord's audit log: `Honeypot: scheduled name scramble` / `Honeypot: name scramble`. `cogs/logging_channel.py` also records each rename as a `channel_renamed` audit row (DB only, nothing is posted), so a scramble adds up to three `audit_log` rows. Config adds `scramble_enabled`, `scramble_interval_hours`, `last_scrambled_at`, `next_scramble_at` (NULL while off or never scrambled) and the live `channel_name` / `category_name`.

**Internal API** (`internal_api/routes/honeypot.py`, `/guilds/{guild_id}/honeypot/...`, bearer secret): `GET`/`PUT config` (PUT also takes `scramble_enabled`, `scramble_interval_hours`), `POST setup`, `POST scramble` (`{actor_id}` -> `{config, channel:{old,new}, category:{old,new}|null, warnings}`; 409 `too_soon`/`rate_limited`/`channel_missing`, 403 Forbidden), `POST invite` (`{actor_id, channel_id}`: create the rejoin invite), `GET appeals/for-user/{user_id}`, `POST appeals/submit-verified`, `POST appeals/rejoin` (see "Appeal outcomes" below), `GET catches` (`limit`, `before_id`), `GET stats`, `GET appeals` (`status` filter), `GET appeals/lookup/{token}`, `POST appeals/submit`, `POST appeals/{id}/approve`, `POST appeals/{id}/deny`. New snowflake keys (`warning_message_id`, `stats_message_id`, `reviewed_by`, `category_id`) were added to `SNOWFLAKE_KEYS`.

**Dashboard backend:**
- Authenticated `/api/honeypot/...` (`server/routes/honeypot.py`): `GET config|catches|stats|appeals` need `moderate_members`; `PUT config`, `POST setup`, `POST scramble`, `POST invite`, `POST appeals/{id}/approve|deny` need `ban_members` (`scramble_interval_hours` is bounded 1-168 here too, mirroring the bot). Actor/reviewer ID always comes from the session, never the request body.
- Public, unauthenticated `/api/public/appeals/...` (`server/routes/appeals.py`): `GET /api/public/appeals/{token}` (30/minute) and `POST /api/public/appeals` (5/minute), rate limited by slowapi keyed on `CF-Connecting-IP` (falling back to the socket address). **Since 2026-10-29 the POST is retired: appeals require Discord sign-in**, so `POST /api/public/appeals` answers every body (typed id, DM token, garbage) with the identical `403 {"error": "signin_required"}` and calls nothing; the internal `POST .../honeypot/appeals/submit` route and `internal_client.submit_appeal` were removed, and `AppealPage` no longer has a typed-User-ID form (the DM link page shows the outcome read-only, or "appealing as <name>" plus the sign-in card). `HoneypotService.submit_appeal` (token / typed-id logic) is still in the service and its unit tests but is not reachable from any API. Built and tested; the page change is live (frontend build), the POST/internal-route removal goes live with the dashboard/bot restart. Errors are flattened to `{"error": "not_eligible"|"already_submitted"|"rate_limited"|"invalid_request"|"unavailable"|"signin_required"}` (HTTP 404/409/429/422/503/403); the token GET answers `not_eligible` identically for malformed, unknown and used tokens. Historical (before 2026-10-29): a POST with only a typed `user_id` (no DM token) always returned the identical `200 {"ok": true}` whether the id is not banned, banned, already appealed or garbage, so it cannot be used to enumerate who was honeypot-banned; a row is created only when a honeypot ban is on record and no submitted appeal covers it, and it is stored `verified = 0` (migration 26) and flagged "Unverified" in the staff queue. `approve_appeal` only unbans when the current ban reason starts with "Honeypot" (otherwise `409 not_a_honeypot_ban`).
- All internal calls go through `server/services/internal_client.py` (`*_honeypot_*`, `lookup_appeal_token`, `get_appeal_status_for_user`, `submit_verified_appeal`, `rejoin_after_appeal`).
- Public appeal sign-in `/api/public/appeal-auth/...` (`server/routes/appeal_auth.py`): see "Appeal outcomes" below.

**Appeal outcomes, Discord sign-in and one-click rejoin (2026-10-28, migration 030; built, unit-tested in `tests/unit/test_honeypot_appeal_outcomes.py` plus updated honeypot tests, frontend typechecked and built; NOT yet live: needs a bot restart (migration 30, the token hashing at startup, new routes) and a dashboard restart (new routes); the two redirect URLs are registered in the Developer Portal).** Problem it fixes: after a ban the bot shares no server with the person, so the approval DM almost never arrived and a denial sent nothing; the appeal page only said "already submitted". Discord offers no OAuth scope that lets a bot DM someone outside a shared server (checked against the OAuth2 docs; `dm_channels.read` is partner-only and read-only).
- **The page shows the outcome.** `GET /api/public/appeals/{token}` (the DM link) now also returns `submitted_at`, `reviewed_at`, `rejoined` and, only when approved, `rejoin_invite_url`; never the reviewer or the staff note. `AppealPage` shows pending / approved / not approved for an already-submitted appeal instead of "already submitted".
- **Sign in with Discord** (`/api/public/appeal-auth/login?intent=appeal`, scope `identify`): proves which account is appealing, so `GET me` may show the person their own appeal and `POST submit` files a VERIFIED appeal (`verified_via='discord_login'`; it uses their untouched DM row for the latest catch if there is one) with honest errors (`not_eligible`, `already_submitted`), without becoming an oracle for who was banned (only the signed-in id is ever looked up). Since 2026-10-29 sign-in is REQUIRED to appeal (the typed-User-ID fallback was removed, see above). Both buttons link to `/api/public/appeal-auth/login?intent=...`, which builds the Discord authorize URL itself with a random `state` stored in the signed `appeal_oauth_state` cookie; the callback refuses any request whose `state` does not match. Never replace the buttons with a static `discord.com/oauth2/authorize` URL: without `state` the callback rejects it (and accepting it would allow login-CSRF), and `guilds.members.read` is not needed.
- **One-click rejoin** (`intent=rejoin`, scopes `identify guilds.join`): for an approved appeal, the callback hands the person's access token to the bot (`POST .../appeals/rejoin`), which checks the appeal is approved and the user is not banned (`fetch_ban`), then calls `PUT /guilds/{guild}/members/{user}` with the bot token (same application as the OAuth client, verified; the bot needs Create Instant Invite, it currently has Administrator). 201 -> `joined` (`rejoined_at`, welcome DM, `honeypot_appeal_rejoin` audit row, `appeal_rejoined` live event), 204 -> `already_member`. Discord's JSON error code decides the answer whatever the HTTP status: 40007 `still_banned`, 30001 `too_many_guilds`, 50025 `login_expired`; other 403 -> `missing_permissions` (logged as a warning for staff; the person sees "couldn't add you"). The page shows the result from `?rejoin=`. Membership Screening (if enabled) makes them `pending`; they go through `!verify` like any new member (roles are not restored).
- **No Discord token is stored, anywhere.** The access token is used once (`/users/@me`, plus the one add-member call for rejoin) and then revoked (`POST /oauth2/token/revoke`). Fully automatic re-adding on approval would need the token kept until a moderator decides, which was rejected; the person clicks "Rejoin server" instead. Left behind: a signed 30-minute identity cookie `appeal_identity` ({uid, name}; itsdangerous, `SESSION_COOKIE_SECRET`, salt `appeal-identity`; HttpOnly, SameSite=Lax, path `/api/public/appeal-auth`) and a signed 10-minute state cookie. Caveat: discord.py logs request bodies only at DEBUG level, which would include the token sent to Discord; the token is revoked seconds later anyway. Rate limits per client IP: login/callback/logout 10/min, me 30/min, submit 5/min.
- **Hashed link tokens.** `honeypot_appeals.token` stores `sha256:<hex>` of the DM link token (`database/repositories/honeypot_repo.hash_token`; the repository hashes on insert and on lookup, so callers still pass the raw token). Plain SHA-256 is enough because the token is 192 random bits (nothing to brute-force, unlike a password). A copy of `bot.db` therefore holds no usable appeal link. Pre-030 rows are hashed at cog load (`hash_legacy_tokens`, idempotent); checked on a copy of the live DB (1 row converted).
- **Outcome DMs.** Approve and deny both try a DM (approve: the rejoin link + "open your appeal page to rejoin with one click"; deny: "not approved, the ban stays") and record `outcome_dm_sent` (shown to staff as "Outcome DM delivered / not delivered"; `deny` now returns `{ok, dm_sent}`).
- **Rejoin link.** `honeypot_config.rejoin_invite_url`: staff paste one (the bot checks it is a discord.gg / discord.com/invite link to THIS server that never expires and stores `https://discord.gg/<code>`) or `POST /api/honeypot/invite {channel_id}` lets the bot create a permanent unlimited invite (needs Create Invite; `honeypot_rejoin_invite` audit row). Shown on approved appeals and in the approval DM. `invite_in_removal_dm` also adds it to kick/softban DMs (never ban DMs; off by default; the UI warns that a still-hacked account gets it too). Dashboard: "Rejoin link" card on the Honeypot Settings tab (`RejoinCard.tsx`; channel picker from `/api/channels` when the viewer has `manage_reaction_roles`, else a channel-ID box). The dashboard's `PUT config` now passes the bot's 400 text through as `message` so validation reasons reach the UI.
- **Developer Portal (2026-10-28, done by a browser agent):** redirects added `https://dash.idlesys.xyz/api/public/appeal-auth/callback` and `http://localhost:8080/api/public/appeal-auth/callback` (the two `/api/auth/callback` ones unchanged). Read-only findings: Public Bot ON, Requires OAuth2 Code Grant OFF, Public Client OFF, User + Guild Install enabled with Install Link "None". None of these matter for an `identify`/`guilds.join` sign-in (Discord docs: `integration_type` only matters with `applications.commands`; Public Bot / code grant only affect adding the bot to servers).
- **Live status:** the bot and dashboard were restarted onto this code on 2026-10-28 (migration 30 applied; `/api/public/appeal-auth/me` answers). The owner's first test used the typed-ID form with an account that has no honeypot ban, so (by design) nothing was created; that led to the Debug-page test cases below.
- **Manual test plan** (not run yet): easiest with a Debug-page test case (section 25 "Appeal flow test") for an alt; otherwise, with an alt account in a separate browser profile, sign in with no ban (`not eligible`), get caught by the trap (honeypot ban, not a manual one), appeal signed in, try Rejoin while pending (`not_approved`), approve in the dashboard, Rejoin (consent screen lists "Join servers for you"; `joined`; the app is not left under Authorized Apps), Rejoin again (`already_member`), optionally re-ban by hand and Rejoin (`still_banned`).

**Frontend:** the management page is `HoneypotPage` at **`/dashboard/honeypot`** (nav entry "Honeypot" and command-palette entry, shown with `moderate_members`; the contract wrote `/honeypot`, the implementation nests it under `/dashboard`). It has the setup card, settings panel, a "Name scrambling" card on the Settings tab (`ScrambleCard.tsx`: current names, last/next scramble, an on/off switch and an interval select that save immediately, and "Scramble now"; disabled while the bot is offline or the channel is missing), live catch feed of embed-style cards, stats dialog (Recharts), and the appeals queue. The public appeal form is `AppealPage` at `/appeal` and `/appeal/:token`, outside `RequireAuth` and `AppShell`, with a "How do I find my User ID?" helper. `recharts` was added to `package.json` for the stats charts.

**Operational notes:** appeal links point at `APPEAL_BASE_URL` (default `https://dash.idlesys.xyz`), so the public dashboard and tunnel must be up for banned users to reach the form. The honeypot works while the dashboard is down (it lives entirely in the bot process). Approving an appeal unbans the user through `ModerationService` and DMs them (best effort); a user who is already unbanned is treated as success with `unbanned: false`.

---

## 20. Testing

- **Run:** from the repo root, `venv\Scripts\python.exe -m pytest` (`pytest.ini` sets `testpaths = tests` and `asyncio_mode = auto`; the longer `-m pytest tests -o asyncio_mode=auto` form used elsewhere in these docs is equivalent). Tests never touch the live databases: `tests/conftest.py` turns the dashboard's housekeeping job off, and every DB test uses a temporary file.
- **Coverage:** `tests/integration/test_guild_repo.py` (3 tests) and `tests/unit/` (honeypot service/repo/appeals/stats, the honeypot internal API, the dashboard honeypot + public appeal routes, and the PO-token service; the `roles` live topic has `test_role_live_service.py` (`RoleEventCoalescer` debounce/cap/merge, never raises, skipped with no subscribers); the Roles page has `test_role_admin_logic.py` (pure helpers), `test_role_admin_api.py` (internal routes against fake discord objects, including the acting-user hierarchy and the duplicate Administrator confirm) and `test_role_admin_dashboard_routes.py` (staff gate, session actor, error mapping); the Fortnite shop has `test_fortnite_shop.py` (parser/grouping/embed splitting/wishlist/timing against the recorded fixture `tests/unit/fixtures/fortnite_shop_sample.json`, repo, restart-safe dedupe, outage handling, no re-post after a partial post, claim released when nothing was sent, wishlist split without cut-off mentions, "next post" while today is pending); the Debug page has `test_debug_actions.py` (SafeChannel mention/view safety, actions do not write state) and `test_debug_routes.py` (404 for non-owners with a body identical to an unknown `/api/*` path, also for invalid bodies and malformed JSON, Lab routes included, owner id from the session, `is_owner`, Fortnite route gate, Lab error pass-through, latency probes only fixed paths) and `test_debug_lab.py` (Lab: command-requirement scanner incl. helpers, constants, owner checks and source order, permission simulator for a granted member / owner / staff role set / unknown role, anti-spam tester flood/cooldown/duplicate/mentions/validation and overrides, previews captured not sent, "Send copy" through SafeChannel, Settings `applied_at`); the supervisor control API has `test_supervisor_control.py` (in-process control server on an ephemeral port with fake services: auth/404/405/413/415, status shape, queue-then-apply on the main loop, requested-restart semantics vs crash backoff, cooldown 429, `all`, in-progress and port-taken 409s, delay clamping, backoff/not-started handling, scrubbed `/logs`, single-instance lock, `main()` exiting before starting anything when the lock is held, `kill_tree` on a real `python -c "sleep"` child, import without side effects, appeal-token/OAuth-code/IP scrubbing, control characters in `reason`/`requested_by`, a request hitting an already-exited process staying a crash, a kill that does nothing being retried then dropped, the waiting-dashboard wording, `child_env()` re-reading `.env`, the per-connection deadline against a byte-dribbling client, the connection cap), `test_supervisor_clients.py` (bot aiohttp and dashboard httpx clients against that server, unavailable/cooldown/error mapping, `/restart` marker and status-embed helpers) and `test_supervisor_dashboard_routes.py` (staff gate incl. bot-down fallback only for no answer / 5xx / guild-not-cached and 403 for member-not-found / 401 / 422 / unrecognised 404, `check_is_staff` keeping the internal API's `detail`, session-derived `requested_by`, CSRF, 503/429/409/502 mapping, logs scrubbing); weekly log rotation has `test_supervisor_log_rotation.py` (Monday 04:00 slot maths, baseline without rotating on a first start, catch-up when overdue and no second run after a restart, zero lines lost with a writer thread and with a real child through the pump during the swap, gzip archive readable, 28-day retention deleting only its own archive names, kicks.log/.pid/.lock/db files untouched, rename retry on a simulated PermissionError and give-up leaving the file, bot_startup.log deferred while held open (simulated and a real open handle on Windows) then rotated at the next bot start, gzip failure kept uncompressed then recovered, `POST /rotate-logs` queued by the HTTP thread and applied by the main loop, 409/429, `/status` fields, CLI, dashboard client round trip) and `test_supervisor_rotate_route.py` (owner-only `/api/supervisor/rotate-logs`: hidden 404 for anonymous and staff, owner proxied with the session identity while the bot is down, error mapping, CSRF), dependency updates have `test_supervisor_deps.py` (pip-audit / npm audit planning incl. majors and ignore list, yt-dlp merge, pin rewriting with extras and CRLF, freeze and lockfile diffs, held services neither restarted nor alerted, `POST /update-deps` queued then started by the main loop, 409/429/400, `/restart` of a held service refused, check mode only audits and persists, Python apply stopping/installing/testing/pinning/releasing, rollback when the tests fail, frontend rebuild + `dist/` swap without a restart, build failure restoring the package files, refusal when the bot runs outside the supervisor, live progress, CLI; every command through a fake runner) and `test_supervisor_update_route.py` (owner-only `/api/supervisor/update-deps` hidden 404, session identity while the bot is down, 422/409/429/503, CSRF, `dependency_update` hidden from non-owner staff in `/api/supervisor/status`), `test_honeypot_appeal_outcomes.py` (Debug-page test cases: eligible like a real catch but out of the feed/stats/counts, an appeal of a test catch is a test too, approved test case rejoins, list and delete, refusals; tokens stored hashed and legacy rows converted idempotently, own outcome shown with the invite only when approved and never the staff note, outcome DM results stored, sign-in status/eligibility and verified submit consuming the DM row, not_eligible/already_submitted, rejoin requires approval and no ban, the add-member call carries their token and never reaches the audit log, 204 = already member, error-code mapping regardless of HTTP status, invite validation (wrong link, other server, expiring) and creation, invite in kick but never ban DMs; dashboard sign-in: me/submit need a valid signed cookie, forged cookies rejected, staff note filtered, the callback hands the token to the bot once, revokes it and never puts it in a cookie, bad/missing state and a cancelled consent), `test_psutil_guard.py` (every bot psutil entry point is wrapped, concurrent `system_section` calls never overlap, the lock is re-entrant), `test_honeypot_scramble.py` (decoration split on the live names, gibberish shape and change, `scramble_due`, channel + category rename with audit row and `last_scrambled_at`, shared category / no category left alone with a warning, manual 10-minute cooldown, channel Forbidden changes nothing, category failure is a warning, rate-limit timeout -> `rate_limited`, missing channel, the scheduler's due/not-due/retry window, config validation and `next_scramble_at`, the internal route incl. 409 `too_soon`; dashboard route gating + error mapping + interval bounds are in `test_dashboard_honeypot_routes.py`) and `test_supervisor_cli.py` (the `sv.bat`-facing CLI: summary formatting and service states, aliases, empty reason -> null, `pid`/`tail`, unreachable/unknown/help, `health` probes, `follow` tail + new lines + held-back half line + rotation)); dashboard auth hardening has `test_dashboard_auth_hardening.py` (live-hub cached-grant fallback only while the bot is unreachable and time-capped, member-gone drops all topics and closes the socket 4403, other bot answers fail closed, `stats` needs `staff_base`, hidden routes identical to an unknown `/api` path for every method/body/trailing slash, API docs off, cookie `Secure` auto/true/false, `/api/me` `is_staff` and member-gone 403, `require_permission`/`require_staff` member-gone, `/api/resolve` dedupe/cap and the 5-at-a-time `fetch_member` limit, tokens not stored, the Alembic token migration up and down on a temp DB, log out everywhere). The message log has `test_message_log.py` (row kinds incl. app invoker and embed text, buffered until flush, duplicate insert ignored, edit only on a real text change, single/bulk delete marks and ordering with inserts, user query matching author or app invoker, channel/window filters, limit keeps the newest, prune with 7- and 1-day retention, reopen and flush on close, export text tags; the cog skips itself/DMs/system messages/ticket channels with the ticket lookup cached, marks raw deletes, `!msglog` staff-only, DM-only export, closed DMs, status/usage, window parsing). The honeypot app/webhook paths are in `test_honeypot_service.py` (app reply catches the invoker with `via_app` stored and the reason/log updated, staff invoker left alone, own messages ignored, webhook message + webhook deleted and logged once per burst, Manage Webhooks missing reported, bot message deleted but not kicked, `message_text`/`app_source`). Anti-spam has `test_anti_spam.py` (user-installed app flood times out the invoker, server-bot replies and plain webhooks ignored; detector: flood/duplicate-across-channels/mention rules and their thresholds, commands and image-only messages never duplicates, 0 = rule off, the cooldown is delete-only and not extended, history cleared after a hit, idle sweep; settings: the off-or-at-least floors and their dashboard mirror, all `antispam_*` live; cog against fake discord objects: delete + timeout + `spam_detected` audit row + log embed on a hit, cooldown deletes without a second action, staff/guild owner/admin never observed, granted-permission exemption cached, disabled does nothing, timeout off/Forbidden/already timed out, bulk-delete fallback and Forbidden, duplicates deleted in every channel, `!antispam on|off` staff-only). Settings has `test_settings_service.py` (strict bool parsing incl. stored "false", per-type/range validation, unknown/read-only keys, Discord-backed ID checks against fake guild objects, template existence, audit rows, reset to default, live-cache invalidation, startup apply onto `config` that never touches `owner_id`, the import-order guard for `main.py`, the internal route 422/503) and `test_settings_dashboard_routes.py` (staff gate, local mirror validation, session actor, 422/503/403-to-409/502 mapping, mirror matches the bot schema). Moderation has `test_moderation_hierarchy.py` (service rank checks incl. owner/guild-owner/bot-limit/ban-by-ID, bot-initiated paths unchecked, timeout/reason/delete-days bounds, `remove_warning` ownership and actor audit rows, purge count/filter helpers and the `before=` command-message exclusion, purge amount/window parsing, the confirm prompt above 100 messages and for time windows (cancel deletes nothing), the newest-first window purge and the by-ID filter for a user who left, `!delwarn`/`!warn`/`!timeout`/`!ban` handlers), `test_moderation_routes.py` (internal routes: 403 `hierarchy_blocked`/`actor_not_member`, owner outside the guild, 404/409/400/503 Discord mapping, 422 bounds, warning removal actor; dashboard: session actor, pass-through, 502 only when the bot can't answer, 422 bounds), `test_kicks_log_counter.py` (temp-file only: format preserved incl. CRLF, atomic replace leaves the original on failure, no temp files left, concurrent reservations lose no counts, release, manual vs auto reason, auto-kick loop survives a per-member exception) and `test_logging_moderation_actor.py` (target + recency matching, timeout-change predicate, retry, unknown actor recording). `test_audit_log.py` covers the audit repository filters/keyset pagination, row serialisation, the live row, retention, permission grant/revoke audit rows and the internal + dashboard audit routes. Tickets have `test_ticket_lifecycle.py` (open race: two quick clicks create one channel, two members get distinct numbers, failed insert deletes the channel; archive always deletes the channel and runs once under a race, close locks the opener out / posts the notice without buttons / deletes after the delay, a second close is refused, restart recovery archives only overdue closed tickets, the stale sweep closes only idle tickets (and archives one whose channel is gone), the weekly sweep waits 7 days between runs via its audit row, staff-only Close button, delete removes a leftover channel; purge-days guard in the service and the loop). Birthdays have `test_birthday_dates.py` (Dec 31/Jan 1 in UTC+14 and UTC-10, role removal the local day after, Feb 29 on Feb 28 in non-leap years incl. 2100/2000, the fetch window) and `test_birthday_dashboard.py` (date/timezone validation incl. Feb 29, nearest window, import parser skipping impossible dates, export formats incl. a departed member, valid ICS dates/escaping/octet folding and the txt round trip, the `%e` staff-only rule, service timezone clear / preview / import / reset with live publishes; internal routes against a fake guild: entry validation and audit, birthday-role self-role policy (staff role, moderator perms, above the bot, @everyone refused), `%e` refused for non-staff and allowed for staff, channel/timezone validation and the hardcoded-mention warning, test announcement / import dry run / export, unknown actor 403; dashboard routes: `manage_birthdays` gate on every route, session actor, id validation, bot refusal pass-through, 502, import file checks). QOTD has `test_qotd_schedule.py` (slot/catch-up maths, attempt tracker, the cog scheduler: catch-up once with the `qotd_approval_sent` marker, no double post after a restart, already-posted skip, Forbidden clears `_pending` and gives up for the day, transient retry, no default approval channel, loop survives errors; generation jobs reuse/failure; the generation deadline falling back). The Music page has `test_music_dashboard.py` (job store done/failed/busy and generic errors, validation helpers, `dashboard_control` pause/resume with pause bookkeeping, announcements with `AllowedMentions.none()` and escaped names, `music` live events, audit calls, refusals, queue move/remove/volume/loop/clear, stop not re-queued by loop, non-YouTube URLs shown without query, search picks only by the searcher, play needs a URL and a voice channel, prefix commands keep their replies, playlist ownership for `!save`/`!delete_playlist` and the dashboard incl. legacy files and staff overwrite keeping the owner; internal routes: snowflakes as strings, 409/400/503 mapping, job polling; dashboard routes: session actor, `control_music` gate, refusal pass-through, 502, staff-only status details and `!debugurl`, a debugurl job not readable through the play/search poll, bad channel id). The dashboard member tools have `test_member_tools.py` (purge parsing/confirm rule and the dashboard mirror for the same inputs, purge jobs one-per-channel / done / Forbidden; verify role swap + audit source, cog re-export; profile for a member (roles order, advisory rank verdict, honeypot), a banned and a kicked non-member; internal routes: purge confirm/422/channel_forbidden/actor_not_member, job polling, audit + mod log, visible-channel list, message-log export staff-only / DM / no_messages / dm_failed / unavailable, verify channel rule; dashboard routes: purge validation + session actor + gate, export `require_staff`, profile sections withheld by permission, search/history accepting kick or ban, verify gate; manual `!kick` failing closed without the moderation service). The security audit has `test_security_audit.py` (anonymous request to every dashboard and internal route, forged and cross-purpose cookies, empty internal secret, CSRF, OAuth state, login token revoked, session / error-report / legacy-ticket retention, music SSRF blocklist incl. DNS answers and the YouTube skip, security headers, access-log scrubbing). Template placeholders have `test_placeholders.py` (plain / argument / fallback syntax, unknown names kept, a failing placeholder empty, lookups by partial name and ID with @everyone refused, member/server/moderation values incl. the non-member target fallback and zero counts, `human_duration`/`ordinal`, every catalogue entry categorised, described and rendering against `sample_context`, renderer clipping to Discord limits, the mod-log `extra` data from kick/ban/unban/timeout/untimeout and its rendering, the verifier passed to the verify message, the internal catalogue route and the dashboard route gate). Error reporting and caching have `test_error_reports.py` (Cache-Control per file kind incl. an uncached 404 for a missing asset, the public report endpoint storing scrubbed text with the user from the session and no IP, bad bodies / size cap / per-IP rate limit / CSRF, unhandled exceptions recorded by route template with a 500, grouping, clear, pruning by age and row cap, the bot error ring grouping/bounds/handler install, the internal scrubber); the owner-only Errors routes are in `test_debug_routes.py`. Nothing tests the migration runner, the rest of the permission system, the ticket routes, the other internal API/dashboard routes, the other cogs' command handlers, or the frontend.
- **Automation page:** `tests/unit/test_automation.py` (shared services on a temp DB, internal API with a fake guild incl. the autorole self-role refusal for the actor, dashboard per-tab gates and session actor; section 30).
- **Public SPA pages:** `tests/unit/test_public_pages_spa.py` builds the dashboard app against a temporary `dist/` and checks that `/terms`, `/hub`, `/status` and `/appeal` get `index.html` from the SPA fallback with no session (hard refresh works; `server/app.py` needed no change), while an unknown `/api/*` path still answers the JSON 404.
- **Privacy page:** `tests/unit/test_privacy_page.py` checks `/privacy` gets `index.html` with no session, `/api/privacy` is still the JSON 404, the route is declared outside the `/dashboard` layout, `privacyContent.tsx` contains no email address (`CONTACT_EXTRA` stays null) and its stated retentions / cookie names match the code (`AUDIT_RETENTION_DAYS`, message-log, ticket-purge defaults, cookie name constants).
- **Frontend checks:** `npm run build` (runs `tsc -b`, so it doubles as the type check) and `npm run lint` (oxlint) in `server/frontend/`. There are no frontend unit tests.
- **Manual smoke test after changes:** start the bot and confirm the log shows migrations applied, `Loaded cog:` for every cog (no `FAILED to load cog`), `Synced N slash command(s)`, and the PO-token server line; start the dashboard, open `http://localhost:8080/status` (all green) and log in through `http://localhost:8080`.


---

## 21. Diagnostics (staff-only ops console)

A full "info dump" page at `/dashboard/diagnostics` (nav link + Ctrl+K entry, gated like Permissions/Settings: hidden without `staff_base` in the UI, enforced by `require_staff` on the API). It is a **separate page** rather than part of the public Status page: Status is unauthenticated and must stay minimal, while this dump shows paths, command lines and internals. Data flow:

- **Bot side:** `GET /guilds/{guild_id}/diagnostics` (`internal_api/routes/diagnostics.py`, `require_internal_secret`) calls `services/diagnostics_service.collect_diagnostics()`. Sections (process, children, system, discord, database, services, cookies, config, logs, activity) are collected independently: a failure yields `{"error": ...}` for that section only. Slow-changing or expensive parts are cached in ONE TTL cache (`heavy()`; see "Caches and the Clear cached data button" below) and stamped `as_of`; the DB heavy queries use a separate read-only connection so they never block `bot.db`. `psutil` is optional at runtime. `cogs/diagnostics_collector.py` keeps in-memory command/interaction/gateway counters and a 10s CPU/RSS/lag/latency sampler (`bot.diagnostics_collector`).
- **Dashboard side:** `GET /api/diagnostics` (`server/routes/diagnostics.py`) merges the bot dump (via `internal_client.get_diagnostics`) with a section about the dashboard process (CPU/RSS, sessions aggregates, per-route request metrics from `RequestMetricsMiddleware`, dashboard.db, cloudflared process, listening ports). If the bot is down it still returns the dashboard section with `bot: {"reachable": false}`; because `require_staff` itself needs the bot, a fallback (`require_staff_even_if_bot_down`, also used by `/api/supervisor/*`) re-applies the bot's staff rule (`OWNER_ID` / `STAFF_ROLE_IDS`) to the roles cached on the session, ONLY when the bot genuinely cannot answer the staff check: no HTTP answer (connection refused/timeout), a 5xx (e.g. 503 bot not attached yet), or 404 "Guild ... not found" while the guild is not cached during startup. Any other answer from the bot, notably 404 "Member ... not found" for someone kicked/banned since login (or 401/422, or an unrecognised 404), is a 403 and never falls back. `require_staff` raises its 502 `from` the `InternalAPIError`, and `internal_client.check_is_staff` keeps the internal API's `detail`, so the fallback can tell these apart. Freshness limit (2026-10-28, from the permission review; built and tested, NOT live until the dashboard restart): role-based cached staff is only honoured when the roles are recent, i.e. the session was created (login) within `CACHED_STAFF_MAX_AGE` (24 h) or the bot confirmed this user as staff through this guard within 24 h in the current dashboard process (`_staff_confirmed_at`); `OWNER_ID` always passes. So a staff member demoted after logging in cannot keep restarting things through the supervisor while the bot is down for the rest of a 30-day session.
- **Stack scoping:** process and socket data is limited to the bot stack, built once per collection (`build_stack`): the bot and its recursive children, the pot server (spawned or adopted external, found by its port; its real PID/uptime/CPU/RSS are looked up from the port owner), the dashboard (found by its port) and its children, and the cloudflared tunnel whose command line contains `idlesys`/`start_tunnel`. The Child processes section is now "Stack processes" (role, origin, PID, command line, CPU, RSS), and the listening-ports table and connection counts include only stack-owned sockets, labelled by role (internal API, dashboard, pot server, cookie listener, tunnel metrics, bot). No machine-wide port/process list is returned.
- **Hard lines (never in the output):** the bot token, `*_SECRET`/`*_TOKEN`/`*_PASSWORD`/API-key values, token/JWT/Bearer/Cookie-header shapes, raw cookie values (only `ab...(len N)`), dashboard session ids and OAuth tokens. Enforced by name-based masking of command lines/log text, an allowlist for config, and a final `scrub_payload` pass on both processes; covered by `tests/unit/test_diagnostics_*.py`.
- **Deploy:** the bot needs a restart (new internal route + collector cog); the dashboard needs a restart (new route + middleware). `psutil` must be installed in the venv (`requirements.txt` / `server/requirements.txt`).
- **One psutil lock (2026-10-28; built and unit-tested in `tests/unit/test_psutil_guard.py`; live since the 2026-10-28 11:11 bot restart, together with `faulthandler`).** Between 2026-10-10 and 2026-10-22 the bot died 12 times with exit code `0xC0000005` (access violation); the Windows Application log ("Application Error", event 1000) named `_psutil_windows.pyd` at offset `0x26d4` every time, after 1 to 17 h of uptime, with nothing unusual in the bot log. psutil was being entered from several threads at once (the dump's `asyncio.to_thread` sections, the collector's sampler on the event loop, the Debug health check, the pot server's command-line check). Every psutil call in the BOT process now holds `services/psutil_guard.PSUTIL_LOCK` (an RLock; `@serialized` on `sync_part` in `process_section`, `_runtime_heavy`, `build_stack`, `children_section`, `system_section`, `_pot_proc_stats`, the collector's `_sample_process`, `internal_api/routes/debug.py` `_process_snapshot`; `pot_server_service._get_cmdline_sync` uses the lock directly). Nothing waits for it on the event loop: the sampler and `pot_server_section` (which used to call psutil, with `cpu_pct`'s 0.2 s priming sleep, on the loop) now run in worker threads. A new psutil call in the bot must go through the lock. The dashboard process has its own psutil use and is not covered (separate process). Unconfirmed that concurrency is the cause: if the crash recurs, `faulthandler` (enabled in `main.py`) writes every thread's Python stack to `logs/supervisor/bot.log` at the crash, which names the call; check the Application log for the faulting module as well.
- **It recurred, and the call is `cpu_stats` (2026-10-31; built and unit-tested, live after the next bot restart).** The bot crashed again on 2026-10-30 20:25 and 10-31 03:48 with the lock in place (same `0xC0000005`, same offset `0x26d4`, 14 such events since 10-10). Both `faulthandler` dumps show the crashing thread inside `psutil.cpu_stats()` (`system_section`, holding the lock) and no other thread in psutil, so concurrency was not the cause. 3.5 million `cpu_stats()` calls in a separate process did not crash, so it only fails inside the bot process, for an unknown reason. `system_section` no longer calls `cpu_stats` (context switches / interrupts / syscalls; the frontend never showed them, so the dump's `system.cpu` simply has no `stats` key). `tests/unit/test_psutil_guard.py` asserts it is not called. The lock stays. If a crash still recurs, read the "Current thread" block of the dump in `logs/supervisor/bot.log`.
- **Services (supervisor.py) card:** the second section of the page (sticky-nav label "Supervisor", `src/pages/diagnostics/SupervisorPanel.tsx`, hooks in `src/api/supervisor.ts`). Per service (bot, dashboard, tunnel): status dot + text (running / down, starting in Ns / waiting for the bot's internal API / not managed because another process holds the port), PID, uptime, restart count, failed health checks, last exit (reason, when), last requested restart (by, source, when, reason), cooldown; "Recent output" shows the last 120 lines of the supervisor's in-memory tail (it keeps 200; refreshed every 5 s, scrubbed, including appeal tokens, OAuth codes and client IPs). Buttons "Restart bot / dashboard / tunnel / everything" open a confirm dialog with an optional reason and the consequences (bot: music/voice dropped and dashboard 502s for ~10-20 s; dashboard/tunnel: this page disconnects briefly, the tunnel cuts remote access for a few seconds). After a 202 the card shows "Restarting... (Ns)" and polls `/api/supervisor/status` (and `/api/status` for the bot) every 2 s, tolerating fetch failures during the gap (each fetch has an 8 s timeout, one check at a time, so checks never pile up or toast twice), until every target runs with a NEW pid (and the bot's internal API is up), then toasts success; timeouts (bot/dashboard 120 s, tunnel 60 s, all 180 s) show an error. The watch lives in a module store (`src/pages/diagnostics/supervisorRestartWatch.ts`), polled by `useSupervisorRestartWatcher()` mounted once in `DiagnosticsPage`, so collapsing the section or the page switching the panel between its bot-down and normal positions does not drop it (leaving the Diagnostics page pauses it; it resumes, or times out, on return). Status polls every 5 s while the section is expanded and the tab visible (react-query, no live topic), every 15 s while the supervisor is unreachable. States: skeleton, "Not running under supervisor.py — restart controls unavailable" empty state with buttons disabled (503), error with retry (a 404 means the dashboard process predates the feature), inline 403 text. The buttons do not use `useBotOffline()` (the supervisor, not the bot, does the work). The section is also rendered when the main diagnostics dump fails to load. The owner (only) also sees a "Rotate logs now" button and a log-rotation status line here (section 26, "Rotate logs from the dashboard").
- **Logs section (2026-10-14; built, unit-tested in `tests/unit/test_diagnostics_logs.py`, frontend typechecked; live since the 2026-10-14 17:43 bot restart (`services/diagnostics_service.py`) and the frontend rebuilt):** `logs_section()` tails, in this order, `logs/supervisor/bot.log` (the bot's console under `supervisor.py`, 150 lines), `logs/supervisor/dashboard.log`, `logs/supervisor/tunnel.log`, `logs/supervisor/supervisor.log`, `logs/pot_server.log` (path from `MUSIC_POT_SERVER_LOG`), `kicks.log` and `bot_startup.log` (60 lines each; each file reads at most its last 256 KB). Each row has `key`, a friendly `label`, the file `name`, `path`, size, modified time, WARNING/ERROR/CRITICAL counts (cloudflared's `WRN`/`ERR`/`FTL` count as WARNING/ERROR/CRITICAL) and the tail; a missing file shows "missing". `kicks.log` only changes when someone is kicked, so an old timestamp there is normal. "Other files" lists anything else in `logs/` and `logs/supervisor/` (`.pid`, `.lock`, rotated files). Services > Pot server keeps its short pot-server tail next to the server's health. Every tailed line goes through `scrub_log_line` (next to `read_tail`): `redact_text` (hard-line secrets) plus the same three access-log patterns as `supervisor.py`'s `scrub_lines` (hand-synced copy; the supervisor is not imported): public appeal tokens in `/api/public/appeals/<token>` and `/appeal/<token>` paths, `code`/`state`/`token`/`access_token`/`refresh_token` query values (OAuth callback), and client IPs in uvicorn access lines become `<ip>`. As in the supervisor, only `127.0.0.1` stays readable (`::1` is masked too). The dump then also passes both `scrub_payload` passes. A test asserts parity with `supervisor.scrub_lines`. **Tail cache:** `read_tail` caches each file's result keyed on (path, size, mtime_ns, lines, bytes) in a module-level dict (one entry per path), so the ~2 s live rebuilds do not re-read or re-scrub unchanged files (entries hold the already-scrubbed text), and a growing file is re-read on the next push; `clear_log_cache()` empties it. **Frontend:** each log panel in `LogsBody` has its own minimize/expand toggle (`aria-expanded`; the collapsed set is stored per viewer in `localStorage["kb-diag-logs-collapsed"]`, try/catch via `src/lib/storage.ts`); a collapsed panel still shows label, file name, size, age and the warn/error badges; an open tail auto-scrolls to the newest line unless the viewer has scrolled up.
- **Page header (2026-10-14):** no actions any more: the "Updated ... (live, pushed every ~2s)" text and the Pause/Resume, Refresh now, Copy JSON and Download buttons were removed (the page always follows live pushes, `useDiagnostics(false)`). Collapse all / Expand all stay in the sticky nav, and the error state keeps "Try again".
- **Caches and the "Clear cached data" button (2026-10-14; built, unit-tested in `tests/unit/test_diagnostics_delta_cache.py`, frontend typechecked; live since the 2026-10-14 17:43 bot + dashboard restart):** every cached part of the bot dump goes through the one `heavy(key, ttl, producer)` cache in `services/diagnostics_service.py` (TTLs are the `TTL_*` constants): stack scan `build_stack` 10 s (feeds children, `system.network`, the pot server lookup), `children` 15 s (CPU% is averaged over that window), Discord static part (intents, cogs, extensions, listener counts, slash commands, bot permissions) 60 s, the whole `database` section (file sizes, migrations, row counts, quick_check) 60 s, `cookies` 60 s, `config` (allowlisted env + package versions) 300 s, internal API route listing 300 s, `process.runtime` (GC census) 45 s, honeypot stats 60 s; `node --version` keeps its own 600 s cache. Whole cached sections carry `as_of` + `ttl_seconds`; partly cached ones (`discord`, and `database` for compatibility) carry `heavy_as_of` + `heavy_ttl_seconds`; the section header shows either as the "as of" badge. Never cached: process CPU/RSS/threads/loop lag/tasks, host CPU/memory/disk/net counters, gateway latency/ready/sequence/cache counts, guild counts, voice, services (pot server state, loops, honeypot config), activity; logs use the separate mtime-keyed tail cache. `clear_caches()` empties the TTL cache and the node cache and calls `clear_log_cache()`. The button sits at the bottom of the System section (`src/pages/diagnostics/ClearCacheButton.tsx`, hook `useClearDiagnosticsCache` in `src/api/diagnostics.ts`; disabled by `useBotOffline()`, success/error toast): `POST /api/diagnostics/cache/clear` (staff, same guard as `GET`, CSRF Origin check) -> internal `POST /guilds/{id}/diagnostics/cache/clear` (`requested_by` = "username (id)", logged bot-side; no `audit_log` row, like the supervisor restart controls, since it changes no bot state) -> then `hub.reset_diagnostics_state()` forgets every browser connection's delta base and wakes the push loop, so every open Diagnostics page receives a full snapshot at once and the recomputed bot sections with the bot producer's next push (~2 s; they arrive as a delta). With the bot down the route answers `502 internal_api_unavailable` ("could not be cleared") and resets nothing.


---

## 22. Live updates (WebSocket)

Replaces interval polling with pushed events, with automatic fallback to polling when the socket is down. Built and tested (pytest with a fake bot, plus an ephemeral-port end-to-end smoke run); it is live in production only after both processes are restarted.

### Topology

```
 browser tab --wss:// /api/ws--> dashboard backend (server/) --ws://127.0.0.1:8766/ws--> bot (internal_api/)
 (src/live, ONE socket)          server/live/endpoint.py        server/live/upstream.py    internal_api/routes/ws.py
                                 server/live/hub.py (authz,     (CLIENT, reconnects        services/live_events.py (fan-out hub)
                                  filtering, refcounts)          forever)                   <- publish() calls in the honeypot,
                                                                                               ticket, moderation, warning, audit services
```

The bot never dials out and exposes nothing new (loopback-only `/ws`, same shared secret). Cloudflare Tunnel proxies websockets; the SPA is same-origin so the browser picks `ws://` or `wss://` from `location.protocol`, with no path or proxy assumptions. No new ports (8766 internal, 8080 dashboard).

### Envelope

`{"v":1,"type":"event"|"snapshot"|"delta"|"pong"|"error"|"hello"|"ack","topic":"...","ts":<ms>,"seq":<int per topic>,"data":{...}}` (diagnostics messages add `rev`/`base_rev`, see "Diagnostics deltas" below). Client sends `{"type":"subscribe"|"unsubscribe","topics":[...]}`, `{"type":"resync","topics":[...]}` (resend a full snapshot of topics this socket is subscribed to and authorized for; answered by an `ack` with `op:"resync"`, `resynced:[...]`) and `{"type":"ping"}`. `hello` (topics you may use, current bot state) arrives first; `ack` answers every subscribe/unsubscribe (`accepted`/`denied`/`unknown`); `error` carries `data.code` (`forbidden`, `bad_message`, `bad_topics`, `unknown_type`, `too_many_subscriptions`). Snowflakes are strings. Events are deltas; snapshots and refetches resync.

### Topics and authorization

| Topic | Who (checked per connection, server-side) | Content |
|---|---|---|
| `bot` | any authenticated guild member | `{online, ready, latency_ms}`; `online` flips when the dashboard's upstream connects or disconnects |
| `stats` | `staff_base` (same gate as `GET /api/dashboard/stats`) | member/presence counts, ~5s, only while subscribed |
| `diagnostics` | staff (`check_is_staff`; cached-role fallback when the bot is down, like `GET /api/diagnostics`) | merged bot+dashboard dump (same shape as the HTTP route): one full `snapshot` per subscribe/resync/cache clear, then a `delta` with only the changed paths ~every 2s (see "Diagnostics deltas"), ONLY while >= 1 subscriber; cached sections keep their `as_of` |
| `honeypot` | `moderate_members` | `catch_created`, `appeal_submitted/approved/denied`, `config_changed`, `stats` |
| `tickets` | `manage_tickets` | `opened/claimed/closed/archived/deleted` |
| `moderation` | `moderate_members` | `kick/ban/unban/timeout/untimeout/warn/warning_removed` |
| `audit` | `view_audit` | `{kind: "audit", row}` for every row written through `AuditService.log`; `row` has the same shape as a `GET /api/audit` entry (including its `id`). Consumed by the Audit log page |
| `birthdays` | `manage_birthdays` (staff bypass) | `{kind: "birthdays", change: "entry"\|"settings"\|"bulk"\|"celebrated"\|"timers", guild_id, user_id?}` from every `BirthdayService` mutation (slash commands, dashboard, the scheduler marking one celebrated). The Birthdays page just re-fetches its overview (`useBirthdaysLive`, `invalidateSoon`). built and tested 2026-10-28, NOT live until the bot and dashboard restart (the frontend page is in the built `dist/` and shows an error state until then) |
| `automation` | ANY of `manage_events`, `manage_schedules`, `manage_autorole`, `manage_templates` (a tuple in `TOPIC_PERMISSIONS`; `server/live/hub.py` `required_permissions` treats a tuple as any-of; staff bypass) | `{section: "events" / "welcome" / "boost" / "schedules" / "autoroles" / "managed", action, ...ids}` from `EventService`, `WelcomeService`/`BoostService`, `SchedulerService` (also after each scheduled run), `AutoroleService`, `ManagedMessageService` mutations, so slash-command changes show up too. Only says which tab changed (no data). The Automation page invalidates that tab's query (`useAutomationLive`, `invalidateSoon`) and polls every 60 s only while the socket is down. built and tested 2026-10-28, NOT live until the bot and dashboard restart (the page is in the built `dist/` and shows error states until then) |
| `roles` | `manage_reaction_roles` (staff bypass) | `{action: "created"\|"updated"\|"deleted", role_id, role_ids, count}` from `cogs/live_roles.py` on guild role create/update (incl. reorders)/delete. Coalesced by `services/role_live_service.py` (0.5s trailing debounce, 2s cap, one message per action kind per flush; created+deleted collapses to deleted), so a 30-role reorder is one message. Nothing is buffered while nobody subscribes. Consumers just invalidate role queries (`api/rolesLive.ts` `useRolesLive` / `invalidateRoleQueries`; the Roles page uses the thin wrapper `useRoleAdminLive` in `api/roleAdmin.ts`, same path) |
| `music` | `control_music` (staff bypass) | `{event}` only (`track_start`, `track_end`, `queue`, `paused`, `resumed`, `skipped`, `volume`, `loop`, `autoplay`, `joined`, `moved`, `left`, `playlists`) from `cogs/music.py` `_notify` on every player change (Discord commands and dashboard alike; never progress ticks). The Music page invalidates its state query (or the playlist list for `playlists`) via `invalidateSoon`; it polls every 5 s only while the socket is down |

Permissions are resolved on connect via internal `check-bulk` + `is-staff`, cached 30s, and re-checked every `DASHBOARD_WS_REVALIDATE_SECONDS` (60): a deleted or expired session closes the socket (4401), a demoted user loses the topic (an `error` with `permission_revoked`). Bot errors (`resolve_access`): only when the bot cannot answer (`guards.bot_unreachable`: no answer, 5xx, guild not cached) are the last known grants kept, and only for `PERMISSION_FALLBACK_MAX_AGE` (10 min) after they were last confirmed; after that, or with nothing cached, the member-level topics plus `diagnostics` for cached-role staff. A bot answering 404 "Member ... not found" (kicked/banned/left) drops every topic and the cached grants and the socket is closed 4403 (at the handshake or the next re-validation); any other bot answer (401/422/unrecognised 404) falls back to member-level topics only. Logout closes that session's sockets immediately. Every outgoing message is filtered by `topic in conn.allowed and in conn.topics`; the client's subscribe request is never trusted.

### Diagnostics deltas (bandwidth)

Built 2026-10-14, unit-tested (`tests/unit/test_diagnostics_delta_cache.py`) and typechecked; live since the 2026-10-14 17:43 dashboard restart. The merged dump is a few hundred KB, so the hub no longer sends it whole every ~2s:

- **Per connection** (`server/live/hub.py`, `DashboardHub.push_diagnostics`), the hub remembers the last payload that socket received (`Connection.diag_last`) and a revision counter (`diag_rev`). With no base (first subscribe, re-subscribe, a `resync` request, or `reset_diagnostics_state()` after the Clear cached data button) it sends `{"type":"snapshot","topic":"diagnostics","rev":N,"data":<full dump>}`. Otherwise it sends `{"type":"delta","topic":"diagnostics","rev":N+1,"base_rev":N,"data":{"set":[[path,value],...],"unset":[path,...]}}`, where `path` is a list of keys (e.g. `["bot","process","cpu_percent"]`, `["generated_at"]`). `diagnostics_delta()` recurses into dicts up to `DIAG_DELTA_DEPTH` = 3 keys and compares everything deeper (lists, `system.cpu`, ...) whole, type-strictly; `unset` lists keys that disappeared (e.g. the bot sections when the bot goes unreachable). Nothing is sent when nothing changed. The payload is scrubbed (`build_diagnostics` -> `_scrub`) before diffing, so deltas carry only scrubbed values. Sections that change every tick (process CPU, loop lag, system counters, `generated_at`) still go out every push; the static and TTL-cached ones (config, database, cookies, Discord registry, routes, ...) only when they change.
- **Browser** (`src/api/diagnostics.ts`): `snapshot` replaces the query data; `delta` is merged immutably with `applyDelta` (copies only the objects on each changed path), so `dataUpdatedAt` advances on every push and the sparkline history keeps sampling. A module-level record keeps the revision and `dataUpdatedAt` of the data it wrote; a delta whose `base_rev` does not match, or arriving when the cache was written by something else (the HTTP fetch at page load or after a reconnect, the offline poll) or is empty, is dropped and a `resync` is sent (throttled to one per 3s) via `LiveConnection.resync` / `useLive().resync`, so a partial object is never rendered. A delta with the already-applied `rev` is ignored (idempotent across several mounted hooks). A queue overflow drop also shows up as a `base_rev` gap and self-heals the same way. A page load or reconnect therefore usually costs the HTTP dump plus one or two full snapshots, then only deltas.
- **Not done:** the bot -> dashboard upstream hop (internal `/ws`, loopback) still carries the full bot dump every ~2s; deltas there would need the hub to rebuild the dump from patches and were left out as low value on localhost.

### Reconnect and fallback

- Upstream (dashboard to bot): backoff 0.5s doubling to a 15s cap with 50-100% jitter, retried forever; library ping plus an app-level ping (gives the round-trip time); each connect replays the topics browsers currently watch (ref-counted, so the bot runs the `stats`/`diagnostics` producers only while someone watches). Bot connected/disconnected is broadcast on `bot`; browser sockets stay open through a bot outage.
- Browser: one `LiveConnection` per tab (`src/live/connection.ts`), same backoff, retries forever, pings every 25s, drops a silent socket after 65s, reconnects immediately on `online` and tab-visible. On every hello and on a bot offline-to-online transition it invalidates the live query roots (`me`, `dashboard-stats`, `status`, `diagnostics`, `honeypot`, `tickets`, `moderation`, `reaction-role-roles`, `permissions`, `role-admin`) plus every role-carrying key matched by `isRoleQueryKey` in `src/api/rolesLive.ts` (`["resolve-single","roles",id]`, role-carrying `["resolve",...]`, `["qotd","roles"]`, `["fortnite","roles"]`), so a role renamed during a socket gap or bot restart is re-fetched everywhere; user/channel resolve caches are left alone. `LiveConnection.tap(listener)` (exposed as `useLive().tap`) observes every parsed frame of any type without subscribing; only the owner-only Debug > Lab live inspector uses it (section 25).
- Sidebar indicator: `Live`, `Bot offline`, `Reconnecting...`, `Offline - polling`. Query hooks use `refetchInterval: connected ? false : <old interval>`; `useStatus` relaxes to 60s while live (bot transitions invalidate it at once). Converted: dashboard stats, status, diagnostics (pause unsubscribes; sparkline history is fed by pushes), honeypot catches/appeals/stats/config (new catch animates in), tickets, moderation recent actions, guild role lists (`invalidateRoleQueries` re-fetches every key matched by `isRoleQueryKey`: Self-roles picker `["reaction-role-roles"]`, Roles page `["role-admin",...]`, Permissions matrix `["permissions","roles"]`, QOTD/Fortnite pickers `["qotd","roles"]` / `["fortnite","roles"]`, and the `<ResolvedRole>` caches `["resolve-single","roles",id]` / role-carrying `["resolve",...]` keys, which otherwise keep a 5 min staleTime). Only the Self-roles, Permissions and Roles pages subscribe to `roles` (each gated on its main query having data, not on `isSuccess`, so a failed background refetch does not drop the subscription); the QOTD and Fortnite pages do not subscribe (their viewers may lack `manage_reaction_roles`), so their pickers refresh only when another open page receives an event, on reconnect resync, or on their normal refetch. Self-roles/Permissions poll every 60s and Roles every 30s only while the socket is down.

### Limits and close codes

Browser socket: 4 KiB messages, 10 msg/s (burst 20), 16 topics, 8 sockets per user, 90s idle timeout. Codes: 4401 unauthenticated or session ended, 4403 bad Origin or not a guild member, 4408 idle, 4429 rate limited or too many sockets, 1009 too big, 1003 binary frame. Internal `/ws`: 8 KiB messages, 20 msg/s, per-connection queue of 256 with drop-oldest.

### Security notes

- **Origin:** `/api/ws` requires an `Origin` in `server/csrf.py`'s allowed set (`allowed_origins()`); missing or foreign gets 4403. SameSite=Lax does not protect websocket handshakes, so this is the CSRF defence. Rejections accept then close so the browser sees a clean code.
- **Internal `/ws`:** loopback peer only, refuses any request carrying an `Origin`; `Authorization: Bearer` (refused before the handshake if wrong) or a first message `{"type":"auth","secret":...}` within 5s; constant-time compare.
- **Redaction:** the bot scrubs every `diagnostics` payload with `diagnostics_service.scrub_payload` (message dropped if scrubbing fails); the dashboard merges and scrubs again with the HTTP route's `_scrub`. Tests plant secrets and assert they never reach the wire.
- **Failure isolation:** `publish()` never raises or blocks; the service hooks cannot break a kick, a ticket close or the honeypot.
- **Requirements:** `websockets==16.0` (both requirements files); uvicorn uses it to serve the websocket routes.

### Stay logged in

- Sessions live in `server/dashboard.db`; the cookie signature key is `SESSION_COOKIE_SECRET` from `.env` (never generated per process), so restarting the site, bot or tunnel does not invalidate sessions.
- Sliding expiry is now 30 days (`DASHBOARD_SESSION_MAX_AGE_SECONDS`). `touch_session` writes at most every 5 minutes (`DASHBOARD_SESSION_TOUCH_INTERVAL_SECONDS`). An open live tab counts as activity (throttled). The browser cookie's own `max_age` is 400 days (it is not re-issued, so the server-side row decides validity); httpOnly, SameSite=Lax and the env-driven `Secure` flag are unchanged.
- Frontend: `/api/me` retries forever with capped backoff and refetches on `online`, tab-visible and websocket reconnect. Only a real `401 {"error":"unauthenticated"}` (JSON) logs out. Network errors, 5xx (502/503/504, Cloudflare 52x/530), non-JSON or HTML error bodies and `internal_api_unavailable` are transient (`ApiError.transient`; logic in `src/api/classify.ts`). The last-known user stays signed in during an outage and the "reconnecting" banner shows.
- Manual check (no frontend test runner is installed): node can import `src/api/classify.ts` directly; `classifyAuthFailure(502, {error:"internal_api_unavailable"}, true)` and `classifyAuthFailure(401, null, true)` must be `"transient"`, `classifyAuthFailure(401, {error:"unauthenticated"}, false)` must be `"unauthenticated"`. The backend wire contract is covered by `tests/unit/test_live_session.py`.

### Tests

`tests/unit/test_live_*.py`: bot hub, internal `/ws` auth and limits, upstream reconnect (fake and real ephemeral server), dashboard hub authz matrix, browser endpoint (origin, cookie, mid-stream deletion/expiry/demotion, limits, diagnostics stream), publish hooks, session throttle. `tests/unit/test_diagnostics_delta_cache.py`: diagnostics deltas (first push full, unchanged paths omitted, depth limit, removed keys, per-connection bases, resync/reset/re-subscribe force a full snapshot, an unbroken `rev` chain from the real loop that rebuilds the last dump), the bot TTL caches and `clear_caches()`, and both cache-clear routes (secret, staff gate, bot down, foreign Origin). `tests/unit/test_role_live_service.py`: `roles` topic coalescing (merge rules, burst debounce, max-delay cap, no-subscriber short-circuit, failure isolation), end-to-end publish through the bot hub with string ids, and the hub permission entry.

The `roles` topic (built and unit-tested) is loaded since the 2026-10-14 restart of the bot and dashboard (`Loaded cog: cogs.live_roles` in the bot log); live behaviour has not been exercised yet. A bot older than that code rejects the `roles` subscription as unknown; other topics are unaffected, and the Self-roles "Refresh roles" button still works.

### Deploy

Restart the bot (new `/ws` route, hub, publish hooks) AND the dashboard (endpoint, lifespan, session changes); the frontend needs `npm run build` (done) and a hard browser refresh. Start the bot first; afterwards the dashboard reconnects by itself whenever the bot restarts.

---

## 23. Roles page (Discord role management)

Staff-only page at `/dashboard/roles` (nav "Roles" in the System group, shown with `staff_base`; every API route is `require_staff`, same reasoning as Permissions: the page can create roles carrying Discord permissions and internal grants, so a grantable gate would allow self-escalation; no new permission string was added). **Status: built, unit-tested and frontend-built; loaded since the 2026-10-14 bot and dashboard restart, but NOT yet exercised against Discord.**

**Files.** Bot: `services/role_admin_service.py` (pure helpers + `RoleAdminService`), `internal_api/routes/role_admin.py`. Dashboard: `server/routes/role_admin.py`, the `_role_admin_request` block at the end of `server/services/internal_client.py`. Frontend: `src/api/roleAdmin.ts`, `src/pages/roles/` (`RolesPage`, `RoleEditorDialog`, `RoleMembersDialog`, `roleUtils.ts`), types in the "Roles page" block of `src/types/api.ts`. `SNOWFLAKE_KEYS` gained `top_role_id` and `source_role_id`.

**What it does.**
- List: every role highest first (name, colour, position, member count, managed/bot/booster badges, hoist, mentionable, Administrator flag, copyable ID, internal grants), search by name/ID, filters (manageable, read-only, has bot permissions, hoisted, default staff), role count against Discord's 250 limit. A divider sits under the bot's top role: "roles below this line are manageable by the bot".
- Manageable = bot holds Manage Roles, not @everyone, not managed (bot/integration/booster), and position strictly below the bot's top role, AND (for anyone but the owner) strictly below the acting user's own top role. Everything else is read-only with a lock tooltip giving the reason.
- **Acting-user hierarchy (server-enforced in `RoleAdminService`, mirrored in the UI).** Staff gating alone is not enough: without it any `STAFF_ROLE_IDS` member could act with the bot's full reach. The owner (`config.OWNER_ID` or the guild owner) bypasses this; every other staff member: may edit / move / delete / add or remove members of a role only when it is strictly below their highest Discord role (`403 actor_hierarchy`); has moves, "top" and "directly above X" placements clamped to directly below their top role (`compute_reorder(..., ceiling_role_id=...)`); may add or remove only Discord permissions they hold themselves (Administrator holders = all) and never Administrator itself, which is owner-only (`403 actor_permission_not_held`); may not give a role to themselves (`403 self_assign_forbidden`) nor give anyone a role carrying Discord permissions they do not hold; must be a guild member (`403 actor_not_member`). Internal bot grants on a role are NOT hierarchy-checked (same as the Permissions page; staff bypass internal checks anyway). The overview takes `?actor_id=` (the dashboard always sends the session user), folds the actor rule into each role's `manageable` / `unmanageable_reason`, and returns an `actor` block (`is_owner`, `top_role_id`, `top_position`, grantable `permissions`) that the editor uses to lock checkboxes and cap positions; non-owners see an info banner explaining the rule.
- Member counts in the overview come from one pass over `guild.members` (`role_member_counts`), not `len(role.members)` per role (which rescans the member cache for every role).
- Create / edit: name, colour (picker, hex, Discord presets), hoist, mentionable, role icon (unicode emoji or PNG/JPEG/WebP up to 256 KB, only when the guild has the `ROLE_ICONS` feature, otherwise a note), position (create: bottom, directly below the bot, or directly above a chosen role; edit: a position select), Discord permissions in General/Membership/Text/Voice/Advanced groups (only flags the bot holds can be added or removed; others are locked, keeping the role's current value; Administrator is offered only to the owner when the bot has it and needs a typed `ADMINISTRATOR` confirmation plus `confirm_administrator` server-side; for non-owners, flags they do not hold are locked too), and the 14 internal bot permissions applied through `PermissionService.grant/revoke` (the Permissions page path). Internal grants stay editable on roles above the bot (they are not Discord changes) but not on @everyone or managed roles.
- Edit dialog concurrency: the form is snapshotted when the dialog opens and Save sends only fields changed since then (permission and internal-grant toggles are applied as a delta on top of the live role), so a live `roles` refresh while the dialog is open is never silently reverted; when the live role differs from the snapshot an info notice offers "Reload latest".
- Reorder: up/down buttons and the edit dialog's position select, clamped to `1..bot_top-1` (and below the acting user's top role for non-owners). Uses `guild.edit_role_positions` with positions recomputed from a fresh `guild.fetch_roles()`; tied/stale positions that would collide with the bot's role return `409 positions_out_of_sync`. No drag-and-drop (no drag library in `package.json`).
- Duplicate (copies colour, hoist, mentionable, icon emoji, the permissions both the bot and the acting user may grant, and internal grants; placed directly below the source when manageable, else at the bottom; dropped permissions are reported; a copy that would carry Administrator needs the same typed `ADMINISTRATOR` confirmation in the UI and `confirm_administrator` on `POST .../duplicate`, else `400 administrator_needs_confirm`), delete with typed-name confirmation (server also checks `confirm_name`; internal grants of the deleted role are revoked), copy role ID, members dialog (paginated 25 per page from `role.members`, filter, add via a member search typeahead, remove; adding someone to a default-staff role asks for confirmation).

**Audit.** Every Discord call carries the reason `Dashboard: <username> (<action>)`; every mutation writes an `audit_log` row through `AuditService` (`role_create`, `role_update`, `role_move`, `role_duplicate`, `role_delete`, `role_member_add`, `role_member_remove`; `target_type` `role`, `detail.via = "dashboard"`), which also reaches the live `audit` topic. Actor id and username always come from the session. Note the existing Permissions page grant/revoke path itself writes no audit row; the role audit rows record internal grant changes in their detail.

**Errors.** Internal API: `{"error", "message"}` details (400 validation / `administrator_needs_confirm`, 403 `permission_not_held` / `discord_forbidden` / `bot_missing_manage_roles` / `actor_hierarchy` / `actor_permission_not_held` / `actor_not_member` / `self_assign_forbidden`, 404 `role_not_found` / `member_not_found`, 409 `role_not_manageable` / `role_limit` / `positions_out_of_sync`, 502 `discord_error`). The dashboard passes 400/404/409 through, turns any internal 403 into `409` (so it is never mistaken for a dashboard `forbidden`), and returns `502 internal_api_unavailable` when the bot is down.

**Live.** The page subscribes to the `roles` topic through `useRoleAdminLive` (a wrapper over the shared `useRolesLive`, enabled once the overview has data; section 22; publisher and hub entry belong to the live-roles work, not this page), so any message runs the shared `invalidateRoleQueries` (its `["role-admin", ...]` queries plus every other role cache); `role-admin` is also a reconnect-resync root; it polls every 30 s only while the socket is down, and also invalidates after its own mutations (plus `["permissions"]`). Discord-bound buttons are disabled through `useBotOffline()`.

**Deploy.** Restart the bot (new internal route + service) and the dashboard (new `/api/roles` router); the frontend is rebuilt (`npm run build`). The bot needs Manage Roles (and should sit above the roles you want to manage) for anything but read-only.


---

## 24. Fortnite item shop

**Status: built, unit-tested, frontend built; loaded since the 2026-10-14 bot and dashboard restart, NOT yet exercised live** (the parser was checked against a real response fetched on 2026-10-13).

**Source.** `GET https://fortnite-api.com/v2/shop?language=en` (fortnite-api.com, an unofficial community API; free; no key needed for this endpoint, optional `FORTNITE_API_KEY` is sent as `Authorization`; no rate limit is documented, and the bot fetches at most once per daily post plus `!shop`, cached 10 minutes). Response: `data.hash`, `data.date` (`YYYY-MM-DDT00:00:00Z`), `data.vbuckIcon`, `data.entries[]` with `regularPrice`/`finalPrice`, `offerId`, `inDate`/`outDate`, `sortPriority`, `layout` (`name` = shop section, `index` = section order; some entries have none and go to "Other"), optional `bundle` {name, info, image}, `banner` {value}, `newDisplayAsset.renderImages[].image`, and `brItems` (cosmetics with `type.displayValue`, `rarity.value`, optional `series` {value, colors}, `images.featured/icon/smallIcon`), `tracks` (Jam Tracks: title, artist, albumArt), `cars`, `instruments`, `legoKits`. A real day had about 380 entries, roughly 110 of them Jam Tracks.

**Files.** `services/fortnite_shop_service.py` (pure parse/group/embed/batch/wishlist/timing functions + `FortniteShopService`), `database/repositories/fortnite_repo.py`, `cogs/fortnite_shop.py` (section 10), migration `027_fortnite_shop.sql`, `internal_api/routes/fortnite.py`, `server/routes/fortnite.py`, the "Fortnite item shop + owner-only Debug page" block at the end of `server/services/internal_client.py`, `src/api/fortnite.ts`, `src/pages/fortnite/FortniteShopPage.tsx`, types at the end of `src/types/api.ts`. `SNOWFLAKE_KEYS` gained `last_message_id`.

**Daily post.** `cogs/fortnite_shop.py`'s 60 s loop calls `run_daily()`: when enabled, a channel is set, it is past 00:00 UTC + `FORTNITE_SHOP_POST_DELAY_MINUTES`, and `last_posted_date` is not today, it force-fetches the shop; if the API still returns yesterday's date it retries 5 minutes later. It posts a header embed plus one embed per section (`**Name** · 1,500 V-Bucks ~~2,100~~ · Bundle · Epic · *banner*`, the rarity/series name on every non-bundle line, section colour from the rarest item or series colour, first item image as thumbnail; sections over ~3900 characters split into "(cont.)" embeds), Jam-Track-only sections collapsed to one line unless "List every Jam Track" is on, batched to at most 10 embeds and 6000 characters per message (a real day was 3 messages). The ping role (if any) is pinged once (@everyone and managed bot/integration roles are refused by `/fortnite pingrole` and `PUT /fortnite/config` with `role_not_pingable`, and hidden from the dashboard picker). **Dedupe:** `last_posted_date`/`last_posted_hash` are written BEFORE the first message is sent, so neither a restart nor a failure partway through ever re-posts batches that already went out. If the very first send fails, the claim is released and the loop retries in 5 minutes (`post_failed`); if a later batch fails, the claim stays, nothing is re-sent, and `last_error` says "partial post ... not retried" (`post_partial`). `last_posted_at`/`last_message_id` are stored after the shop embeds. Wishlist matches (case-insensitive substring of any item name) are then sent in as many messages as needed (each under 1900 characters, no member's mention is ever cut off; a member's own over-long item list is shortened), and only the wishes whose mention was actually sent are marked notified (each wish at most once per shop date); a failed wishlist message is recorded in `last_error`. Fetch uses aiohttp with a 30 s timeout and 3 attempts (2 s, 5 s backoff, worst case about 97 s) for the daily post and `!shop`; manual posts (`/fortnite postnow`, dashboard Post now, Debug page actions and the shop data viewer) use a single attempt (`MANUAL_FETCH_ATTEMPTS`, about 30 s), and the dashboard's slow internal-API timeout (`_SLOW_TIMEOUT` in `internal_client.py`) is 150 s, above the bot's worst case including a wait on the shop lock, so the dashboard does not report a failure for a post that then goes out anyway; failures are logged and stored in `last_error` (shown on the dashboard and `/fortnite status`), and never stop the loop. Enabling it mid-day posts today's shop within a minute (catch-up). "Next post" (`compute_next_post`; `post_pending` in `GET /fortnite/status`) reports "due now" or the loop's retry time (`FortniteShopService.retry_at`, set by the cog after a failed tick) while today's post is still pending, and tomorrow's slot only once today is done. `/fortnite postnow`, the dashboard "Post now" and the Debug page post without touching the dedupe columns.

**Deliberate trade-off (per-item images).** The daily post is a compact list: one embed per section, so each item line has name, price, type and rarity name, but the section has a single colour and only its first item's picture as a thumbnail. Per-item embeds with the full image and each item's rarity colour are in `!shop` (and the Debug page "Fortnite !shop page" action). One embed per item in the channel would be dozens of messages a day (about 380 entries, 10 embeds per message); a "rich" mode was not built.

**`!shop`.** Uses the in-memory copy (refetched when older than 10 minutes or from a previous UTC day; on an outage a stale cached copy is used rather than failing). Composite grid images were not built: Pillow is not installed in the venv, and downloading hundreds of images per day was not worth it; embeds carry the images.

**Dashboard.** `/dashboard/fortnite` (`manage_schedules`): enable switch, channel, ping role, Jam Tracks toggle, last posted, next post time, cached shop, wishlist counts, last error, Post now (disabled while the bot is offline). No live topic: the status query polls every 60 s.

**Deploy.** Restart the bot (migration 27, new cog, internal routes) and the dashboard (new `/api/fortnite` and `/api/debug` routers, `is_owner` in `/api/me`); the frontend is rebuilt. Then `/fortnite channel set` + `/fortnite enable` (or use the dashboard page).

---

## 25. Owner-only Debug page

**Status: built, unit-tested, frontend built; loaded since the 2026-10-14 bot and dashboard restart, NOT yet exercised live** (nothing was posted to Discord).

A hidden test bench at `/dashboard/debug` so the owner can trigger features without slash commands. **Hidden:** `server/permissions/guards.py` `require_owner` compares the session's Discord user ID with `OWNER_ID` from the root `.env` (`server/config.py`) and returns **404** to everyone else (no session, non-member, staff). The 404 body is byte-identical to an unknown `/api/*` path's (`HiddenRouteNotFound` exception mapped in `server/app.py` to `{"error":"not_found"}`, which is also what the all-methods `/api/{rest:path}` catch-all returns, with or without a frontend build), for every method, body and trailing slash: the owner check runs before body validation (router-level dependency plus raw-body `guards.owner_body`, so no 422 even for malformed JSON), and wrong methods hit the catch-all (no 405 / `Allow`). FastAPI's `/docs`, `/redoc` and `/openapi.json` are disabled (they listed these routes) unless `DASHBOARD_API_DOCS=true`. The frontend bundle does still contain the lazy `DebugPage` chunk and the `/dashboard/debug` nav entry, so someone reading the JS can learn the route exists; they still cannot use it; `/api/me` returns `is_owner`; the frontend adds the `__owner_only` pseudo-permission (`src/lib/owner.ts`, set in `AuthContext`) so the nav item ("Debug", System group), palette entry and route appear only for the owner, and the page renders `NotFound404` for anyone else. The owner id sent to the bot always comes from the session, and the internal `POST /debug/run` refuses (404) any `owner_id` other than `OWNER_ID`.

**Page.** A target text-channel picker (remembered in localStorage) used by every test; a bot health panel (gateway ready, latency, process uptime via psutil, extensions loaded vs `COGS` with load errors captured by `main.py` into `bot.cog_load_errors`, task loops stopped/failed; refreshes every 15 s; full detail stays on Diagnostics); test action cards grouped by feature, each with its own options, an in-flight/2 s cooldown lock, `useBotOffline()` disabling, and a result panel (success with an "Open" message link, or the error); a Fortnite shop data viewer (cached or fresh fetch, raw parsed JSON); and a list of features deliberately not testable.

**Actions** (`services/debug_actions_service.py`): plain message, plain embed, Fortnite shop post (test mode: no role or wishlist pings, dedupe untouched), Fortnite `!shop` page, welcome message, boost message, birthday announcement (owner as the member; reads settings without creating a row; no role grant, nothing marked celebrated), QOTD embed (fallback or typed question; no history row, no ping), reaction-role panel preview and ticket panel preview (pick a panel; buttons are rendered disabled so nothing can be granted or opened; panel `message_id` untouched), render any saved template with the owner as member (covers verify/welcome/ticket/moderation DM templates), log-channel embed for any `log_router` event, honeypot warning embed, honeypot "caught so far" embed (read-only totals), honeypot DM preview (posted in the channel, never DMed, fake appeal link). **Safety:** every send goes through `SafeChannel`: `AllowedMentions` limited to the owner (never @everyone/@here/roles), content prefixed `[TEST] <action>`, and any view with an enabled component is dropped.

**Skipped on purpose** (listed on the page): QOTD staff approval message, ticket open/close/transcript, `!verify` role swap, the honeypot catch flow, kick/ban/timeout/warn, the birthday role grant, scheduled sends/managed messages, music.

**Appeal flow test (2026-10-28, migration 031; built, unit-tested in `tests/unit/test_honeypot_appeal_outcomes.py` and the hidden-route list of `test_debug_routes.py`, frontend typechecked and built; NOT yet live: needs a bot + dashboard restart).** The one Debug feature that deliberately WRITES feature state (the test actions above never do): to try the real appeal sign-in, review and one-click rejoin without a real honeypot ban, the owner enters an alt's User ID and a starting state, and `HoneypotService.create_test_case` records a TEST honeypot ban catch (`is_test = 1`, content `[TEST] ...`; nobody is banned, nothing is sent): `eligible` (catch only: the alt signs in on `/appeal` and files the appeal), `pending` (+ a pending test appeal to approve/deny in Honeypot > Appeals) or `approved` (+ an approved one: the alt, outside the server, goes straight to "Rejoin server"). An appeal filed against a test catch (sign-in or typed-ID path) inherits `is_test`. Test rows are excluded from the catch feed, the stats, the "Caught so far" count and the appeal counts (`honeypot_repo` filters `is_test = 0`), but eligibility, review and rejoin treat them like real ones; the Appeals queue labels them "Test (Debug page)". Refused (400 with a reason, shown to the owner) for an account with real honeypot records, the bot, the owner, an unknown user id or an unknown state. "Delete test data" (`delete_test_cases`) removes every test catch/appeal and publishes `appeals_changed` on the `honeypot` topic; it never touches Discord (an alt that rejoined stays in the server). Audit rows `honeypot_test_case_created` / `honeypot_test_cases_deleted`. Routes: dashboard `GET`/`POST /api/debug/appeal-test`, `POST /api/debug/appeal-test/clear` (router-level `require_owner`, body read with `owner_body`; hidden 404 for everyone else), internal `GET`/`POST /guilds/{id}/debug/appeal-test`, `POST .../appeal-test/clear` (POSTs also require `owner_id == OWNER_ID`). UI: `src/pages/debug/AppealTestCard.tsx` (hooks in `src/api/debug.ts`).

**Lab tab (2026-10-28; built, unit-tested in `tests/unit/test_debug_lab.py` and the hidden-route list of `tests/unit/test_debug_routes.py`, frontend built; the Lab backend is NOT live until the bot AND dashboard restart, until then the tab says so).** The Debug page now has a tab bar (`?tab=lab`): **Actions** = everything above, **Lab** = read-only test tools from `services/debug_lab_service.py` (`src/pages/debug/lab/`, hooks/types in `src/api/debugLab.ts`). All side-effect free: read-only repo calls only (grants, event bindings, templates, saved settings), no audit rows; the only Discord output is the optional "Send copy" of a preview into the Actions tab's target channel through `SafeChannel` (owner-only pings, `[TEST] Lab preview: ...` prefix; the chosen member is never pinged or DMed).
- *Permission simulator:* pick a member (typeahead via the moderation member search, a pasted ID, or "Use me") or a hypothetical role set (+ @everyone). Shows staff bypass (OWNER_ID / STAFF_ROLE_IDS role), every bot permission with the granting role(s) (member mode cross-checks `PermissionService.has_permission` and notes any disagreement), grants on deleted roles, the dashboard nav pages they would see (computed client-side from `NAV_LINKS`, same filter as the sidebar, staff-only/owner-only pseudo-permissions included) and every loaded prefix + slash command with its requirement and Yes / Partly / No. Requirements are read from each command callback's SOURCE (`scan_checks`: `has_permission("x")` with literal or module-constant perms, `is_staff*`, `is_any_staff*` = staff_base, `== OWNER_ID`, following `self.<helper>` / module helpers defined in `cogs.*` up to 3 levels, sorted by source position; the first check is the entry gate, "Partly" = passes it but not every sub-action's), plus Discord-side `default_permissions`. Heuristic, labelled as such.
- *Event message preview:* welcome, boost, or any `/event` binding (member_join, member_leave, boost, role_add, role_remove; optional `{role}`) rendered for a chosen member with the feature's real code (`send_welcome`/`send_boost` into a `CaptureChannel` that records instead of sending; bindings rendered with the binding's channel as context, notes when disabled / no channel / DMs too) and shown in the browser; "Send copy" posts it via SafeChannel.
- *Template render:* any saved template with a chosen member's placeholders, plus the value of every registered placeholder; optional SafeChannel copy.
- *Anti-spam tester:* paste `seconds | user | channel | text` lines (presets: normal chat, flood, cross-channel duplicate, mention spam, cooldown), optionally override thresholds (validated like a Settings save); runs `services/spam_detector.py` with a fake clock and the saved rules; shows per-message verdicts, deletions and would-be timeouts. Pure.
- *Live-event inspector:* every raw WebSocket envelope this tab receives (new `LiveConnection.tap()` / `useLive().tap`, browser-only), pause/clear, hide pong/ack, expandable JSON, and checkboxes to subscribe this tab to any `LIVE_TOPICS` topic.
- *Internal API latency matrix:* the dashboard times a fixed list of ~20 read-only internal GETs one at a time (`server/routes/debug.py` `LATENCY_PROBES`, `internal_client.probe_get`; Diagnostics' dump excluded) and shows status + ms. Works while the bot is down.
Routes: dashboard `GET /api/debug/lab/options`, `POST /api/debug/lab/permissions` `{user_id}|{role_ids}`, `POST /api/debug/lab/preview` `{kind: welcome|boost|event|template, user_id, event_type?, role_id?, template?, send_channel_id?}`, `POST /api/debug/lab/antispam` `{overrides?, messages}`, `GET /api/debug/lab/latency` (router-level `require_owner`, bodies via `owner_body`, owner id from the session); internal `GET /guilds/{id}/debug/lab/options`, `POST .../debug/lab/permissions|preview|antispam` (POSTs require `owner_id == OWNER_ID`, 400 `{"error": "lab_error", "message"}` for fixable problems).

**Errors tab (2026-10-30; built and unit-tested in `tests/unit/test_error_reports.py` and `tests/unit/test_debug_routes.py`, frontend built; the backend is live after the next dashboard AND bot restart; the dashboard Alembic revision `c7e2a9d4b1f3` is already applied to the live `server/dashboard.db`).** `?tab=errors` (short link `/admin`, a plain redirect to `/dashboard/debug?tab=errors`; non-owners end on the same 404) lists every error the site and bot have seen, grouped by fingerprint (same error = one row with a count, first/last seen, up to 5 pages, browsers and signed-in users, the newest sample's stack and React component stack), with a source filter, per-row Dismiss, a two-step Clear, "Send test error" (posts a `test` browser report through the real endpoint), refreshing every 30 s. Sources: **Browser** = reports from `src/lib/errorReport.ts` (see "Crash screen and error reports" in section 9), stored by the dashboard backend in `server/dashboard.db` table `error_reports` (`server/services/error_reports.py`); **Website** = unhandled exceptions in the dashboard backend, recorded by `ErrorCaptureMiddleware` (route TEMPLATE + method, never the raw path; the response is still the normal 500) in the same table; **Bot** = every ERROR/CRITICAL log record of the bot process (failed cogs, prefix/slash command errors, task-loop and event exceptions, any `log.error`/`log.exception`), kept in memory by `core/error_ring.py` (`ErrorRingHandler` attached by `core/logging_setup.setup_logging`, at most 200 groups, empty after every bot start; native crashes stay in `logs/supervisor/bot.log`). Stored reports are scrubbed before storage (diagnostics secret scrubber, appeal tokens, OAuth `code`/`state`/`token` query values), clipped (name 200, message 2000, stack 8000, component stack 4000, path 300, user agent 400 chars), carry the signed-in user from the session (never from the body), never an IP address, and are pruned after `ERROR_REPORT_RETENTION_DAYS` (30) or beyond `ERROR_REPORT_MAX_ROWS` (5000, oldest first), at most every 10 minutes from `record()`. Bot groups are scrubbed when served (`scrub_log_line` per line + `scrub_payload`). Routes: public `POST /api/public/client-errors` `{kind: render|error|rejection|chunk|test, name, message, stack?, component_stack?, path?}` (`server/routes/client_errors.py`: no login needed, 20/minute per client IP (flat `{"error":"rate_limited"}`), 32 KB body cap -> 413 `too_large`, 400 `invalid_request`, CSRF Origin check, 204 with no body when stored; unverified, so the tab labels browser reports as such); owner-only `GET /api/debug/errors` (stored groups + bot groups merged, newest first; `bot.available=false` when the bot does not answer) and `POST /api/debug/errors/clear` `{source?: all|browser|server|bot, id?}`; internal `GET /guilds/{id}/debug/errors` and `POST /guilds/{id}/debug/errors/clear` `{fingerprint?}`. Frontend: `src/pages/debug/errors/ErrorsTab.tsx`, hooks/types in `src/api/debugErrors.ts`.

**Routes.** Internal `internal_api/routes/debug.py` (`/debug/actions`, `/debug/run`, `/debug/fortnite/preview`, `/debug/health`, `/debug/appeal-test*`, `/debug/lab/*`, `/debug/errors*`); dashboard `server/routes/debug.py` (`/api/debug/*`, all `require_owner`). `require_owner` also gates `POST /api/supervisor/rotate-logs` (the owner-only "Rotate logs now" button on the Diagnostics Services card, section 26) and `POST /api/supervisor/update-deps` (the owner-only "Dependency updates" panel there). Deploy: restart the bot and the dashboard (see section 24); `OWNER_ID` must be set in `.env` for the dashboard too.

---

## 26. Supervisor control API (restart from Discord and the dashboard)

**Status: built, unit-tested (in-process, ephemeral port, fake services) including the review fixes below, and ACTIVATED on 2026-10-14:** the old pre-v2.0 supervisor tree was killed with `taskkill /T` and `kick-bot-supervisor.lnk` started again; `supervisor.log` shows `supervisor up v2.0 ... control=127.0.0.1:8767`, `supervisor.py status` returns all three services running, and the bot log shows `Loaded cog: cogs.supervisor_control`. Not yet exercised live: an actual restart through `/restart`, the Diagnostics card or `supervisor.py restart`.

**Topology.**

```
 Discord /restart --> bot (cogs/supervisor_control.py, services/supervisor_client.py, aiohttp) --+
                                                                                                 +--> supervisor.py control API
 browser Diagnostics card --> dashboard /api/supervisor/* (server/services/supervisor_client.py) -+    127.0.0.1:8767, Bearer secret
                                                                                                        |  queue.Queue (not-before time)
                                                                                                        v
                                                                                     main poll loop: Service.request_restart -> kill_tree
```

The dashboard calls the supervisor DIRECTLY, not through the internal API, so a restart works while the bot is down or hung. This is the one exception to "the dashboard only talks to the internal API". The two clients are deliberately duplicated (the processes do not import each other); `supervisor.py` stays stdlib-only, and importing it only reads `.env` and creates `logs/supervisor/` (everything else is in `main()`, behind `if __name__ == "__main__"`).

**Server (`supervisor.py`).** `ThreadingHTTPServer` in a daemon thread, bound to `127.0.0.1` only (not configurable), port `SUPERVISOR_CONTROL_PORT` (8767), `SO_EXCLUSIVEADDRUSE` on Windows so no second process can share the port. Auth `Authorization: Bearer <SUPERVISOR_CONTROL_SECRET, else INTERNAL_API_SECRET>` checked with `hmac.compare_digest` before routing (401 for anything unauthenticated, then 404 unknown path, 405 wrong method with `Allow`). POST bodies: `application/json` only (415), max 4 KB (413), must be an object (400). Each recv has a 5 s socket timeout, and a per-connection timer shuts the socket down after 10 s in total (`REQUEST_DEADLINE`), so a client dribbling header bytes cannot hold a thread; at most 16 connections are handled at once (`MAX_CONTROL_CONNECTIONS`, a semaphore in `process_request`), extra ones are closed immediately without a thread. Request logging goes to `slog` (`logs/supervisor/supervisor.log`), skipping successful GETs (the dashboard polls); the secret is never logged. No secret at all, or the port is taken: the supervisor logs why and runs without the control API.

| Endpoint | Body / query | Response |
|---|---|---|
| `GET /status` | | `{"supervisor": {pid, started_at, version, control: true, restart_cooldown_s}, "services": [{name, running, pid, uptime_s, restarts, last_exit: {reason, at}\|null, last_request: {by, source, reason, at}\|null, health_fails, cooldown_remaining_s, waiting_on_dependency, skip_reason, next_start_in_s, restart_pending}], "log_rotation": {schedule, retention_days, archive_dir, running, pending, last_run: {trigger, requested_by, source, reason, at, finished_at, ok, files: [{key, file, status: rotated\|empty\|missing\|deferred\|failed\|compress_failed, bytes, archive, gz_bytes?, error}], deleted: [...], error?}\|null, next_run_at, overdue, deferred: [...]}}` (`log_rotation` from v2.1; times there are local with UTC offset). From v2.2 each service also has `held` (null, or `"dependency update"` while the updater keeps it stopped) and the payload has `"dependency_update": {running, pending, step, current: {mode, at, requested_by, steps}\|null, last_run: {mode, requested_by, source, reason, at, finished_at, ok, plan: {python: [{package, from, to, why}], python_no_fix, npm: {frontend\|pot_server: {label, fixable, manual, ignored}}, min_severity}, steps: [{name, ok, detail, seconds, output}], changes, manual, restarted, error}\|null, held: [...]}` (times UTC). The dashboard's `GET /api/supervisor/status` drops `dependency_update` for everyone but the owner |
| `POST /restart` | `{target: bot\|dashboard\|tunnel\|all, requested_by: str, source: dashboard\|discord\|bot\|cli, reason: str\|null (cut to 300), delay_s?: number (clamped 0-10, default 1.5)}` | `202 {accepted: [...], skipped: [{name, error, message}], delay_s, eta_s}` (`eta_s` = seconds until the new process is launched, not until it is ready); `429 {error: "cooldown", retry_after_s}` + `Retry-After` when any target was restarted on request within `SUPERVISOR_RESTART_COOLDOWN` (30 s); `409 restart_in_progress` (already queued or being killed) or `409 not_managed` (the service never started because another process the supervisor did not start holds its port, so a restart would do nothing); for `all`, such services are skipped and the rest accepted (409 only when nothing is left); `400` validation errors |
| `POST /rotate-logs` (v2.1) | `{requested_by: str, source: dashboard\|discord\|bot\|cli, reason: str\|null}` (same validation and one-line flattening as `/restart`) | `202 {queued: true, message}`: queued only; the main loop starts the run within a second and the result appears in `/status` `log_rotation.last_run`. `409 rotation_in_progress` (queued or running), `429 cooldown` + `Retry-After` (60 s between accepted requests), `409 not_managed` (no rotator), `400` validation. See "Weekly log rotation" below |
| `POST /update-deps` (v2.2) | `{mode: check\|apply (default apply), requested_by, source, reason}` (same validation as `/rotate-logs`) | `202 {queued: true, mode, message}`: queued; progress and the result appear in `/status` `dependency_update`. `409 update_in_progress` (queued or running), `429 cooldown` + `Retry-After` (60 s between accepted requests), `409 not_managed` (no updater), `400 invalid_mode` / validation. See "Dependency updates" below |
| `GET /logs?service=&lines=` | `lines` 1-200 (default 50) | `{service, lines}`: the in-memory output tail (200 lines kept per child), scrubbed with a stdlib port of the Diagnostics redaction (values of env vars named like token/secret/password/key/webhook/cookie, from the environment and a fresh read of `.env`, the control secret, Discord tokens, JWTs, `Bearer`/`Basic` values, Discord webhook URL tokens) plus access-log redaction: public appeal tokens (`/api/public/appeals/<token>`, `/appeal/<token>`), `code=`/`state=`/`token=` query values (OAuth callback), and client IPs in uvicorn access lines (except `127.0.0.1`) |

**Thread safety.** The HTTP thread never calls `kill_tree` or touches a `Service`: it validates, checks the cooldown/state against a snapshot (rebuilt by the main loop every second, read under a lock) and puts a request with a not-before time on a `queue.Queue`. The main loop drains the queue each tick and applies due requests (`Service.request_restart`). The delay lets the HTTP response, and for a self-restart the requester's reply, get out before the kill.

**Request text.** `reason` (300 chars) and `requested_by` (120) have every run of control characters (CR/LF, tabs, other C0, DEL) replaced by one space before they are stored, logged or alerted, so a reason cannot forge extra `supervisor.log` lines (the only record of dashboard-initiated restarts).

**Requested-restart semantics.** Running service: marked as requested, killed; the exit is NOT a crash: no backoff growth, no quick-crash count, restart after 1 s, log line `restart requested by X via Y: reason`, and an INFO webhook (blue, no ping) instead of the red crash alert; `last_exit.reason` says the same. An exit only counts as requested within 60 s of the kill. If the process had already exited when the request is applied (a crash or health-check kill the main loop had not seen yet), it is NOT marked as requested: the normal crash path keeps the red alert, output tail and backoff. `kill_tree` logs a failed `taskkill` (return code + output); if a requested kill has not ended the process after 15 s (`KILL_CONFIRM_AFTER`) it is retried once, and after another 15 s the request is dropped (log line + yellow no-ping alert "requested restart failed"), so `restart_pending` clears and new requests are accepted again. Not running (in backoff after a crash, waiting on its dependency, or its port was taken): backoff reset and it is started as soon as allowed (info alert; for a dashboard waiting on its dependency the text says it starts once the bot's internal API answers).

**Environment of children.** Each child is started with the real process environment plus a FRESH read of `.env` (`child_env()`; real env vars win, like `load_dotenv()` without override; if `.env` cannot be read at that moment the copy read at startup is used). So a requested or crash restart picks up changed `.env` values. The supervisor's own settings (`SUPERVISOR_*`, and the control secret it derived from `INTERNAL_API_SECRET`) are read once at startup and need a supervisor restart. `all` queues bot, dashboard and tunnel; the dashboard's existing `needs=internal_api_ok` gate keeps it waiting until the restarted bot's internal API answers, so restarting the dashboard while the bot is down leaves it down until the bot is back.

**Single-instance lock.** At the start of `main()`: an exclusive non-blocking OS lock on `logs/supervisor/supervisor.lock` (Windows `msvcrt.locking` on a byte past the pid text; `fcntl.flock` elsewhere), held for the process lifetime; the file holds `<pid> <start time>`. If another process holds it: `slog("another supervisor is already running (pid X) - exiting without starting anything")` and exit code 3, before any service or the control API starts. If the lock file itself cannot be opened, it logs that and continues without the guard. NOTE: the copies running today use the OLD code and take no lock; a stray duplicate keeps running until it is killed, and the tunnel has no port check, so two supervisors mean two `cloudflared` connectors for the same tunnel.

**CLI.** `venv\Scripts\python.exe supervisor.py status`, `... supervisor.py restart <bot|dashboard|tunnel|all> [reason...]` and `... supervisor.py rotate-logs [reason...]` talk to the running supervisor (`source: "cli"`); they never take the lock or start anything. Also (2026-10-28): `summary` (readable table: state per service incl. `restarting` / `waiting for bot` / `not managed` / `down, retry in Ns` / `(health n/3)`, uptime, restarts, last exit with Windows NTSTATUS codes also in hex, last requested restart, log rotation), `pid`, `tail <service> [lines]` (the scrubbed `/logs` tail), `health` and `follow <log>`, plus aliases (`s`, `j`/`json`, `c`, `r`, `o`/`rotate`, `t`, `f`; targets `b`/`d`/`dash`/`t`/`a`) and `help`. `health` probes over HTTP with a User-Agent (Cloudflare 403s urllib's default): the control API, `127.0.0.1:<INTERNAL_API_PORT>/health`, `127.0.0.1:8080/api/status` and `<APPEAL_BASE_URL or https://dash.idlesys.xyz>/api/status`; exit 1 if any fails. Unlike the supervisor's own dashboard check (TCP `port_open(8080)` only, which let a hung dashboard that still accepted connections sit ~55 min before the 3/3 kill on 2026-10-27), it catches a dashboard that accepts but never answers. `follow` reads `logs/supervisor/<supervisor|bot|dashboard|tunnel>.log` straight from disk (works with the supervisor down), opening the file only for each 1 s read so the weekly rotation can still rename it; a file that shrank is announced as rotated and read from the start. Tests: `tests/unit/test_supervisor_cli.py`.

**`sv.bat` (repo root, 2026-10-28).** Short front end for the CLI, for when the supervisor runs windowless (`pythonw` from `kick-bot-supervisor.lnk`). `sv` alone opens a `choice`-based menu (summary on top; one key each for refresh, health, restart submenu with an optional reason, rotate logs with a Y/N, live log in a new window, recent output, start, stop, help, quit); `sv help` lists every command. Everything it does not handle itself is passed to `supervisor.py <args>`. Its own commands: `logs <log>` (opens `supervisor.py follow` in a new window), `start` (does nothing if the control API answers; otherwise `start "" pythonw supervisor.py` and waits up to 20 s for the control API), `stop` (gets the pid from the control API, asks Y/N, `taskkill /PID <pid> /T /F`, which also ends the bot, dashboard and tunnel; refuses when the control API does not answer) and `install` (runs `install_scripts\windows\07-install-sv.ps1`, which asks before each step: a Desktop shortcut "kick-bot control" opening the menu, and adding the repo folder to the user `Path` in `HKCU\Environment`, keeping the value's registry type and unexpanded entries, then broadcasting the change by setting and removing a dummy user variable). Menu reasons have double quotes stripped and are passed as one quoted argument (an empty one becomes `reason: null`). Tested by hand on 2026-10-28 against the live supervisor: help, summary, json, health, pid, tail, start (already-running path), the menu (quit, restart submenu Back, recent output) and the stop pid lookup; `stop`, a menu restart and `install` were not run (they would act on the live stack / the user's profile). The file must keep CRLF line endings (cmd mis-parses labels in LF-only batch files).

**Weekly log rotation (supervisor v2.1; built and unit-tested in `tests/unit/test_supervisor_log_rotation.py` and `tests/unit/test_supervisor_rotate_route.py`, frontend typechecked; live: supervisor v2.1 running since 2026-10-14, first rotation done that day, archives in `logs/archive/`).** `LogRotator` in `supervisor.py`.
- **When:** Mondays 04:00 local time. The main loop calls `tick()` every second; it runs when the most recent Monday 04:00 is later than the last slot handled, which is persisted in `logs/supervisor/log_rotation.json` (`last_slot`, `deferred`, `last_run`). So a slot missed while the PC was off/asleep or the supervisor was down runs once, as soon as the supervisor is up again, and a restart never runs the same slot twice. **First start with no state file: it records the current slot as handled and does NOT rotate; the first automatic run is the next Monday** (the log line says so). `rotate-logs` (CLI, control API, dashboard button) runs one at once and does not move the schedule. The run itself is in a worker thread (gzip of large files never blocks the health checks).
- **Files and how:** `logs/supervisor/bot.log`, `dashboard.log`, `tunnel.log` (written by the supervisor's pump threads through a per-service `LogFile`): under that file's lock the handle is closed and the file renamed into the archive dir; the next write opens a fresh file. While the lock is held the pump waits and the child's output waits in its stdout pipe (no restart, nothing dropped). The lock is released between rename retries, so a reader holding the file (Windows sharing violation) never blocks the pump for long. Works the same when the child is down (nothing open) or restarting (the old and new pump share the `LogFile`; the handle closes when the last one finishes). `logs/supervisor/supervisor.log`: renamed under `slog`'s lock (it opens/appends/closes per line). `bot_startup.log` (repo root): despite the name it is the bot's FULL log, written by a `logging.FileHandler` in `core/logging_setup.py` that stays open for the bot's whole life, but only when the bot runs outside the supervisor (under it `KICKBOT_SUPERVISED=1` skips the handler, so the rename below normally succeeds at once); a Windows file held open that way cannot be renamed, so while the bot runs the rename fails after the retry window (10 attempts, 0.25 s apart) and the file is marked **deferred** (persisted, shown in `/status`, logged, not an error, nothing touched); it is rotated right before the supervisor next starts the bot (`Service.before_start`), when no process holds it. Copy-then-truncate is not used: lines written between the copy and the truncate would be lost. Its content is the same as `logs/supervisor/bot.log` (the bot logs to stdout too), so nothing is missing while it is deferred.
- **Never touched:** `kicks.log` (line 1 `TOTAL_KICKS:N` is the bot's kick counter, read and rewritten by `cogs/kick.py`), `logs/pot_server.log` (the bot's Node child holds it as its stdout; the bot already renames it to `pot_server.log.1` at about 1 MB on each pot-server launch), `.pid`/`.lock` files, `logs/supervisor_restart_marker.json`, `bot.db`, `server/dashboard.db`, `cookies.txt`. The rotator only acts on its explicit file list; retention only deletes names matching its own archive pattern.
- **Archive layout:** `logs/archive/<key>.<YYYY-MM-DD>.log.gz` (key `bot`, `dashboard`, `tunnel`, `supervisor`, `bot_startup`; date = the day the rotation ran; a second run the same day adds `.1`, `.2`). The file is first renamed to `<...>.log` in the archive dir, gzipped to `<...>.log.gz.tmp`, renamed to `.log.gz`, and only then is the uncompressed copy deleted; an uncompressed leftover (supervisor stopped mid-gzip, or gzip failed) is compressed on the next run or supervisor start. Empty or missing files are skipped. `logs/archive/` is under the gitignored `logs/` and is not listed by the Diagnostics Logs section (it scans only `logs/` and `logs/supervisor/`).
- **Retention:** after each run, archives whose date is 28 or more days before today are deleted (so about four weekly archives per file are kept).
- **Logging and alerts:** `supervisor.log` gets a line per rotated file (`log rotation: bot.log -> logs/archive/bot.2026-10-19.log.gz (N bytes, M gzipped)`), per deferral, per failure and per deleted batch, plus a start and a summary line. A failure (file still in use after the retries, gzip error) leaves the data in place, never stops a service or the supervisor, and sends a yellow no-ping webhook alert "Log rotation problem"; no alert on success or deferral.
- **Dashboard:** owner-only "Rotate logs now" button on the Diagnostics Services card (below).

**Bot and dashboard sides.** `/restart` (section 10, `cogs/supervisor_control.py`, staff only, audit-logged) and Diagnostics -> "Services (supervisor.py)" (section 21; routes in section 9 under `supervisor.py`; staff with the bot-down fallback, which applies only when the bot cannot answer, never to a member the bot reports as gone; CSRF origin check from the global middleware; `requested_by` = session username + Discord id; `source: "dashboard"`). Supervisor unreachable maps to "not running under supervisor.py" on both sides (`SupervisorUnavailable` / `503 supervisor_unavailable`).

**Rotate logs from the dashboard (owner only).** `POST /api/supervisor/rotate-logs` (`server/routes/supervisor.py`, body `{reason?}` max 300) is gated by `require_owner`, NOT staff: everyone else, staff included, gets the hidden 404 (`HiddenRouteNotFound`, byte-identical to an unknown `/api/*` path for every method and body: the body is read raw and validated only after the owner check, invalid -> 422 `invalid_request` for the owner only; section 25). It needs no bot call, so it works while the bot is down. It calls `server/services/supervisor_client.rotate_logs()` (same auth, timeout and error mapping as the restart call: 503 `supervisor_unavailable`, 429 cooldown, 409 `rotation_in_progress`) and returns the supervisor's `202 {queued, message}`. Like dashboard restarts, the record is the supervisor's `control: accepted log rotation (requested by <username> (<id>) via dashboard...)` line in `supervisor.log` (no `audit_log` row: the bot may be down). Frontend: the "Rotate logs now" button in `SupervisorPanel.tsx` (hook `useSupervisorRotateLogs` in `src/api/supervisor.ts`) and its confirm dialog (what it archives, fresh files, 4-week retention, bot_startup.log waits for the bot's next restart, kicks.log and pot_server.log untouched) and a "Log rotation: ..." status line (schedule, next run, last run summary, deferred files) are rendered only when `/api/me` says `is_owner` (the `__owner_only` pseudo-permission); for anyone else they are not in the DOM. The button is disabled while the supervisor is unreachable (no status), while a rotation is queued/running, and when the running supervisor predates v2.1 (no `log_rotation` in `/status`); never by `useBotOffline()`. After the 202 it toasts "started", then watches the 5 s status poll for a new finished `last_run` and toasts its summary (rotated files with sizes, deferred, problems, deleted archives). The bot-side `services/supervisor_client.py` / `/restart` have no rotate command (possible follow-up).

**Dependency updates (supervisor v2.2, 2026-11-02; built and unit-tested in `tests/unit/test_supervisor_deps.py` and `tests/unit/test_supervisor_update_route.py`, frontend built; a real `check` run and the frontend build-and-swap were run against this repo. Live since 2026-11-02 12:00, when the supervisor was restarted onto v2.2 (the bot and dashboard came back as its children, so the dashboard route is live too); the owner ran a dashboard Check on 2026-11-03 (clean), and no apply has run yet).** `DependencyUpdater` in `supervisor.py`, so it keeps running while it stops the bot and the dashboard. One run at a time, in a worker thread; the main loop's `tick()` does the stopping and releasing.
- **Plan (both modes):** the same audit as the bot's weekly one (`cogs/dependency_audit.py`): `<venv python> -m pip_audit --format json`, `pip list --outdated` (an outdated yt-dlp is upgraded too), `npm audit --omit=dev --json` in `server/frontend` and `pot_server/server`, with `DEPENDENCY_AUDIT_MIN_SEVERITY` / `DEPENDENCY_AUDIT_IGNORE` read from a fresh `.env`. Python: the highest fix version per vulnerable package. npm: a finding whose `fixAvailable` is `true` or a non-major update counts as fixable; a major bump or no fix goes to `manual` and is NEVER applied. `mode: "check"` stops here (no alert).
- **Apply, dashboard frontend:** package files copied to `logs/supervisor/deps-backup/frontend/`, `npm audit fix`, then `npx --no-install tsc -b` and `npx --no-install vite build --outDir dist-next --emptyOutDir`; `dist/` is renamed to `dist-old/` (retried for 10 s if the dashboard has a file open), `dist-next/` takes its place, `dist-old/` is deleted. No restart (the dashboard reads `dist/` per request; open tabs need a reload, a stale lazy chunk shows the crash screen's "dashboard was updated" text). A failed type-check/build restores `package.json` + `package-lock.json` and runs `npm ci`; `dist/` is untouched. `npm audit fix` that changes nothing is reported, not an error (unless npm itself failed).
- **Apply, Python and the PO-token server:** these need the bot AND the dashboard down (shared venv, Windows locks loaded `.pyd` files; the bot's Node child holds `pot_server/server/node_modules`). The updater sets `Service.hold` on both and kills them; a held service's exit is logged as "stopped for the dependency update" (no crash alert, no backoff) and `poll()` does not start it. It refuses (step failed, nothing installed) when either runs outside the supervisor (port held by another process) or does not stop within 45 s (kill retried once at 15 s); `/restart` of a held service answers 409 `update_in_progress`. PO-token server: `npm audit fix` + `npx --no-install tsc`, restored + `npm ci` + `tsc` on failure. Python: `pip freeze` saved to `logs/supervisor/deps-backup/pip-freeze-before.txt`, `pip install name==fix ...`, `pip check`, then the whole test suite (`python -m pytest -q -x`, with the supervisor's environment WITHOUT `.env`, like a developer shell); if any of the three fails, `pip install -r` the saved freeze puts the old versions back. On success the versions are pinned: existing `name==x` lines in `requirements.txt` / `server/requirements.txt` are rewritten (extras and line endings kept), a package in neither is appended to `requirements.txt`. Then the holds are released and both start normally (dashboard after the bot's internal API). Expect a few minutes of bot + dashboard downtime (the tests alone take over a minute).
- **Result:** `/status` `dependency_update` (live `step`, finished `last_run` with each step's scrubbed last 40 output lines), saved to `logs/supervisor/dependency_update.json` (survives a supervisor restart), `supervisor.log` lines per step, and for an apply one webhook alert (blue when ok, yellow with failed steps, never a ping). Changed `requirements.txt` / `package-lock.json` files are left for you to commit.
- **Dashboard (owner only):** `POST /api/supervisor/update-deps` (`{mode: check|apply, reason?}`, `require_owner` with the hidden 404 and raw-body `owner_body` exactly like `rotate-logs`, works with the bot down), client `server/services/supervisor_client.update_deps`. Frontend: `src/pages/diagnostics/DependencyUpdatePanel.tsx` (rendered by `SupervisorPanel` only for `is_owner`; hook `useSupervisorUpdateDeps` in `src/api/supervisor.ts`): "Check" and "Update now" (confirm dialog spelling out the downtime and rollback), live step, "Waiting for the dashboard to come back" while the dashboard is down, last run with plan / changes / manual work / steps with output. Service cards show "Paused" for a held service. CLI: `supervisor.py update-deps [check|apply] [reason]` (alias `u`; check is the default); `summary` shows the run. The weekly audit's webhook "Fix:" line points to this button.

**Config.** `SUPERVISOR_CONTROL_PORT`, `SUPERVISOR_CONTROL_SECRET`, `SUPERVISOR_RESTART_COOLDOWN` (section 6). Bot: root `config.py` (`SUPERVISOR_CONTROL_URL`); dashboard: `server/config.py` (`SUPERVISOR_BASE_URL`). All three processes read the root `.env`; with no new keys the defaults (8767, `INTERNAL_API_SECRET`, 30 s) work.

**Activate (once).** The new supervisor restarts the bot, dashboard and tunnel as its children, which also loads the bot's new cog and the dashboard's new routes (the frontend is already built). From the repo root in PowerShell:

1. Find the supervisors: `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*supervisor.py*' } | Select-Object ProcessId, Name, CommandLine` (expect the `pythonw.exe` one from `kick-bot-supervisor.lnk` and the stray `python.exe` duplicate).
2. Stop both supervisors AND what they started (killing only the supervisor leaves its children running, and the new supervisor would then refuse to start the bot/dashboard because their ports are taken): `taskkill /PID <pid> /T /F` for each. Then make sure nothing is left: `Get-NetTCPConnection -State Listen -LocalPort 8766,8080,4416 -ErrorAction SilentlyContinue` (no rows) and `Get-Process cloudflared -ErrorAction SilentlyContinue` (stop leftovers with `Stop-Process -Id <id>`).
3. Start it the normal way: double-click `kick-bot-supervisor.lnk` in `shell:startup` (or `Start-Process venv\Scripts\pythonw.exe -ArgumentList supervisor.py -WorkingDirectory (Get-Location)`).
4. Check: `logs/supervisor/supervisor.log` shows `supervisor up v2.0 ... control=127.0.0.1:8767`; `venv\Scripts\python.exe supervisor.py status` prints JSON; the bot log has `Loaded cog: cogs.supervisor_control`; `/restart status` in Discord (a new command can take a moment to appear) and the Diagnostics card both show the three services.

## 27. Audit log

**Status (2026-10-14):** built, unit-tested (`tests/unit/test_audit_log.py`), frontend typechecked. Live since the 2026-10-14 17:43 bot + dashboard restart (frontend rebuilt).

**Data.** The existing `audit_log` table (migration 2; no new migration, the single-guild table stays small and `id` order already gives newest-first). Rows are written by `services/audit_service.py` `AuditService.log` (most cogs) and, for older call sites, `AuditRepository.log` directly. `AuditRepository.log` now stamps `created_at` in Python (same `YYYY-MM-DD HH:MM:SS` UTC format as `CURRENT_TIMESTAMP`) and returns the row id; `AuditService.log` returns it too and publishes the live `audit` event with that id.

**Wire shape** (`serialize_row`, shared by the list endpoint and the live event): `{id, guild_id, actor_id, action, target_type, target_id, detail, detail_raw, created_at}`. `guild_id`/`actor_id`/`target_id` are strings (`target_id` can be a snowflake or a small db id such as a template id), ints in `detail` above 2^53-1 (every snowflake, e.g. `member_id`) become strings, `created_at` is ISO UTC ending in `Z`, and a `detail_json` that does not parse is returned as `detail_raw` instead of failing the page. List entries also carry `actor_name`/`target_name` from the bot cache (no Discord calls; null when unknown), so viewers without `staff_base` (who cannot call `/api/resolve`) still see names.

**Routes.** Internal `GET /guilds/{id}/audit` + `/audit/actions` (`internal_api/routes/audit.py`); dashboard `GET /api/audit` + `/api/audit/actions` (`server/routes/audit.py`, `require_permission("view_audit")`; bad params 422, unreachable or not-ready bot 502 `internal_api_unavailable`, other bot 4xx passed through). Filters (ANDed): `action`, `actor_id`, `target_id`, `target_type`, `since` (inclusive) / `until` (exclusive) ISO datetimes. Pagination is keyset on `id`: pass `next_before_id` as `before_id`; it is null on the last page (the service fetches `limit+1`).

**Page** (`/dashboard/audit`, `src/pages/audit/`, hooks in `src/api/audit.ts`). Filter bar (action select with counts, actor ID, target ID, From/To dates; the To day is inclusive), kept in the URL query (`?action=&actor=&target=&from=&to=`) so a filtered view can be linked; id inputs apply after a 400 ms pause once valid. Rows: relative time (absolute in a tooltip), action badge (colour derived from the verb, label always printed), actor, target, one-line detail preview; expanding shows the detail as key/value pairs (`*_id` snowflakes resolved, `before`/`after` shown as a change) or pretty JSON, the ids with copy buttons, and "This action / This actor / This target" filter shortcuts. Names: `<ResolvedUser>`/`<ResolvedRole>`/`<ResolvedChannel>` when the viewer has `staff_base`, otherwise the server-provided names, else the raw id. "Load more" appends the next page (`useInfiniteQuery`). Skeleton, empty (with and without filters), error and inline-403 states.

**Live.** `useAuditLive(filters, enabled)` subscribes to `audit` once the log query has data: a row matching the active filters is prepended to the first page (deduped by `id`, older-than-top rows are left to the next refetch), the action counts are bumped (a new action re-fetches the list), and a row without an `id` invalidates the `audit` queries. The log polls every 30 s only while the socket is down; the `audit` query root is in `LIVE_ROOTS` for reconnect resync. Rows written through `AuditRepository.log` directly (not `AuditService`) are not published live and appear on the next refetch.

**Retention.** `AUDIT_RETENTION_DAYS = 365` in `services/audit_service.py` (set to `None` to keep everything): `AuditService.log` deletes rows older than that at most once per day per bot process (the first write after start, then the first write 24 h later). No scheduler; a failed prune is logged and never fails the write.

**Permission grant audit rows:** see section 8 "Permission grants".

---

## 28. Music page (dashboard player control)

**State: built and tested 2026-10-28; NOT live until the bot AND the dashboard are restarted** (the frontend `dist/` is built, so the page shows its error state until then). No migration, no new env var.

**Permission.** New grantable `control_music` (section 8) gates viewing and controlling. Discord music commands stay open to everyone. Staff-only on the dashboard: the `!musicstatus` staff view (`GET /api/music/status/staff`) and `!debugurl` (`POST /api/music/debugurl`, masked report only; `--full` is never offered).

**How it works.** `internal_api/routes/music.py` reaches the LIVE `MusicCog` (`bot.get_cog("Music")`) and calls its `dashboard_*` methods, which use the same player state (one guild, `bot.voice_clients[0]`) and core methods as the prefix commands (section 10 `cogs/music.py`). `services/music_control.py` holds the pieces that are not tied to voice objects: `MusicActionError` (code, message, HTTP status), `MusicJobs` (in-memory background jobs, at most 4 running, 15 min TTL, generic error text so stream URLs/paths never leak), validation helpers and bounds.

**Parity with the commands.**

| Discord | Dashboard |
|---|---|
| `!join [id]` | voice channel picker + Join; Move here when already connected (new: `vc.move_to`) |
| `!leave` | Leave |
| `!play <url>` | Add (URL): joins first, then a 202 job resolves and queues (`_enqueue_url`) and starts playback |
| `!play <query>` + number reply | Search (202 job, 10 results) then Queue on a result; only the person who searched can pick (same rule as Discord) |
| `!pause` / `!resume` | Pause / Resume |
| `!skip` | Skip: immediate (holding `control_music` is the authority; no vote) |
| (none) | Stop: clears the queue and stops the current track (`_stop_requested` keeps loop mode from re-queuing it) |
| `!volume <n>` | slider 0-200, sent on release |
| `!np`, `!queue`, `!history` | now-playing card with progress, queue list (first 200 + total), recently played |
| `!previous` | Previous button |
| `!shuffle`, `!remove_queue`, `!remove <i>`, `!move <a> <b>` | Shuffle, Clear (confirm), remove and up/down buttons |
| `!loop`, `!autoplay [on\|off]` | switches (explicit on/off) |
| `!save`, `!load`, `!playlists`, `!delete_playlist` | playlists card (owner rule, section 10) |
| `!musicstatus` | status card (public view; staff see details) |
| `!debugurl` | staff-only debug card |
| `!musichelp`, `/music help` | not needed (the page is the UI) |

**Announcements and audit.** Every dashboard action posts in the music text channel the same message the command would, plus ` (via dashboard by **name**)`, with `AllowedMentions.none()` and escaped names. The music text channel is the channel a music command was last used in (remembered in memory; `!musicstatus`/`!debugurl`/`!musichelp` do not count), else the connected voice channel's own text chat; with neither, nothing is posted. Every control action writes one `audit_log` row `music_control` (`target_type` `music`, detail `{action, via: "dashboard", ...}`); progress is never audited. "Now playing" messages for tracks started from the dashboard go to the same channel.

**Long operations.** yt-dlp work (URL resolve, search, debugurl) always runs as a bot-side job: the browser polls `GET /api/music/jobs/{id}` (or `/api/music/debugurl/{id}`) every 1.5 s, so no request comes near Cloudflare's 100 s cut. Voice joins are bounded to 20 s bot-side; `internal_client` uses a 45 s timeout for music calls. Jobs are lost on a bot restart (poll answers 404 `job_not_found`).

**Privacy.** The state payload never carries stream URLs, cookies, file paths or tokens: queue URLs are shown as-is only for YouTube hosts, other links lose their query and fragment. Status fields go through `sanitize_status_text` like the Discord view.

**Live.** Topic `music` (section 22); `useMusicLive` invalidates on every event; polling (5 s) only while the socket is down. Controls are disabled by `useBotOffline`.

---

## 29. Member profile, verify, purge and message-log export (dashboard parity)

Added 2026-10-28. **Status: built and unit-tested (`tests/unit/test_member_tools.py`), frontend built (so the pages are visible now), but the backend is NOT live until the bot AND the dashboard are restarted** (until then the new pages show their error states).

| Discord command | Dashboard | Gate (same as the command) |
|---|---|---|
| `/userinfo` | `/dashboard/members/:userId` profile (richer: warnings, history, honeypot, ban/departure state) | `staff_base` (records: see the routes table) |
| `/serverinfo` | Overview "Server" card (`GET /api/dashboard/server-info`) | `staff_base` (Overview gate) |
| `!verify` | profile "Verify" | `verify_members`; message goes to a text channel of the Unverified category (where `!verify` works) |
| `!purge` | Message tools "Purge" (202 job + poll) | `manage_messages_bulk`; you must be able to see the channel |
| `!msglog` | Message tools "Message log" + profile "DM me their messages" (DM only) | staff only (`require_staff`) |
| `!warn` / `!warnings` / `!delwarn` / `!timeout` / `!untimeout` | Moderation page + profile | `moderate_members` |
| `!kick` | Moderation page + profile | `kick_members` |
| `!ban` / `!unban` | Moderation page + profile | `ban_members` |
| `!antispam on|off` | Settings > Anti-spam tab (already existed; not duplicated) | `require_staff` |

- **Services:** `services/member_profile_service.py` (profile; non-members via `fetch_user` + `fetch_ban`; status `member`/`banned`/`kicked`/`not_member`; the page overlays the user cache's last known name/avatar/`departed_at`/`departed_by_id`), `services/verify_service.py`, `services/purge_service.py` (bounds, `purge_channel`, `PurgeJobs`: in memory, one running job per channel, finished jobs kept 15 min; a bot restart loses them), `services/message_log_service.py` export helpers. `HoneypotRepository.list_events_for_user` / `get_latest_real_appeal_for_user` (both `is_test = 0`).
- **Rank checks:** every human action from the profile goes through the existing `/api/moderation/*` routes (`check_hierarchy=True`). The profile's `actor_block_reason` is advisory only (shown as a banner).
- **Audit:** `member_verified` / `messages_purged` / `message_log_exported` with `detail.source = "dashboard"`; plus the usual moderation rows.
- **Bounds parity:** purge numbers live in `services/purge_service.py` and are mirrored in `server/routes/moderation.py` (`PURGE_*`, `purge_amount_error`) and `src/api/members.ts` (`checkPurgeAmount`); a test feeds the same inputs to both Python sides.
- **`<ResolvedUser link>`:** opt-in prop; the name links to the profile for `staff_base` viewers. Never pass it inside another link or button.

## 30. Automation page (dashboard parity for /event, /welcome, /boost, /schedule, /autorole, /managed)

**State:** built and tested 2026-10-28, NOT live until the bot and dashboard restart (the page is in the built `dist/` and shows error states until then). No migration (existing tables only).

**What it is.** `/dashboard/automation`, five tabs, each gated by the SAME check as its slash command (staff = owner / `STAFF_ROLE_IDS` bypass every grant):

| Tab | Discord commands | Dashboard gate | Dashboard actions |
|---|---|---|---|
| Events | `/event set`, `clear`, `list`, `test` | `manage_events` | set/edit (channel, template, also DM), clear, **Test** (confirm dialog: posts to the REAL bound channel with the session user as the member, DMs them when the binding has DM on, like `/event test`) |
| Welcome & Boost | `/welcome`, `/boost` `setchannel`, `toggle`, `status` | `staff_base` (same as the cogs' `is_any_staff`) | status, channel (saving one also turns it on, like `setchannel`), on/off, **Send test** (= `setchannel test:True`, posts the real message for the session user) |
| Schedules | `/schedule add`, `remove`, `toggle`, `list` | `manage_schedules` | list with the next 3 run times, create (cron validated bot-side by `POST .../schedules/preview` with the scheduler's own croniter parser, next 5 UTC runs shown while typing), pause/resume, remove |
| Autoroles | `/autorole add`, `remove`, `list` | `manage_autorole` | list (with a grant-time warning when the bot would now refuse the role), create, remove. The role goes through `services/self_role_policy.py` with the session user resolved to a live Member as the configuring actor, exactly like the cog (staff roles, roles with a `permission_grants` row, moderator-permission roles, roles above the actor or the bot -> 409 `role_not_allowed`); the picker greys those out with the reason (`GET .../autoroles/roles`) |
| Managed messages | `/managed list`, `untrack`, `sync` | `manage_templates` | list with jump links, sync status, untrack, sync a template now. New managed messages are still posted from the template editor (`/template post`) |

**Files.** `internal_api/routes/automation.py` (resolves the cog-owned services: `bot.event_service`, `bot.scheduler_service`, `bot.autorole_service`, the ManagedMessages cog's `managed_service`, the Welcome/Boost cogs' `_service`; 503 `feature_unavailable` if a cog is not loaded), `server/routes/automation.py`, the "Dashboard Automation page" block at the end of `server/services/internal_client.py`, `src/api/automation.ts`, `src/pages/automation/*`. Template parity: `internal_api/routes/templates.py` + `server/routes/templates.py` (versions, rollback, export, import, preview), `src/api/templates.ts`, `src/pages/templates/TemplateToolsCard.tsx`, `ImportTemplateDialog.tsx`.

**Actor + audit.** The actor is always the session user (never the body). Audit rows: `event_binding_set`/`event_binding_clear`, `event_test`, `welcome_setchannel`/`welcome_toggle`/`welcome_test`, `boost_setchannel`/`boost_toggle`/`boost_test`, `schedule_add`/`schedule_toggle`/`schedule_remove`, `autorole_add`/`autorole_remove`, `managed_untrack`/`managed_sync`, plus the existing `template.*` rows. Event/schedule/autorole/managed rows are written in the services (so the slash commands now write them too); welcome/boost rows are written by the cogs and, for the dashboard, by the internal route with the same action names.

**Snowflakes.** `SNOWFLAKE_KEYS` gained `trigger_role_id` and `changed_by`; `POST /templates/{id}/post` now stringifies `message_id`.

**Known behaviour kept from the bot.** Resuming a paused schedule keeps its stored `next_run_at`; if that time has passed it posts on the next 30 s tick (the page says so). Autorole delays wait in memory, so a restart during the delay skips that grant.

**Tests.** `tests/unit/test_automation.py`: services on a temp in-memory DB (event set/clear audit + live, cron validation / guild-scoped toggle / next runs, template export-import round trip, managed untrack guild scope, autorole bounds), internal API with a fake guild (autorole self-role refusal for the actor, actor not found, picker reasons, cron preview, 503 when a cog is missing), dashboard routes (per-tab permission gates, staff_base greetings, any-of options, actor from the session). `tests/unit/test_live_dashboard_hub.py` covers the any-of `automation` topic.

## 31. Member hub (`/hub`)

Built and tested 2026-10-30; live after that day's bot + dashboard restart. The home page for regular members, modelled on Discord's own member surfaces: Server Guide (a welcome plus "Getting started" to-dos), Channels & Roles (pick your roles) and Account Standing. MEE6, Carl-bot and Dyno only have admin dashboards, so there was no member page there to copy.

**Flow.** Signed out, `/hub` is a sign-in gate ("Continue with Discord" = `/api/auth/login?next=/hub`, a preview of what's inside, and an appeal link). It uses the normal dashboard login and session (anyone with a Discord account gets a session; nothing extra is stored). `/` sends signed-in people without any bot permission, and people who aren't in the server, to `/hub` instead of a 403. `GET /api/hub` asks the bot for the SESSION user's view (internal `GET /guilds/{id}/hub/{user_id}`). The bot decides membership live, so the login-time `is_guild_member` flag is never trusted here.

**What a member sees** (`services/member_hub_service.py`). Every section is best effort: a missing cog or a failure is left out and named in `unavailable`.

| Section | Data |
|---|---|
| Hero | avatar, display name, time in the server, member/online counts, boosts + level, "Open in Discord" |
| Getting started | verified (only when `VERIFIED_ROLE_ID`/`UNVERIFIED_ROLE_ID` is set), picked a self-role, birthday set |
| Your roles | every POSTED reaction-role panel (`message_id` set) with the roles they hold; clicking toggles (below) |
| Question of the day | latest `qotd_history` row + jump link |
| Upcoming events | Discord scheduled events (scheduled or active, up to 5) |
| Your standing | `good` / `warned` / `timed_out`; warning COUNT and dates only, never the reason or the moderator (staff notes) |
| Now playing | `MusicCog.dashboard_state` (title, channel, listeners), only while connected |
| Your profile | joined, account created, booster since, roles (divider roles such as `─── STAFF ───` are hidden by the frontend, `hubFormat.isDividerRole`) |
| Birthdays | their own entry + the `/birthday show-nearest` window (departed people left out) |
| Your tickets | their OPEN tickets with a jump link |

**Not in the server.** Only their own state: `trap` (a honeypot catch, from `HoneypotService.appeal_status_for_user`: eligible plus their appeal's status and dates, never the staff note; the button goes to `/appeal`, which still needs its own appeal sign-in), `banned` (`guild.fetch_ban`), or `not_member`.

**Self-role toggles** (`member_toggle`) follow the panel buttons' rules (`cogs/reaction_roles.py`): the panel must be in this guild and posted, the role still on it and existing; a GRANT re-checks `self_role_policy.grant_block_reason` (staff, granted or dangerous roles: 403 `role_blocked`); removing is always allowed; an exclusive panel drops the member's other roles from it. Discord audit reason `Member hub (self-role)`. The frontend flips the chip at once and rolls back on a refusal.

**Files.** Bot: `services/member_hub_service.py`, `internal_api/routes/member_hub.py`. Dashboard: `server/routes/hub.py`, `internal_client.get_member_hub` / `toggle_hub_self_role`, `server/auth/oauth.py` (`next`). Frontend: `src/api/hub.ts`, `src/pages/hub/HubPage.tsx`, `HubSections.tsx`, `hubFormat.ts` (SQLite timestamps without a zone are read as UTC), `components/DiscordIcon.tsx`, `PublicShell` `headerActions`. The Privacy Policy ("The member hub") and Terms describe it.

**Tests.** `tests/unit/test_member_hub.py`: toggles (grant/remove, exclusive, privileged refused but droppable, unposted or missing panels), the member view (own data, no warning reasons, only posted panels, `unavailable`), trap/banned/not-member, the internal routes, the dashboard routes always using the session user plus the rate limit, and the login `next` allowlist. The anonymous route walk in `tests/unit/test_security_audit.py` covers both new routes (401).

