# Credits and licensing

## This project's licence

Keystone (formerly kick-bot) is free software, released under the **GNU General Public License v3.0** (`LICENSE`), with one part under the **GNU Affero General Public License v3.0** (`LICENSES/AGPL-3.0.txt`). That part is listed below.

Copyright (C) 2026 Keira ([KeiraOMG0](https://github.com/KeiraOMG0))

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the licence texts for details.

### Parts under the AGPL-3.0

The birthday system is a Python port of **[BirthdayBot](https://github.com/NoiTheCat/BirthdayBot)** by **NoiTheCat**, licensed AGPL-3.0. That covers its commands and their structure (`/birthday`, `/config`, `/override`, `/birthdays export|import`), the `%n` / `%e` announcement placeholders, the announcement and role timing, and the export formats. These files stay under the AGPL-3.0 and say so in their headers:

- `cogs/birthdays.py`, `cogs/birthday_config.py`, `cogs/birthday_override.py`, `cogs/birthday_io.py`
- `services/birthday_service.py`, `database/repositories/birthday_repo.py`
- the birthday migrations `database/migrations/sql/012_birthdays.sql` to `015_birthday_nullable_tz.sql` (not edited: applied migrations are never changed)

The GPL-3.0 (section 13) explicitly allows combining GPL-3.0 code with AGPL-3.0 code. Each part keeps its own licence, and the AGPL's extra condition applies to the combined program: if you run a **modified** version and people interact with it over a network (for example members using its birthday commands on Discord), you must offer them the corresponding source code. This repository is public and linked from the website's member hub (`/hub`, "Bot source code"), which satisfies this for the running bot.

### Vendored code

- **`pot_server/`**: [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) by **Brainicism** and contributors, GPL-3.0 (its own `pot_server/LICENSE`). It produces the YouTube proof-of-origin tokens the music player needs. It builds on [BgUtils](https://github.com/LuanRT/BgUtils) by **LuanRT** (MIT) and [YouTube.js](https://github.com/LuanRT/YouTube.js) (MIT). It runs as a separate local process; the bot only talks to it over HTTP.

### Project contributions

- **Keystone** itself: written by Keira ([KeiraOMG0](https://github.com/KeiraOMG0)).

- **[musicbot-cookie-sync](https://github.com/KeiraOMG0/musicbot-cookie-sync)** by Keira (MIT): the Chrome extension that keeps the music player's YouTube cookies fresh. The bot's cookie listener speaks its `GET /needs-refresh` / `POST /cookies` protocol (`docs/COOKIE_EXTENSION.md`).
- **Frontend UI components** in `server/frontend/src/components/ui/` start from [shadcn/ui](https://ui.shadcn.com) (MIT, © shadcn) and use [Radix UI](https://www.radix-ui.com) primitives (MIT, © WorkOS).

## Libraries

Versions are the pinned ones at the time of writing; licences were read from each installed package's own metadata. Every licence below is compatible with the GPL-3.0.

### Bot and dashboard backend (Python, `requirements.txt`, `server/requirements.txt`)

| Library | Licence | Used for |
|---|---|---|
| [discord.py](https://github.com/Rapptz/discord.py) | MIT | Discord bot framework |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp), yt-dlp-ejs | Unlicense (ejs: Unlicense AND MIT AND ISC) | music extraction |
| [bgutil-ytdlp-pot-provider](https://pypi.org/project/bgutil-ytdlp-pot-provider/) (plugin) | GPL-3.0 | yt-dlp plugin for the PO-token server |
| [mutagen](https://github.com/quodlibet/mutagen) | GPL-2.0-or-later | audio metadata |
| [PyNaCl](https://github.com/pyca/pynacl), davey | Apache-2.0, MIT | Discord voice encryption |
| [FastAPI](https://github.com/fastapi/fastapi), Starlette, pydantic | MIT, BSD-3-Clause, MIT | internal API and dashboard backend |
| [uvicorn](https://github.com/encode/uvicorn), [httpx](https://github.com/encode/httpx), [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause | ASGI server, HTTP client, live updates |
| [SQLAlchemy](https://www.sqlalchemy.org), [Alembic](https://github.com/sqlalchemy/alembic), [aiosqlite](https://github.com/omnilib/aiosqlite) | MIT | databases and migrations |
| [aiohttp](https://github.com/aio-libs/aiohttp) (+ multidict, yarl, frozenlist, aiosignal, propcache, aiohappyeyeballs) | Apache-2.0 / MIT, PSF-2.0 | HTTP client used by discord.py |
| [slowapi](https://github.com/laurentS/slowapi), [itsdangerous](https://github.com/pallets/itsdangerous), aiofiles | MIT, BSD-3-Clause, Apache-2.0 | rate limits, signed cookies, static files |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause | diagnostics |
| [croniter](https://github.com/kiorky/croniter), python-dateutil, tzdata | MIT, Apache-2.0/BSD dual, Apache-2.0 | schedules, dates, time zones |
| python-dotenv, requests, urllib3, certifi, idna, charset-normalizer, brotli, attrs, six, packaging, colorama, cffi, pycparser, pycryptodomex, audioop-lts | BSD-3 / Apache-2.0 / MIT / MPL-2.0 (certifi) / PSF-2.0 | supporting libraries |

Development and maintenance only (not needed to run the bot): pytest (MIT), pytest-asyncio (Apache-2.0), pip-audit (Apache-2.0).

### Dashboard frontend (`server/frontend/package.json`)

| Library | Licence |
|---|---|
| [React](https://react.dev), react-dom | MIT |
| [React Router](https://reactrouter.com) | MIT |
| [TanStack Query](https://tanstack.com/query) | MIT |
| [Radix UI](https://www.radix-ui.com), [shadcn/ui](https://ui.shadcn.com) | MIT |
| [Tailwind CSS](https://tailwindcss.com), tailwind-merge, tw-animate-css, clsx | MIT |
| class-variance-authority | Apache-2.0 |
| [Lucide](https://lucide.dev) icons | ISC |
| [Recharts](https://recharts.org) | MIT |
| [Geist](https://vercel.com/font) and Geist Mono fonts (via Fontsource) | SIL Open Font License 1.1 |
| Build: [Vite](https://vite.dev), TypeScript, oxlint | MIT, Apache-2.0, MIT |

### PO-token server (`pot_server/server/package.json`)

express, axios, youtubei.js, bgutils-js, jsdom, canvas, commander, proxy-agent: all MIT.

## Related projects by Keira

- **[ytqueue](https://github.com/KeiraOMG0/ytqueue)** (MIT): a Discord-agnostic YouTube player core (yt-dlp + ffmpeg, PO-token server, cookie sync), extracted from the playback fixes made in this bot's music player and another bot. kick-bot does not depend on it; it is the reusable version of the same work.

## Services and tools used at runtime (not included in this repository)

- **Discord** (API and gateway), **YouTube** (music, via yt-dlp), **[Fortnite-API.com](https://fortnite-api.com)** (daily item shop data), **Cloudflare Tunnel** (`cloudflared`, website access).
- **FFmpeg** (audio playback) and **Node.js** (the PO-token server), installed separately.
- **[Ollama](https://ollama.com)** with a local model (default `llama3.2:3b`, under Meta's Llama 3.2 Community License) for question-of-the-day generation. The model is downloaded by Ollama and is not part of this project.
