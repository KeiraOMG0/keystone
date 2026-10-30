"""
SQLAlchemy 2.0 declarative models for the dashboard backend's own SQLite
database (server/dashboard.db — separate from the bot's bot.db; this process
never touches bot.db).

Typed Mapped[...]/mapped_column() throughout so Alembic generates
dialect-correct DDL, per the approved architecture's future Postgres
migration path.
"""

from __future__ import annotations

import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.db.base import Base


class Session(Base):
    """A logged-in dashboard user's server-side session.

    The session cookie sent to the browser holds only `session_id` (signed,
    so it can't be tampered with in transit) — all other session state lives
    here server-side and is never sent to the client.

    The three Discord OAuth token columns are LEGACY and always NULL: the
    access token is only used inside the OAuth callback and never stored
    (Alembic b3c1d2e4f5a6 made them nullable and nulled existing values).
    They must NEVER be included in any API response sent to the frontend.
    """

    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    discord_username: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    discord_avatar: Mapped[str | None] = mapped_column(String(64), nullable=True)

    discord_access_token: Mapped[str | None] = mapped_column(String(255), nullable=True)  # legacy, always NULL
    discord_refresh_token: Mapped[str | None] = mapped_column(String(255), nullable=True)  # legacy, always NULL
    token_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # legacy

    # Resolved at login time (callback) via GET /users/@me/guilds/{id}/member.
    # Stored rather than re-checked on every request per-route.
    is_guild_member: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cached_role_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class ErrorReport(Base):
    """One error seen by the website: a crash/uncaught error reported by a
    visitor's browser (source "browser", POST /api/public/client-errors) or an
    unhandled exception in this backend (source "server",
    server/services/error_reports.py's middleware). Shown grouped by
    `fingerprint` on the owner-only Debug page's Errors tab. Text is scrubbed
    (secrets, appeal tokens, OAuth values) before it is stored; no IP address
    is kept. Pruned after ERROR_REPORT_RETENTION_DAYS / ERROR_REPORT_MAX_ROWS.
    Bot-process errors are NOT stored here (the bot keeps them in memory,
    core/error_ring.py)."""

    __tablename__ = "error_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # browser | server
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # render | error | rejection | chunk | test | exception
    fingerprint: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    component_stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    path: Mapped[str | None] = mapped_column(String(300), nullable=True)  # page path / "METHOD /route/{template}"
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # signed-in dashboard user, if any
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
