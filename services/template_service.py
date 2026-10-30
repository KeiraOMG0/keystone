import json
import re

from database.repositories.template_repo import TemplateRepository
from database.repositories.embed_repo import EmbedRepository
from database.repositories.version_repo import VersionRepository
from database.repositories.audit_repo import AuditRepository

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class TemplateService:
    def __init__(
        self,
        template_repo: TemplateRepository,
        embed_repo: EmbedRepository,
        version_repo: VersionRepository,
        audit_repo: AuditRepository,
    ):
        self._templates = template_repo
        self._embeds = embed_repo
        self._versions = version_repo
        self._audit = audit_repo

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_name(self, name: str) -> list[str]:
        errors = []
        if not name:
            errors.append("Name must not be empty.")
        elif len(name) > 50:
            errors.append("Name must be 50 characters or fewer.")
        elif not _NAME_RE.match(name):
            errors.append("Name may only contain letters, digits, hyphens, and underscores.")
        return errors

    async def _snapshot(self, template_id: int, actor_id: int, summary: str = None) -> None:
        full = await self._templates.get_full(template_id)
        await self._versions.save_snapshot(
            template_id,
            json.dumps(full),
            actor_id,
            summary,
        )
        await self._versions.prune_old(template_id, keep=20)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create(
        self,
        guild_id: int,
        name: str,
        created_by: int,
        description: str = None,
        category: str = None,
        content: str = None,
    ) -> tuple[int, list[str]]:
        errors = self._validate_name(name)
        if errors:
            return (0, errors)

        existing = await self._templates.get_by_name(guild_id, name)
        if existing:
            return (0, [f"A template named '{name}' already exists in this guild."])

        template_id = await self._templates.create(
            guild_id, name, created_by, description, category, content
        )

        await self._audit.log(
            guild_id,
            created_by,
            "template.create",
            target_type="template",
            target_id=template_id,
        )

        return (template_id, [])

    async def get_full(self, guild_id: int, name: str) -> dict | None:
        row = await self._templates.get_by_name(guild_id, name)
        if not row or row["guild_id"] != guild_id:
            return None
        return await self._templates.get_full(row["id"])

    async def list_templates(self, guild_id: int, category: str = None) -> list[dict]:
        return await self._templates.list_for_guild(guild_id, category)

    async def update_content(self, guild_id: int, name: str, actor_id: int, content: str | None) -> list[str]:
        row = await self._templates.get_by_name(guild_id, name)
        if not row or row["guild_id"] != guild_id:
            return [f"Template '{name}' not found."]
        await self._templates.update(row["id"], updated_by=actor_id, content=content)
        await self._audit.log(guild_id, actor_id, "template.edit", target_type="template", target_id=row["id"])
        return []

    async def update_embed(
        self,
        guild_id: int,
        name: str,
        actor_id: int,
        embed_index: int = 0,
        **kwargs,
    ) -> list[str]:
        row = await self._templates.get_by_name(guild_id, name)
        if not row or row["guild_id"] != guild_id:
            return [f"Template '{name}' not found."]

        template_id = row["id"]

        await self._snapshot(template_id, actor_id, summary="pre-edit snapshot")

        embeds = await self._embeds.get_for_template(template_id)

        if embed_index >= len(embeds):
            embed_id = await self._embeds.create(template_id, position=embed_index)
        else:
            embed_id = embeds[embed_index]["id"]

        if kwargs:
            await self._embeds.update(embed_id, **kwargs)

        await self._audit.log(
            guild_id,
            actor_id,
            "template.edit",
            target_type="template",
            target_id=template_id,
        )

        return []

    async def update_fields(
        self,
        guild_id: int,
        current_name: str,
        actor_id: int,
        **kwargs,
    ) -> list[str]:
        """Update top-level template metadata fields (description/category/
        content/name). Distinct from update_content (content-only) and
        update_embed (single-embed patch) — this is the dashboard editor's
        general-purpose "save the form" entry point. Renaming to a name that
        collides with another template in the guild is rejected.

        `current_name` (the lookup key) is intentionally a separate
        parameter from `kwargs["name"]` (the new name on a rename) — an
        earlier version used `name` for both, which meant a rename's
        `**kwargs` collided with the positional `name` arg
        (TypeError: got multiple values for argument 'name')."""
        row = await self._templates.get_by_name(guild_id, current_name)
        if not row or row["guild_id"] != guild_id:
            return [f"Template '{current_name}' not found."]

        allowed = {"name", "description", "category", "content"}
        update_kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        if not update_kwargs:
            return []

        new_name = update_kwargs.get("name")
        if new_name and new_name != current_name:
            errors = self._validate_name(new_name)
            if errors:
                return errors
            existing = await self._templates.get_by_name(guild_id, new_name)
            if existing:
                return [f"A template named '{new_name}' already exists in this guild."]

        await self._templates.update(row["id"], updated_by=actor_id, **update_kwargs)
        await self._audit.log(guild_id, actor_id, "template.edit", target_type="template", target_id=row["id"])
        return []

    async def replace_embeds(
        self,
        guild_id: int,
        name: str,
        actor_id: int,
        embeds_data: list[dict],
    ) -> list[str]:
        """Delete-and-reinsert every embed (and its fields) for a template in
        one shot — the shape the dashboard editor submits (the whole embeds
        array, since the UI edits them all in-memory and saves once).

        NOTE on atomicity: database.connection.Database.execute() commits
        after every single statement (see its docstring-free but observable
        behavior — each repo call is its own auto-committed unit), which is
        incompatible with Database.transaction()'s explicit BEGIN/COMMIT
        wrapper — nesting them causes "cannot rollback - no transaction is
        active" (the inner auto-commits close the outer transaction early).
        Confirmed by reproducing directly against a scratch DB copy while
        building this method. So this does NOT wrap the delete+reinsert in
        a DB transaction, matching the pre-existing rollback() method above
        (services/template_service.py's TemplateService.rollback also does
        a delete-then-reinsert loop with no transaction wrapper) — same
        pattern, same trade-off, not a regression introduced here. A
        mid-write failure could theoretically leave a partial embed set;
        the pre-edit snapshot taken below is the recovery path (rollback to
        the snapshot), same as it already is for update_embed/rollback.
        """
        row = await self._templates.get_by_name(guild_id, name)
        if not row or row["guild_id"] != guild_id:
            return [f"Template '{name}' not found."]

        template_id = row["id"]

        allowed_embed_keys = {
            "title", "description", "color", "url", "footer_text", "footer_icon_url",
            "thumbnail_url", "image_url", "author_name", "author_icon_url", "author_url",
            "timestamp_mode",
        }

        await self._snapshot(template_id, actor_id, summary="pre-edit snapshot")

        existing_embeds = await self._embeds.get_for_template(template_id)
        for embed in existing_embeds:
            await self._embeds.delete(embed["id"])

        for position, embed_data in enumerate(embeds_data):
            embed_kwargs = {k: v for k, v in embed_data.items() if k in allowed_embed_keys}
            new_embed_id = await self._embeds.create(template_id, position=position, **embed_kwargs)

            for field_position, field in enumerate(embed_data.get("fields", []) or []):
                if not field.get("name") or not field.get("value"):
                    continue
                await self._embeds.add_field(
                    new_embed_id,
                    name=field["name"],
                    value=field["value"],
                    inline=bool(field.get("inline", False)),
                    position=field.get("position", field_position),
                )

        await self._audit.log(
            guild_id, actor_id, "template.edit", target_type="template", target_id=template_id,
        )

        return []

    async def delete(self, guild_id: int, name: str, actor_id: int) -> bool:
        row = await self._templates.get_by_name(guild_id, name)
        if not row or row["guild_id"] != guild_id:
            return False

        await self._audit.log(
            guild_id,
            actor_id,
            "template.delete",
            target_type="template",
            target_id=row["id"],
        )

        return await self._templates.delete(guild_id, name)

    # ------------------------------------------------------------------
    # Export / import (/template export|import and the dashboard Templates
    # page; one implementation so both produce/accept the same JSON)
    # ------------------------------------------------------------------

    _IMPORT_EMBED_KEYS = {
        "title", "description", "color", "url", "footer_text", "footer_icon_url",
        "thumbnail_url", "image_url", "author_name", "author_icon_url", "author_url",
        "timestamp_mode",
    }

    async def export_template(self, guild_id: int, name: str) -> dict | None:
        """The portable JSON shape of a template (no ids), or None if missing."""
        template_data = await self.get_full(guild_id, name)
        if template_data is None:
            return None
        return {
            "name": template_data["name"],
            "description": template_data.get("description"),
            "category": template_data.get("category"),
            "content": template_data.get("content"),
            "embeds": [
                {k: v for k, v in e.items() if k not in ("id", "template_id")}
                for e in (template_data.get("embeds") or [])
            ],
        }

    async def import_template(
        self,
        guild_id: int,
        payload,
        actor_id: int,
        name_override: str | None = None,
    ) -> tuple[int, list[str], str | None]:
        """Create a NEW template from an export blob. Returns
        (template_id, errors, imported_name); never overwrites an existing
        template (same-name import fails like create)."""
        if not isinstance(payload, dict):
            return (0, ["The JSON must be an object (the output of export)."], None)
        import_name = name_override or payload.get("name")
        if not import_name or not isinstance(import_name, str):
            return (0, ["JSON must include a 'name' field."], None)

        def _text(key: str):
            value = payload.get(key)
            return value if isinstance(value, str) else None

        template_id, errors = await self.create(
            guild_id=guild_id,
            name=import_name,
            created_by=actor_id,
            description=_text("description"),
            category=_text("category"),
            content=_text("content"),
        )
        if errors:
            return (0, errors, import_name)

        embeds = payload.get("embeds") or []
        if not isinstance(embeds, list):
            embeds = []
        for embed_data in embeds[:10]:
            if not isinstance(embed_data, dict):
                continue
            embed_kwargs = {k: v for k, v in embed_data.items() if k in self._IMPORT_EMBED_KEYS}
            position = embed_data.get("position", 0)
            if not isinstance(position, int):
                position = 0
            embed_id = await self._embeds.create(template_id, position=position, **embed_kwargs)
            fields = embed_data.get("fields") or []
            for field in fields if isinstance(fields, list) else []:
                if not isinstance(field, dict):
                    continue
                pos = field.get("position")
                await self._embeds.add_field(
                    embed_id,
                    name=str(field.get("name", "")),
                    value=str(field.get("value", "")),
                    inline=bool(field.get("inline", False)),
                    position=pos if isinstance(pos, int) else None,
                )
        return (template_id, [], import_name)

    async def get_version_history(
        self, guild_id: int, name: str, limit: int = 20
    ) -> list[dict]:
        row = await self._templates.get_by_name(guild_id, name)
        if not row or row["guild_id"] != guild_id:
            return []
        return await self._versions.list_versions(row["id"], limit)

    async def rollback(
        self,
        guild_id: int,
        name: str,
        version_number: int,
        actor_id: int,
    ) -> list[str]:
        row = await self._templates.get_by_name(guild_id, name)
        if not row or row["guild_id"] != guild_id:
            return [f"Template '{name}' not found."]

        template_id = row["id"]

        version = await self._versions.get_version(template_id, version_number)
        if not version:
            return [f"Version {version_number} not found for template '{name}'."]

        snapshot = json.loads(version["snapshot_json"])

        # Restore top-level fields
        top_level_fields = {}
        for field in ("content", "description", "category"):
            if field in snapshot:
                top_level_fields[field] = snapshot[field]
        if top_level_fields:
            await self._templates.update(template_id, updated_by=actor_id, **top_level_fields)

        # Replace all embeds
        existing_embeds = await self._embeds.get_for_template(template_id)
        for embed in existing_embeds:
            await self._embeds.delete(embed["id"])

        for embed_data in snapshot.get("embeds", []):
            allowed_embed_keys = {
                "title", "description", "color", "url", "footer_text", "footer_icon_url",
                "thumbnail_url", "image_url", "author_name", "author_icon_url", "author_url",
                "timestamp_mode",
            }
            embed_kwargs = {k: v for k, v in embed_data.items() if k in allowed_embed_keys}
            position = embed_data.get("position", 0)
            new_embed_id = await self._embeds.create(template_id, position=position, **embed_kwargs)

            for field in embed_data.get("fields", []):
                await self._embeds.add_field(
                    new_embed_id,
                    name=field["name"],
                    value=field["value"],
                    inline=bool(field.get("inline", False)),
                    position=field.get("position"),
                )

        await self._snapshot(template_id, actor_id, summary=f"rollback to v{version_number}")

        await self._audit.log(
            guild_id,
            actor_id,
            "template.rollback",
            target_type="template",
            target_id=template_id,
            detail={"rolled_back_to": version_number},
        )

        return []
