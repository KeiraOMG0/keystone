"""
Async SQLAlchemy engine + session factory for the dashboard backend's own
SQLite database. Separate file (server/dashboard.db) from the bot's bot.db —
per the approved architecture, this process never opens bot.db directly;
anything bot-state-related goes through server/services/internal_client.py.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server import config

engine = create_async_engine(
    f"sqlite+aiosqlite:///{config.DASHBOARD_DB_PATH}",
    echo=False,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
