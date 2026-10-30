"""
Session store: CRUD helpers over server.db.models.Session.

Sessions are opaque server-side state keyed by a random session_id. The
cookie sent to the browser holds only that ID (itsdangerous-signed so it
can't be forged) — never any Discord token or role data directly.
"""

from __future__ import annotations

import datetime
import secrets

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession as DBAsyncSession

from server import config
from server.db.models import Session


def generate_session_id() -> str:
    return secrets.token_urlsafe(32)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def create_session(
    db: DBAsyncSession,
    *,
    discord_user_id: int,
    discord_username: str,
    discord_avatar: str | None,
    is_guild_member: bool,
    cached_role_ids: list[int],
    # Accepted for old callers and IGNORED: Discord OAuth tokens are no longer
    # stored (nothing used them; Alembic b3c1d2e4f5a6 nulled the old ones).
    discord_access_token: str | None = None,
    discord_refresh_token: str | None = None,
    token_expires_at: datetime.datetime | None = None,
) -> Session:
    session = Session(
        session_id=generate_session_id(),
        discord_user_id=discord_user_id,
        discord_username=discord_username,
        discord_avatar=discord_avatar,
        discord_access_token=None,
        discord_refresh_token=None,
        token_expires_at=None,
        is_guild_member=is_guild_member,
        cached_role_ids=cached_role_ids,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def get_session(db: DBAsyncSession, session_id: str) -> Session | None:
    result = await db.execute(select(Session).where(Session.session_id == session_id))
    return result.scalar_one_or_none()


async def delete_session(db: DBAsyncSession, session_id: str) -> None:
    await db.execute(delete(Session).where(Session.session_id == session_id))
    await db.commit()


async def delete_user_sessions(db: DBAsyncSession, discord_user_id: int) -> list[str]:
    """Delete every session of one Discord user ("log out everywhere").
    Returns the deleted session ids (the caller closes their live sockets)."""
    result = await db.execute(select(Session.session_id).where(Session.discord_user_id == discord_user_id))
    ids = [row[0] for row in result.all()]
    if ids:
        await db.execute(delete(Session).where(Session.discord_user_id == discord_user_id))
        await db.commit()
    return ids


async def touch_session(db: DBAsyncSession, session: Session, *, force: bool = False) -> Session:
    """Sliding expiry: bump last_seen_at on activity, but at most once per
    SESSION_TOUCH_INTERVAL_SECONDS (default 5 min) so an ordinary request does
    not turn into a DB write. `force=True` always writes."""
    now = _utcnow()
    last = session.last_seen_at
    if last is not None and not force:
        if last.tzinfo is None:
            last = last.replace(tzinfo=datetime.timezone.utc)
        if (now - last).total_seconds() < config.SESSION_TOUCH_INTERVAL_SECONDS:
            return session
    session.last_seen_at = now
    await db.commit()
    await db.refresh(session)
    return session
