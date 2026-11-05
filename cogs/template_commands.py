"""
cogs/template_commands.py — Discord slash-command interface for the template system.

Provides the ``/template`` command group with sub-commands:
    create, edit, list, delete, preview, history, rollback
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from core.ui.paginator import Paginator
from core.ui.confirm import ConfirmView
from core.ui.embed_editor import EmbedEditorView


class TemplateCog(commands.Cog, name="Templates"):
    """Slash commands for managing message templates."""

    def __init__(
        self,
        bot: commands.Bot,
        template_service,
        permission_service,
        renderer,
        managed_service=None,
    ):
        self.bot = bot
        self.template_service = template_service
        self.permission_service = permission_service
        self.renderer = renderer
        self.managed_service = managed_service

    # ------------------------------------------------------------------
    # Command group
    # ------------------------------------------------------------------

    template = app_commands.Group(
        name="template",
        description="Manage message templates.",
    )

    # ------------------------------------------------------------------
    # /template create
    # ------------------------------------------------------------------

    @template.command(name="create", description="Create a new template.")
    @app_commands.describe(
        name="Unique name for the template (letters, digits, hyphens, underscores).",
        description="Optional description of what this template is for.",
    )
    async def template_create(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str = None,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to manage templates.", ephemeral=True
            )
            return

        template_id, errors = await self.template_service.create(
            guild_id=interaction.guild_id,
            name=name,
            created_by=interaction.user.id,
            description=description,
        )

        if errors:
            await interaction.response.send_message(
                "Could not create template:\n" + "\n".join(f"• {e}" for e in errors),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Template **{name}** created. Use `/template edit {name}` to add content.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /template edit
    # ------------------------------------------------------------------

    @template.command(name="edit", description="Open the embed editor for a template.")
    @app_commands.describe(name="Name of the template to edit.")
    async def template_edit(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to manage templates.", ephemeral=True
            )
            return

        template_data = await self.template_service.get_full(
            interaction.guild_id, name
        )
        if template_data is None:
            await interaction.response.send_message(
                f"No template named **{name}**.", ephemeral=True
            )
            return

        view = EmbedEditorView(
            template_data=template_data,
            template_service=self.template_service,
            renderer=self.renderer,
            guild=interaction.guild,
            actor_id=interaction.user.id,
        )

        from core.ui.embed_editor import _build_status_embed
        status_embed = _build_status_embed(template_data, view._embed_index)

        await interaction.response.send_message(
            embed=status_embed,
            view=view,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /template list
    # ------------------------------------------------------------------

    @template.command(name="list", description="List all templates in this server.")
    async def template_list(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to view templates.", ephemeral=True
            )
            return

        templates = await self.template_service.list_templates(interaction.guild_id)

        if not templates:
            await interaction.response.send_message(
                "No templates created yet.", ephemeral=True
            )
            return

        def formatter(slice_: list, page: int, max_page: int) -> discord.Embed:
            embed = discord.Embed(
                title="Templates",
                color=discord.Color.blurple(),
                description=f"Page {page + 1} of {max_page + 1}",
            )
            for row in slice_:
                t_name = row.get("name", "unknown")
                category = row.get("category") or "—"
                updated_at = row.get("updated_at") or row.get("created_at") or "—"
                embed.add_field(
                    name=f"**{t_name}**",
                    value=f"Category: {category} | Updated: {updated_at}",
                    inline=False,
                )
            return embed

        paginator = Paginator(items=templates, page_size=10, formatter=formatter)
        await interaction.response.send_message(
            embed=paginator.render_embed(),
            view=paginator,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /template delete
    # ------------------------------------------------------------------

    @template.command(name="delete", description="Delete a template.")
    @app_commands.describe(name="Name of the template to delete.")
    async def template_delete(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to manage templates.", ephemeral=True
            )
            return

        view = ConfirmView(timeout=30)
        await interaction.response.send_message(
            f"Are you sure you want to delete **{name}**? This cannot be undone.",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if not view.confirmed:
            await interaction.edit_original_response(
                content="Deletion cancelled.", view=None
            )
            return

        deleted = await self.template_service.delete(
            guild_id=interaction.guild_id,
            name=name,
            actor_id=interaction.user.id,
        )

        if deleted:
            await interaction.edit_original_response(
                content=f"Deleted **{name}**.", view=None
            )
        else:
            await interaction.edit_original_response(
                content=f"No template named **{name}** was found.", view=None
            )

    # ------------------------------------------------------------------
    # /template preview
    # ------------------------------------------------------------------

    @template.command(name="preview", description="Preview a rendered template.")
    @app_commands.describe(name="Name of the template to preview.")
    async def template_preview(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to preview templates.", ephemeral=True
            )
            return

        template_data = await self.template_service.get_full(
            interaction.guild_id, name
        )
        if template_data is None:
            await interaction.response.send_message(
                f"No template named **{name}**.", ephemeral=True
            )
            return

        from core.placeholder_engine import sample_context

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        ctx = sample_context(interaction.guild, member, interaction.channel)
        try:
            rendered = self.renderer.render_message(template_data, ctx)
        except Exception as exc:
            await interaction.response.send_message(
                f"Render error: {exc}", ephemeral=True
            )
            return

        content = rendered.get("content")
        embeds = rendered.get("embeds") or []

        if not content and not embeds:
            await interaction.response.send_message(
                f"Template **{name}** renders to nothing. Add some content first.",
                ephemeral=True,
            )
            return

        note = (
            "-# Preview: member placeholders show **you**, moderation ones show example values "
            "(reason, duration, case number). `/template placeholders` lists them all."
        )
        await interaction.response.send_message(
            content=(content + "\n" + note) if content else note,
            embeds=embeds[:10],
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /template placeholders
    # ------------------------------------------------------------------

    @template.command(name="placeholders", description="List every placeholder you can use in templates.")
    @app_commands.describe(category="Which group to show (leave empty for an overview).")
    @app_commands.choices(category=[
        app_commands.Choice(name=c, value=c) for c in (
            "Member", "Server", "Channel", "Date & time", "Moderation", "Staff", "Tickets", "Roles",
            "Lookups & extras",
        )
    ])
    async def template_placeholders(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to manage templates.", ephemeral=True
            )
            return
        from core.placeholder_engine import CATEGORIES, catalogue

        entries = catalogue()
        if category is None:
            counts = {c: sum(1 for e in entries if e["category"] == c) for c in CATEGORIES}
            embed = discord.Embed(
                title="Template placeholders",
                color=0xF2B6CD,
                description=(
                    "Write a placeholder in any template text and the bot fills it in when the message is sent.\n\n"
                    "**`{name}`** plain, e.g. `{mention}`, `{server}`, `{membercount}`\n"
                    "**`{name:something}`** with a value, e.g. `{channel:general}`, `{role:Moderator}`, "
                    "`{joindate:R}`, `{random:Hi;Hello;Hey}`\n"
                    "**`{name|backup}`** if it's empty use the backup: `{nickname|user}`, "
                    "`{reason|No reason given}`\n\n"
                    + "\n".join(f"**{c}**: {n}" for c, n in counts.items())
                    + "\n\nPick a category with `/template placeholders category:...`, or see them all with "
                    "examples on the dashboard (Templates, open a template, Placeholders)."
                ),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        lines = [
            f"`{e['syntax']}` {e['description']}"
            for e in entries if e["category"] == category.value
        ]
        where = sorted({e["where"] for e in entries if e["category"] == category.value})
        embeds, chunk = [], ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 3900:
                embeds.append(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            embeds.append(chunk)
        out = [
            discord.Embed(title=f"Placeholders: {category.value}" if i == 0 else None, description=text, color=0xF2B6CD)
            for i, text in enumerate(embeds)
        ]
        out[-1].set_footer(text="Works in: " + "; ".join(where))
        await interaction.response.send_message(embeds=out[:10], ephemeral=True)

    # ------------------------------------------------------------------
    # /template history
    # ------------------------------------------------------------------

    @template.command(name="history", description="View version history for a template.")
    @app_commands.describe(name="Name of the template.")
    async def template_history(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to view template history.", ephemeral=True
            )
            return

        versions = await self.template_service.get_version_history(
            interaction.guild_id, name
        )

        if not versions:
            await interaction.response.send_message(
                f"No version history found for **{name}**. "
                "The template may not exist or has never been edited.",
                ephemeral=True,
            )
            return

        def formatter(slice_: list, page: int, max_page: int) -> discord.Embed:
            embed = discord.Embed(
                title=f"Version History: {name}",
                color=discord.Color.og_blurple(),
                description=f"Page {page + 1} of {max_page + 1}",
            )
            for v in slice_:
                version_num = v.get("version_number", "?")
                changed_by = v.get("changed_by") or v.get("actor_id") or "unknown"
                created_at = v.get("created_at", "—")
                summary = v.get("change_summary") or v.get("summary") or "—"
                embed.add_field(
                    name=f"v{version_num}",
                    value=(
                        f"Changed by: <@{changed_by}>\n"
                        f"Date: {created_at}\n"
                        f"Summary: {summary}"
                    ),
                    inline=False,
                )
            return embed

        paginator = Paginator(items=versions, page_size=10, formatter=formatter)
        await interaction.response.send_message(
            embed=paginator.render_embed(),
            view=paginator,
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /template post
    # ------------------------------------------------------------------

    @template.command(name="post", description="Post a template as a tracked managed message.")
    @app_commands.describe(
        name="Name of the template to post.",
        channel="Channel to post in.",
    )
    async def template_post(
        self,
        interaction: discord.Interaction,
        name: str,
        channel: discord.TextChannel,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to post managed messages.", ephemeral=True
            )
            return

        if self.managed_service is None:
            await interaction.response.send_message(
                "Managed messages are not available yet.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        message_id, errors = await self.managed_service.post(
            guild=interaction.guild,
            channel=channel,
            template_name=name,
            actor_id=interaction.user.id,
        )

        if errors:
            await interaction.followup.send(
                "Failed to post:\n" + "\n".join(f"• {e}" for e in errors),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Posted template **{name}** to {channel.mention}. "
            f"It will auto-update whenever the template is edited. "
            f"(message ID: `{message_id}`)",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /template rollback
    # ------------------------------------------------------------------

    @template.command(name="rollback", description="Roll back a template to an earlier version.")
    @app_commands.describe(
        name="Name of the template.",
        version="Version number to roll back to.",
    )
    async def template_rollback(
        self,
        interaction: discord.Interaction,
        name: str,
        version: int,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to manage templates.", ephemeral=True
            )
            return

        view = ConfirmView(timeout=30)
        await interaction.response.send_message(
            f"Roll back **{name}** to version {version}?",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if not view.confirmed:
            await interaction.edit_original_response(
                content="Rollback cancelled.", view=None
            )
            return

        errors = await self.template_service.rollback(
            guild_id=interaction.guild_id,
            name=name,
            version_number=version,
            actor_id=interaction.user.id,
        )

        if errors:
            await interaction.edit_original_response(
                content="Rollback failed:\n" + "\n".join(f"• {e}" for e in errors),
                view=None,
            )
        else:
            await interaction.edit_original_response(
                content=f"**{name}** rolled back to version {version}.",
                view=None,
            )
            # Trigger auto-sync for any managed messages using this template
            row = await self.template_service._templates.get_by_name(interaction.guild_id, name)
            sync_queue = getattr(self.bot, "sync_queue", None)
            if row and sync_queue is not None:
                await sync_queue.enqueue(row["id"])

    # ------------------------------------------------------------------
    # /template export
    # ------------------------------------------------------------------

    @template.command(name="export", description="Export a template as a JSON code block.")
    @app_commands.describe(name="Name of the template to export.")
    async def template_export(
        self,
        interaction: discord.Interaction,
        name: str,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to export templates.", ephemeral=True
            )
            return

        export = await self.template_service.export_template(interaction.guild_id, name)
        if export is None:
            await interaction.response.send_message(f"No template named **{name}**.", ephemeral=True)
            return

        import json as _json
        blob = _json.dumps(export, indent=2, ensure_ascii=False)

        if len(blob) > 1900:
            # Send as a file attachment
            import io
            file = discord.File(io.BytesIO(blob.encode()), filename=f"{name}.json")
            await interaction.response.send_message(
                f"Template **{name}** exported:", file=file, ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Template **{name}** exported:\n```json\n{blob}\n```",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /template import
    # ------------------------------------------------------------------

    @template.command(name="import", description="Import a template from JSON. Paste the JSON blob as the data argument.")
    @app_commands.describe(
        data="The JSON export blob (from /template export).",
        name_override="Optional: give the imported template a different name.",
    )
    async def template_import(
        self,
        interaction: discord.Interaction,
        data: str,
        name_override: str = None,
    ) -> None:
        if not await self.permission_service.has_permission(interaction.user, "manage_templates"):
            await interaction.response.send_message(
                "You don't have permission to import templates.", ephemeral=True
            )
            return

        import json as _json
        try:
            payload = _json.loads(data)
        except _json.JSONDecodeError as exc:
            await interaction.response.send_message(
                f"Invalid JSON: {exc}", ephemeral=True
            )
            return

        if not isinstance(payload, dict) or not (name_override or payload.get("name")):
            await interaction.response.send_message("JSON must include a 'name' field.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Shared with the dashboard Templates page (TemplateService.import_template).
        template_id, errors, import_name = await self.template_service.import_template(
            interaction.guild_id, payload, interaction.user.id, name_override=name_override,
        )
        if errors:
            await interaction.followup.send(
                "Import failed:\n" + "\n".join(f"• {e}" for e in errors), ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Template **{import_name}** imported successfully. Use `/template edit {import_name}` to review it.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Cog setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        import logging
        logging.getLogger(__name__).warning(
            "bot.db is not available — template_commands cog not loaded."
        )
        return

    from database.repositories.template_repo import TemplateRepository
    from database.repositories.embed_repo import EmbedRepository
    from database.repositories.version_repo import VersionRepository
    from database.repositories.audit_repo import AuditRepository
    from services.template_service import TemplateService
    from core.renderer import TemplateRenderer

    template_repo = TemplateRepository(bot.db)
    embed_repo = EmbedRepository(bot.db)
    version_repo = VersionRepository(bot.db)
    audit_repo = AuditRepository(bot.db)

    svc = TemplateService(template_repo, embed_repo, version_repo, audit_repo)
    perm = bot.permission_service  # type: ignore[attr-defined]
    renderer = TemplateRenderer()

    await bot.add_cog(TemplateCog(bot, svc, perm, renderer))
