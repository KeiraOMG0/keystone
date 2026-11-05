# Keystone

A self-hosted Discord bot for running one community server, plus a web dashboard for its staff. Built by **Keira** ([KeiraOMG0](https://github.com/KeiraOMG0)) for Gamer Girls HQ.

Keystone was called kick-bot during development, so that name still appears in some files, scripts and process names (`sv.bat`, the supervisor, logs). Both names refer to the same project.

- **Moderation:** warnings, kick/ban/timeout with rank checks, purge by count or time window, automatic spam detection (including spam from user-installed apps), a 7-day message log, a full audit log.
- **Members:** verification, tickets (support and verify), welcome and boost messages, reaction-role and button self-role panels, autoroles, birthdays, question of the day (local AI through Ollama), music from YouTube, a Fortnite item-shop post.
- **Trap channel ("honeypot"):** catches spam accounts and bots, with public ban appeals that use Discord sign-in.
- **Templates:** reusable messages and embeds with 100+ placeholders (`{mention}`, `{reason|No reason given}`, `{channel:general}`, ...).
- **Dashboard:** everything above from the browser, gated by the bot's own permission system (roles get permissions such as `moderate_members` or `manage_tickets`; Discord's permissions are not used). Live updates over WebSocket, a diagnostics page, and an owner-only debug page with an error log.
- **Operations:** a supervisor that restarts crashed processes, rotates logs weekly, sends crash alerts to a Discord webhook, and runs a weekly dependency audit (pip-audit + npm audit).

Full technical reference: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Contributor quick reference: [`CLAUDE.md`](CLAUDE.md).

## How it fits together

```
Discord <── bot (main.py) ── internal API 127.0.0.1:8766 ──┐
                 └── PO-token server 127.0.0.1:4416 (music) │ bearer secret
Browser ── (optional Cloudflare Tunnel) ── dashboard backend 127.0.0.1:8080 (server/)
                                            └── serves the built React app (server/frontend/dist)
supervisor.py runs and watches all three (bot, dashboard, tunnel)
```

The bot and the dashboard are separate processes that share one `.env`. The dashboard never opens the bot's database; it calls the bot's internal API.

## Requirements

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.13 (what it is developed and tested on) | bot and dashboard |
| Node.js + npm | Node 22+ | building the dashboard and the music PO-token server |
| FFmpeg on `PATH` | any recent | music playback |
| Ollama | optional | question of the day |
| cloudflared | optional | putting the dashboard on the internet |

Windows is the main platform (`.bat` scripts, `sv.bat`). The Python and Node parts are cross-platform, and `scripts/` has macOS and Linux versions, but those are less tested.

## 1. Create the Discord application

1. At <https://discord.com/developers/applications>, create an application and add a **Bot**.
2. Under **Bot**, turn on the privileged intents **Server Members**, **Message Content** and **Presence**, and copy the token.
3. Under **OAuth2**, copy the client ID and client secret, and add these redirects (use your public URL instead of `localhost:8080` if you host the dashboard):
   - `http://localhost:8080/api/auth/callback` (dashboard sign-in)
   - `http://localhost:8080/api/public/appeal-auth/callback` (ban appeal sign-in)
4. Invite the bot to your server with the `bot` and `applications.commands` scopes and the Administrator permission (or the moderation, role and channel permissions you want it to have).

## 2. Install

```bat
git clone https://github.com/KeiraOMG0/keystone.git
cd keystone
python -m venv venv
venv\Scripts\pip install -r requirements.txt -r server\requirements.txt
copy .env.example .env
```

(macOS / Linux: `python3 -m venv venv`, `venv/bin/pip ...`, `cp .env.example .env`.)
`scripts/` can do steps 2 to 4 for you: see [`scripts/README.md`](scripts/README.md).

## 3. Configure `.env`

Fill in at least:

| Variable | What |
|---|---|
| `GUILD_ID` | your server's ID (the bot leaves every other server) |
| `DISCORD_TOKEN` | the bot token |
| `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` | from the OAuth2 page |
| `INTERNAL_API_SECRET` | a long random string (shared by the bot and the dashboard) |
| `SESSION_COOKIE_SECRET` | another, different long random string |
| `OWNER_ID` | your Discord user ID (full access, owner-only pages) |
| `STAFF_ROLE_IDS` | comma-separated role IDs that get full access |

Make random secrets with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Every other setting (channels, roles, music, anti-spam, the dashboard URL, webhooks, ...) is described in `.env.example`. Most of them can be changed later on the dashboard's Settings page. Never commit `.env`.

## 4. Build

```bat
REM dashboard database (sessions, error reports)
venv\Scripts\python.exe -m alembic -c server\alembic.ini upgrade head

REM dashboard website -> server\frontend\dist
cd server\frontend
npm ci
npm run build
cd ..\..

REM music PO-token server -> pot_server\server\build (only needed for music)
cd pot_server\server
npm ci
npx tsc
cd ..\..
```

The bot's own database (`bot.db`) is created and migrated automatically on first start.

## 5. Run

Pick one way:

| Way | Command | Notes |
|---|---|---|
| Supervisor (recommended) | `venv\Scripts\python.exe supervisor.py` | runs the bot, dashboard and tunnel; restarts them if they crash; weekly log rotation; crash alerts to `SUPERVISOR_WEBHOOK_URL` |
| `sv` menu (Windows) | `sv start`, `sv s` (status), `sv r a <reason>` (restart all), `sv help` | runs the supervisor without a window; `scripts\windows\06-install-supervisor-autostart.bat` starts it at login |
| Separate windows | `scripts\windows\start_all.bat`, or `run_bot.bat` then `run_dashboard.bat` from the same folder | no auto-restart; start the bot before the dashboard |

Open the dashboard at **`http://localhost:8080`** (not `127.0.0.1`: sign-in cookies and the OAuth redirect are tied to the exact host) and sign in with Discord.

To put it online, point a Cloudflare Tunnel (or another HTTPS reverse proxy) at `http://localhost:8080`. Then set `FRONTEND_BASE_URL`, `DISCORD_OAUTH_REDIRECT_URI` and `APPEAL_BASE_URL` to the public URL, and add the public redirect URLs in the Developer Portal. `server\start_tunnel.bat` runs `cloudflared tunnel run <name>`.

First steps in Discord: `/help` lists everything. Grant permissions to your staff roles on the dashboard's **Permissions** page, and set channels and roles on **Settings**.

## After changing things

| Changed | Do |
|---|---|
| bot Python files | restart the bot (`sv r b <reason>`) |
| `server/**/*.py` | restart the dashboard (`sv r d <reason>`) |
| frontend (`server/frontend/src`) | `npm run build` (no restart; refresh the page) |
| `.env` | restart whichever process reads the value |
| a new dashboard migration | `alembic upgrade head`, then restart the dashboard |
| Python packages | stop the bot first (Windows locks loaded files), `pip install -r requirements.txt`, start again |

## Development

```bat
venv\Scripts\python.exe -m pytest tests -o asyncio_mode=auto
cd server\frontend && npm run dev     REM Vite dev server on :5173, proxies /api to :8080
```

`npm run build` also type-checks the frontend. Security notes, the permission model and every feature are documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Updating dependencies

`!depaudit` (staff) or the weekly run reports vulnerable or outdated packages to your webhook. To update Python packages: stop the bot, run `venv\Scripts\pip install -U <package>`, pin the new version in `requirements.txt`, run the tests, then start again. For the frontend or the PO-token server: run `npm update` / `npm audit fix` in that folder, rebuild, then restart.

## Licence and credits

GPL-3.0 (`LICENSE`). The birthday system is a port of [BirthdayBot](https://github.com/NoiTheCat/BirthdayBot) by NoiTheCat and stays AGPL-3.0 (`LICENSES/AGPL-3.0.txt`). If you run a modified version for other people, the AGPL asks you to offer them its source. `pot_server/` is [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) by Brainicism (GPL-3.0). The cookie extension is [musicbot-cookie-sync](https://github.com/KeiraOMG0/musicbot-cookie-sync). Everything else this project builds on, with licences: [`CREDITS.md`](CREDITS.md).

Copyright (C) 2026 Keira.
