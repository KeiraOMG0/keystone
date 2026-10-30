# Install Scripts

Cross-platform setup scripts for this repo (bot + dashboard). Full context on
what these steps actually do lives in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)
(sections 4-7 and 11).

Pick your OS folder: `windows/`, `macos/`, or `linux/`. Each contains the
same four numbered steps plus an `install-all` wrapper that runs them in
order:

| Step | What it does |
|---|---|
| `01-check-tools` | Detects Python, Node.js, and ffmpeg; installs any that are missing via the OS's package manager (winget / Homebrew / apt-dnf-pacman-zypper) |
| `02-setup-python-env` | Creates `venv/`, installs `requirements.txt` + `server/requirements.txt`, copies `.env.example` to `.env` if missing |
| `03-setup-database` | Runs `alembic upgrade head` for the dashboard's database. The bot's own database migrates itself automatically on first run, so there's nothing to do for it here. |
| `04-build-frontend` | `npm install && npm run build` for the React dashboard |

Run them individually if you want to inspect each step, or just run
`install-all` to do everything in sequence.

## Windows

```
install_scripts\windows\install-all.bat
```
(or `install-all.ps1` directly from PowerShell). Individually-numbered
`.bat` files are also provided if you'd rather run one step at a time by
double-click.

## macOS

```
./install_scripts/macos/install-all.sh
```

## Linux

```
./install_scripts/linux/install-all.sh
```
Detects `apt`, `dnf`, `pacman`, or `zypper` automatically. If none of those
are found, it prints manual-install instructions instead of guessing.

## First run

The first time you run any `install-all` (or `02-setup-python-env` on its
own), it will create `.env` from `.env.example` and stop, since it can't
guess your Discord token/secrets for you. Open `.env`, fill in at least:

```
DISCORD_TOKEN=
INTERNAL_API_SECRET=
DISCORD_CLIENT_ID=
DISCORD_CLIENT_SECRET=
SESSION_COOKIE_SECRET=
```

then re-run the same script to continue.

## After install

Start both processes (see `docs/ARCHITECTURE.md` section 11 for full detail,
including production/tunnel setup):

```
# Windows
venv\Scripts\python.exe main.py
venv\Scripts\python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8080

# macOS / Linux
venv/bin/python main.py
venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8080
```

### Windows extras (not run by `install-all`)

- `windows\05-install-autostart.bat`: Startup-folder shortcuts for `run_bot.bat` and `run_dashboard.bat`.
- `windows\06-install-supervisor-autostart.bat`: logon task for `supervisor.py` (needs admin; the live
  setup uses a Startup shortcut `kick-bot-supervisor.lnk` instead).
- `windows\07-install-sv.ps1`: run it with `sv install` from the repo root. Asks before each step:
  a Desktop shortcut "kick-bot control" that opens the `sv.bat` supervisor menu, and adding the repo
  folder to your user PATH so `sv status`, `sv restart dashboard`, ... work from any new terminal.
  `sv help` lists every command.
