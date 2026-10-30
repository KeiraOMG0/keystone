"""
Alembic environment for the dashboard backend's own database
(server/dashboard.db). Scoped ONLY to server/db/models.py's metadata —
distinct alembic_version table from the bot's schema_migrations, distinct
migrations directory, per the approved architecture. This must never be
pointed at bot.db.

Uses a synchronous sqlite3 connection for migrations (simpler and the
standard Alembic pattern) even though the app itself uses aiosqlite for
runtime queries — DDL via Alembic doesn't need to be async.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the repo root importable regardless of CWD the alembic CLI is
# invoked from (mirrors server/config.py's explicit-path approach).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from server import config as dashboard_config  # noqa: E402
from server.db.base import Base  # noqa: E402
from server.db import models  # noqa: E402  (import registers models on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Override the ini's placeholder URL with the real (sync) sqlite path,
# computed from server/config.py so there's exactly one source of truth
# for where dashboard.db lives.
_sync_db_url = f"sqlite:///{dashboard_config.DASHBOARD_DB_PATH}"
config.set_main_option("sqlalchemy.url", _sync_db_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
