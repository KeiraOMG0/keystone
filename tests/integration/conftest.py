import pathlib
import pytest_asyncio

from database.connection import Database
from database.repositories.guild_repo import GuildRepository

# Resolved relative to the repo (tests/integration/conftest.py -> repo root),
# never a machine-specific absolute path.
_MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "database" / "migrations" / "sql" / "001_initial.sql"
)


def _load_migration_sql() -> str:
    # Fail loudly if the migration is missing rather than silently testing a
    # divergent inline copy of the schema.
    return _MIGRATION_PATH.read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def db():
    """In-memory aiosqlite Database with schema applied."""
    database = Database(":memory:")
    await database.connect()
    await database.executescript(_load_migration_sql())
    yield database
    await database.close()


@pytest_asyncio.fixture
async def guild_repo(db):
    """GuildRepository backed by the in-memory db fixture."""
    return GuildRepository(db)
