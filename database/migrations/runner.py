from database.connection import Database


async def run_migrations(db: Database, migrations: list[tuple[int, str, str]]):
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    applied = {row["version"] for row in await db.fetchall("SELECT version FROM schema_migrations")}

    for version, name, sql_path in sorted(migrations, key=lambda m: m[0]):
        if version in applied:
            continue

        with open(sql_path, "r", encoding="utf-8") as f:
            sql = f.read()

        await db.executescript(sql)
        await db.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (version, name),
        )

        print(f"Applied migration {version}: {name}")
