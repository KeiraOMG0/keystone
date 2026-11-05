"""
core/ui/embed_editor.py — Interactive embed editor View and its associated Modals.

Opened when a staff member runs ``/template edit [name]``.  The editor shows a
compact status-card embed for the template being edited and provides buttons
that each open a Modal to modify one section.  Every Modal re-fetches the
template after saving so the status card stays in sync.
"""
from __future__ import annotations

import discord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_embed_data(template_data: dict, index: int) -> dict:
    """Return the embed dict at *index*, or an empty dict if it doesn't exist."""
    embeds = template_data.get("embeds") or []
    if index < len(embeds):
        return embeds[index]
    return {}


def _truncate(text: str | None, max_len: int = 100) -> str:
    if not text:
        return "*not set*"
    return text if len(text) <= max_len else text[:max_len] + "…"


def _build_status_embed(template_data: dict, embed_index: int) -> discord.Embed:
    """Return a compact status-card embed summarising the current template state."""
    name = template_data.get("name", "unknown")
    embed_data = _get_embed_data(template_data, embed_index)

    embed = discord.Embed(
        title=f"Editing: {name}",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="Title",
        value=_truncate(embed_data.get("title"), 100),
        inline=True,
    )
    embed.add_field(
        name="Description",
        value=_truncate(embed_data.get("description"), 100),
        inline=True,
    )

    raw_color = embed_data.get("color")
    color_display = f"#{raw_color:06X}" if isinstance(raw_color, int) else (raw_color or "*not set*")
    embed.add_field(name="Color", value=color_display, inline=True)

    footer_text = embed_data.get("footer_text") or (
        (embed_data.get("footer") or {}).get("text")
    )
    embed.add_field(
        name="Footer",
        value=_truncate(footer_text, 80),
        inline=True,
    )

    has_image = bool(embed_data.get("image_url"))
    has_thumb = bool(embed_data.get("thumbnail_url"))
    embed.add_field(
        name="Images",
        value=(
            f"Main: {'set' if has_image else 'not set'} | "
            f"Thumb: {'set' if has_thumb else 'not set'}"
        ),
        inline=True,
    )

    author_name = embed_data.get("author_name") or (
        (embed_data.get("author") or {}).get("name")
    )
    embed.add_field(
        name="Author",
        value=_truncate(author_name, 80),
        inline=True,
    )

    fields = embed_data.get("fields") or []
    embed.add_field(
        name="Fields",
        value=str(len(fields)) if fields else "0",
        inline=True,
    )

    msg_content = template_data.get("content")
    embed.add_field(
        name="Message Content",
        value=_truncate(msg_content, 80),
        inline=True,
    )

    embed.set_footer(text=f"Embed index: {embed_index} | Timeout: 5 minutes")
    return embed


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class ContentModal(discord.ui.Modal, title="Edit Content"):
    msg_content = discord.ui.TextInput(
        label="Message Content",
        required=False,
        max_length=2000,
        style=discord.TextStyle.paragraph,
    )
    embed_title = discord.ui.TextInput(
        label="Embed Title",
        required=False,
        max_length=256,
        placeholder="Supports {placeholders}",
    )
    description = discord.ui.TextInput(
        label="Description",
        required=False,
        max_length=4000,
        style=discord.TextStyle.paragraph,
        placeholder="Supports {placeholders}, markdown",
    )
    embed_url = discord.ui.TextInput(
        label="Title URL",
        required=False,
        max_length=500,
    )

    def __init__(self, view: "EmbedEditorView"):
        super().__init__()
        self._editor = view
        # Pre-populate
        template_data = view.template_data
        embed_data = _get_embed_data(template_data, view._embed_index)
        self.msg_content.default = template_data.get("content") or ""
        self.embed_title.default = embed_data.get("title") or ""
        self.description.default = embed_data.get("description") or ""
        self.embed_url.default = embed_data.get("url") or ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        svc = self._editor.template_service
        name = self._editor.template_data["name"]
        guild_id = self._editor.guild.id
        actor_id = self._editor.actor_id
        idx = self._editor._embed_index

        errors = await svc.update_embed(
            guild_id,
            name,
            actor_id,
            embed_index=idx,
            title=self.embed_title.value or None,
            description=self.description.value or None,
            url=self.embed_url.value or None,
        )

        if not errors:
            new_content = self.msg_content.value or None
            await svc.update_content(guild_id, name, actor_id, new_content)

        await self._editor._refresh_view(interaction)


