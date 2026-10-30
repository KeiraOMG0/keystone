import aiosqlite
from contextlib import asynccontextmanager


class Database:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def fetchone(self, sql: str, params: tuple = ()):
        async with self._conn.execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()):
        async with self._conn.execute(sql, params) as cursor:
            return await cursor.fetchall()

    async def execute(self, sql: str, params: tuple = ()) -> int:
        async with self._conn.execute(sql, params) as cursor:
            await self._conn.commit()
            return cursor.rowcount

    async def execute_returning_id(self, sql: str, params: tuple = ()) -> int:
        async with self._conn.execute(sql, params) as cursor:
            await self._conn.commit()
            return cursor.lastrowid

    async def executescript(self, sql: str):
        await self._conn.executescript(sql)
        await self._conn.commit()

    @asynccontextmanager
    async def transaction(self):
        await self._conn.execute("BEGIN")
        try:
            yield self
            await self._conn.execute("COMMIT")
        except Exception:
            await self._conn.execute("ROLLBACK")
            raise
