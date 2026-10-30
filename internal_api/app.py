"""
FastAPI application factory for the internal API.

This app is embedded in the bot process (see main.py) — it is not a
separate process. main.py sets `app.state.bot = bot` right after creating
this app so route dependencies (internal_api.deps.get_bot) can reach the
live discord.py Bot instance.

Scope: stats, permission-check, and moderation (warnings) routes, plus WS /ws
(live events, dashboard backend only). No dashboard backend and no OAuth — those
live in server/.
"""

from __future__ import annotations

from fastapi import FastAPI

from internal_api.routes import (
    diagnostics,
    honeypot,
    moderation,
    permissions,
    qotd,
    reaction_roles,
    resolve,
    settings,
    stats,
    templates,
    ticket_panels,
    tickets,
    ws,
)
from internal_api.routes import role_admin  # dashboard Roles page (/role-admin/...)
from internal_api.routes import debug, fortnite  # Fortnite shop config + owner-only Debug page
from internal_api.routes import audit  # dashboard Audit log page (read-only audit_log)
from internal_api.routes import automation  # dashboard Automation page (/automation/...)
from internal_api.routes import birthdays  # dashboard Birthdays page (/birthdays/...)
from internal_api.routes import member_tools  # member profile, verify, purge job, msglog DM export, server info
from internal_api.routes import music  # dashboard Music page (/music/...), drives the live MusicCog


def create_app() -> FastAPI:
    app = FastAPI(
        title="Internal Bot API",
        description="Internal-only API embedded in the bot process. Not internet-facing.",
        version="0.1.0",
        # Only GET /health may answer without the bearer; the schema/docs pages would too.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.include_router(stats.router)
    app.include_router(permissions.router)
    app.include_router(moderation.router)
    app.include_router(tickets.router)
    app.include_router(reaction_roles.router)
    app.include_router(resolve.router)
    app.include_router(templates.router)
    app.include_router(ticket_panels.router)
    app.include_router(qotd.router)
    app.include_router(settings.router)
    app.include_router(honeypot.router)
    app.include_router(diagnostics.router)
    app.include_router(role_admin.router)  # dashboard Roles page
    app.include_router(fortnite.router)  # Fortnite shop config/status/post-now
    app.include_router(debug.router)  # owner-only Debug page test actions
    app.include_router(audit.router)  # GET /guilds/{id}/audit (+ /actions) for the Audit log page
    app.include_router(automation.router)  # events / welcome+boost / schedules / autoroles / managed messages
    app.include_router(member_tools.router)  # member profile / verify / purge / msglog export / server-info
    app.include_router(birthdays.router)  # Birthdays page: list / config / override / test / timers / import-export
    app.include_router(music.router)  # Music page: state / control / play+search jobs / playlists / status
    app.include_router(ws.router)  # WS /ws: live-event stream for the dashboard backend only

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app
