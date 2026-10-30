"""
server — the dashboard backend.

A SEPARATE FastAPI process from internal_api/ (which is embedded in the bot
process, see main.py). This process:

  - Handles Discord OAuth2 login and server-side sessions (server/auth/).
  - Is the ONLY process that talks to a browser directly.
  - Never touches bot.db or live discord.py objects directly — for anything
    bot-state-related it calls Step 1's internal API over HTTP
    (server/services/internal_client.py is the sole module allowed to do so).

Run with: uvicorn server.app:app --host 127.0.0.1 --port 8080 (from repo root)
"""