class FooterModal(discord.ui.Modal, title="Edit Footer"):
    footer_text = discord.ui.TextInput(
        label="Footer Text",
        required=False,
        max_length=2048,
    )
    footer_icon = discord.ui.TextInput(
        label="Footer Icon URL",
        required=False,
    )

    def __init__(self, view: "EmbedEditorView"):
        super().__init__()
        self._editor = view
        embed_data = _get_embed_data(view.template_data, view._embed_index)
        self.footer_text.default = embed_data.get("footer_text") or (
            (embed_data.get("footer") or {}).get("text") or ""
        )
        self.footer_icon.default = embed_data.get("footer_icon_url") or (
            (embed_data.get("footer") or {}).get("icon_url") or ""
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        svc = self._editor.template_service
        errors = await svc.update_embed(
            self._editor.guild.id,
            self._editor.template_data["name"],
            self._editor.actor_id,
            embed_index=self._editor._embed_index,
            footer_text=self.footer_text.value or None,
            footer_icon_url=self.footer_icon.value or None,
        )
        await self._editor._refresh_view(interaction)


class AuthorModal(discord.ui.Modal, title="Edit Author"):
    author_name = discord.ui.TextInput(
        label="Author Name",
        required=False,
        max_length=256,
    )
    author_icon = discord.ui.TextInput(
        label="Author Icon URL",
        required=False,
        placeholder="Use 'avatar' for member avatar",
    )
    author_url = discord.ui.TextInput(
        label="Author URL",
        required=False,
    )

    def __init__(self, view: "EmbedEditorView"):
        super().__init__()
        self._editor = view
        embed_data = _get_embed_data(view.template_data, view._embed_index)
        self.author_name.default = embed_data.get("author_name") or (
            (embed_data.get("author") or {}).get("name") or ""
        )
        self.author_icon.default = embed_data.get("author_icon_url") or (
            (embed_data.get("author") or {}).get("icon_url") or ""
        )
        self.author_url.default = embed_data.get("author_url") or (
            (embed_data.get("author") or {}).get("url") or ""
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        svc = self._editor.template_service
        errors = await svc.update_embed(
            self._editor.guild.id,
            self._editor.template_data["name"],
            self._editor.actor_id,
            embed_index=self._editor._embed_index,
            author_name=self.author_name.value or None,
            author_icon_url=self.author_icon.value or None,
            author_url=self.author_url.value or None,
        )
        await self._editor._refresh_view(interaction)


class ColorModal(discord.ui.Modal, title="Edit Color"):
    color = discord.ui.TextInput(
        label="Color (hex)",
        required=False,
        max_length=7,
        placeholder="#FFB3C1",
    )

    def __init__(self, view: "EmbedEditorView"):
        super().__init__()
        self._editor = view
        embed_data = _get_embed_data(view.template_data, view._embed_index)
        raw = embed_data.get("color")
        self.color.default = f"#{raw:06X}" if isinstance(raw, int) else (raw or "")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        svc = self._editor.template_service
        raw = self.color.value.strip().lstrip("#")
        color_int: int | None = None
        if raw:
            try:
                color_int = int(raw, 16)
            except ValueError:
                await interaction.response.send_message(
                    "Invalid hex color. Use a format like `#FFB3C1`.",
                    ephemeral=True,
                )
                return

        errors = await svc.update_embed(
            self._editor.guild.id,
            self._editor.template_data["name"],
            self._editor.actor_id,
            embed_index=self._editor._embed_index,
            color=color_int,
        )
        await self._editor._refresh_view(interaction)


class ImageModal(discord.ui.Modal, title="Images"):
    image_url = discord.ui.TextInput(
        label="Main Image URL",
        required=False,
    )
    thumbnail_url = discord.ui.TextInput(
        label="Thumbnail URL",
        required=False,
    )

    def __init__(self, view: "EmbedEditorView"):
        super().__init__()
        self._editor = view
        embed_data = _get_embed_data(view.template_data, view._embed_index)
        self.image_url.default = embed_data.get("image_url") or ""
        self.thumbnail_url.default = embed_data.get("thumbnail_url") or ""

    async def on_submit(self, interaction: discord.Interaction) -> None:
        svc = self._editor.template_service
        errors = await svc.update_embed(
            self._editor.guild.id,
            self._editor.template_data["name"],
            self._editor.actor_id,
            embed_index=self._editor._embed_index,
            image_url=self.image_url.value or None,
            thumbnail_url=self.thumbnail_url.value or None,
        )
        await self._editor._refresh_view(interaction)


class FieldModal(discord.ui.Modal, title="Add Field"):
    field_name = discord.ui.TextInput(
        label="Field Name",
        max_length=256,
    )
    field_value = discord.ui.TextInput(
        label="Field Value",
        max_length=1024,
        style=discord.TextStyle.paragraph,
    )
    inline = discord.ui.TextInput(
        label="Inline? (yes/no)",
        max_length=3,
        default="no",
        required=False,
    )

    def __init__(self, view: "EmbedEditorView"):
        super().__init__()
        self._editor = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        svc = self._editor.template_service
        template_data = self._editor.template_data
        guild_id = self._editor.guild.id
        actor_id = self._editor.actor_id
        idx = self._editor._embed_index

        # Ensure the embed row exists first
        errors = await svc.update_embed(
            guild_id,
            template_data["name"],
            actor_id,
            embed_index=idx,
        )
        if errors:
            await interaction.response.send_message(
                "Error: " + "\n".join(errors), ephemeral=True
            )
            return

        # Resolve embed id and add field via the embed repo
        is_inline = self.inline.value.strip().lower() in ("yes", "y", "true", "1")
        try:
            full = await svc.get_full(guild_id, template_data["name"])
            embeds = full.get("embeds") or [] if full else []
            if idx < len(embeds):
                embed_id = embeds[idx]["id"]
                await svc._embeds.add_field(
                    embed_id,
                    name=self.field_name.value,
                    value=self.field_value.value,
                    inline=is_inline,
                )
        except Exception:
            pass  # graceful fallback — field may not persist but won't crash

        await self._editor._refresh_view(interaction)


# ---------------------------------------------------------------------------
# Main editor view
# ---------------------------------------------------------------------------

class EmbedEditorView(discord.ui.View):
    """
    The primary embed-editor View sent (ephemerally) to a staff member who
    runs ``/template edit [name]``.

    Parameters
    ----------
    template_data:
        Full template dict as returned by ``TemplateService.get_full()``.
    template_service:
        The ``TemplateService`` instance used to persist changes.
    renderer:
        The ``TemplateRenderer`` instance used to render previews.
    guild:
        The ``discord.Guild`` the template belongs to.
    actor_id:
        Discord user ID of the staff member performing the edit.
    """

    def __init__(
        self,
        template_data: dict,
        template_service,
        renderer,
        guild: discord.Guild,
        actor_id: int,
    ):
        super().__init__(timeout=300)
        self.template_data = template_data
        self.template_service = template_service
        self.renderer = renderer
        self.guild = guild
        self.actor_id = actor_id
        self._embed_index: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _refresh_view(self, interaction: discord.Interaction) -> None:
        """Re-fetch template data and edit the interaction message with a fresh status card."""
        fresh = await self.template_service.get_full(
            self.guild.id, self.template_data["name"]
        )
        if fresh:
            self.template_data = fresh
            # Trigger auto-sync of any managed messages that use this template
            sync_queue = getattr(interaction.client, "sync_queue", None)
            if sync_queue is not None:
                await sync_queue.enqueue(fresh["id"])

        status_embed = _build_status_embed(self.template_data, self._embed_index)

        if not interaction.response.is_done():
            await interaction.response.edit_message(embed=status_embed, view=self)
        else:
            await interaction.edit_original_response(embed=status_embed, view=self)

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    @discord.ui.button(label="Edit Content", style=discord.ButtonStyle.primary, row=0)
    async def btn_content(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(ContentModal(self))

    @discord.ui.button(label="Footer", style=discord.ButtonStyle.secondary, row=0)
    async def btn_footer(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(FooterModal(self))

    @discord.ui.button(label="Author", style=discord.ButtonStyle.secondary, row=0)
    async def btn_author(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(AuthorModal(self))

    @discord.ui.button(label="Color", style=discord.ButtonStyle.secondary, row=1)
    async def btn_color(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(ColorModal(self))

    @discord.ui.button(label="Image/Thumb", style=discord.ButtonStyle.secondary, row=1)
    async def btn_image(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(ImageModal(self))

    @discord.ui.button(label="Add Field", style=discord.ButtonStyle.secondary, row=1)
    async def btn_field(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(FieldModal(self))

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.success, row=2)
    async def btn_preview(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        from core.placeholder_engine import sample_context

        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        ctx = sample_context(self.guild, member, interaction.channel)
        try:
            rendered = self.renderer.render_message(self.template_data, ctx)
        except Exception as exc:
            await interaction.response.send_message(
                f"Render error: {exc}", ephemeral=True
            )
            return

        content = rendered.get("content")
        embeds = rendered.get("embeds") or []

        if not content and not embeds:
            await interaction.response.send_message(
                "The template renders to nothing. Add some content first.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            content=content or "",
            embeds=embeds[:10],  # Discord limit
            ephemeral=True,
        )

    @discord.ui.button(label="Save & Close", style=discord.ButtonStyle.danger, row=2)
    async def btn_save(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[union-attr]
        await interaction.response.edit_message(content="Saved.", embed=None, view=self)
        self.stop()
