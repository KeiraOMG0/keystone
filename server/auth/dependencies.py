"""
FastAPI dependencies for reading the session cookie and resolving the
current Session row.

The cookie value is signed (itsdangerous) so it can't be tampered with, but
carries no session data itself — it's just the session_id lookup key. All
real state lives in server/dashboard.db.
"""

from __future__ import annotations

import datetime

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.ext.asyncio import AsyncSession as DBAsyncSession

from server import config
from server.auth.session import delete_session, get_session, touch_session
from server.db.engine import async_session_factory
from server.db.models import Session

_serializer = URLSafeSerializer(config.SESSION_COOKIE_SECRET, salt="dashboard-session-cookie")


def sign_session_id(session_id: str) -> str:
    return _serializer.dumps(session_id)


def unsign_session_id(cookie_value: str) -> str | None:
    try:
        value = _serializer.loads(cookie_value)
    except BadSignature:
        return None
    if not isinstance(value, str):
        return None
    return value


async def _get_db() -> DBAsyncSession:
    async with async_session_factory() as db:
        yield db


async def get_current_session(
    request: Request,
    db: DBAsyncSession = Depends(_get_db),
) -> Session | None:
    """Returns the current Session row, or None if there isn't a valid one.

    Also enforces sliding expiry: a session whose last_seen_at is older than
    SESSION_MAX_AGE_SECONDS is treated as expired (and deleted) even though
    the DB row existed.
    """
    return await load_valid_session(db, request.cookies.get(config.SESSION_COOKIE_NAME))


async def load_valid_session(
    db: DBAsyncSession, cookie_value: str | None, *, touch: bool = True
) -> Session | None:
    """Shared by the HTTP dependency above and the browser websocket
    endpoint: verify the signed cookie, load the row, enforce sliding expiry
    (expired rows are deleted) and (throttled) touch it."""
    if not cookie_value:
        return None

    session_id = unsign_session_id(cookie_value)
    if session_id is None:
        return None

    session = await get_session(db, session_id)
    if session is None:
        return None

    now = datetime.datetime.now(datetime.timezone.utc)
    last_seen = session.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=datetime.timezone.utc)
    age = (now - last_seen).total_seconds()
    if age > config.SESSION_MAX_AGE_SECONDS:
        await delete_session(db, session_id)
        return None

    if touch:
        session = await touch_session(db, session)
    return session


async def require_session(
    session: Session | None = Depends(get_current_session),
) -> Session:
    """401 {"error": "unauthenticated"} if there's no valid session."""
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthenticated"},
        )
    return session
