from database.repositories.base import BaseRepository


class VersionRepository(BaseRepository):

    async def save_snapshot(
        self,
        template_id: int,
        snapshot_json: str,
        changed_by: int,
        change_summary: str = None,
    ) -> int:
        row = await self._db.fetchone(
            "SELECT COALESCE(MAX(version_number), 0) AS max_ver FROM template_versions WHERE template_id = ?",
            (template_id,),
        )
        next_version = dict(row)["max_ver"] + 1
        return await self._db.execute_returning_id(
            """
            INSERT INTO template_versions (template_id, version_number, snapshot_json, changed_by, change_summary)
            VALUES (?, ?, ?, ?, ?)
            """,
            (template_id, next_version, snapshot_json, changed_by, change_summary),
        )

    async def list_versions(self, template_id: int, limit: int = 20) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM template_versions WHERE template_id = ? ORDER BY version_number DESC LIMIT ?",
            (template_id, limit),
        )
        return [dict(row) for row in rows]

    async def get_version(self, template_id: int, version_number: int) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM template_versions WHERE template_id = ? AND version_number = ?",
            (template_id, version_number),
        )
        return dict(row) if row else None

    async def prune_old(self, template_id: int, keep: int = 20) -> None:
        await self._db.execute(
            """
            DELETE FROM template_versions
            WHERE template_id = ?
              AND id NOT IN (
                  SELECT id FROM template_versions
                  WHERE template_id = ?
                  ORDER BY version_number DESC
                  LIMIT ?
              )
            """,
            (template_id, template_id, keep),
        )
