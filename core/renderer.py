from __future__ import annotations
import discord
from core.placeholder_engine import resolve_text, RenderContext
from core.validator import TemplateValidator


# Discord's limits. Placeholders can make text longer than what was saved
# (e.g. {roles}), so rendered text is clipped instead of failing the send.
LIMITS = {"content": 2000, "title": 256, "description": 4096, "field_name": 256, "field_value": 1024,
          "footer": 2048, "author": 256}


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return text
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TemplateRenderer:
    def __init__(self) -> None:
        self._validator = TemplateValidator()

    def render_message(self, template_data: dict, ctx: RenderContext) -> dict:
        content = _clip(resolve_text(template_data.get("content"), ctx), LIMITS["content"]) or None
        embeds_data = template_data.get("embeds", [])
        embeds = [
            self._render_embed(e, ctx)
            for e in sorted(embeds_data, key=lambda x: x.get("position", 0))
        ]
        return {
            "content": content,
            "embeds": [e for e in embeds if e is not None],
        }

    def _render_embed(self, embed_data: dict, ctx: RenderContext) -> discord.Embed | None:
        r = lambda t: resolve_text(t, ctx)
        v = self._validator.validate_url

        title = _clip(r(embed_data.get("title")), LIMITS["title"]) or None
        description = _clip(r(embed_data.get("description")), LIMITS["description"]) or None
        color_raw = embed_data.get("color")
        color = discord.Color(int(color_raw)) if color_raw is not None else discord.Color.default()

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
        )

        thumbnail_url = v(r(embed_data.get("thumbnail_url")))
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        image_url = v(r(embed_data.get("image_url")))
        if image_url:
            embed.set_image(url=image_url)

        # Support both flat DB keys (author_name) and nested dict (author.name)
        _author = embed_data.get("author") or {}
        author_name = _clip(r(embed_data.get("author_name") or _author.get("name")), LIMITS["author"])
        if author_name:
            kwargs = {"name": author_name}
            icon = v(r(embed_data.get("author_icon_url") or _author.get("icon_url")))
            url = v(r(embed_data.get("author_url") or _author.get("url")))
            if icon:
                kwargs["icon_url"] = icon
            if url:
                kwargs["url"] = url
            embed.set_author(**kwargs)

        _footer = embed_data.get("footer") or {}
        footer_text = _clip(r(embed_data.get("footer_text") or _footer.get("text")), LIMITS["footer"])
        if footer_text:
            kwargs = {"text": footer_text}
            icon = v(r(embed_data.get("footer_icon_url") or _footer.get("icon_url")))
            if icon:
                kwargs["icon_url"] = icon
            embed.set_footer(**kwargs)

        for field in embed_data.get("fields") or []:
            name = _clip(r(field.get("name")), LIMITS["field_name"])
            value = _clip(r(field.get("value")), LIMITS["field_value"])
            inline = bool(field.get("inline", False))
            if name and value:
                embed.add_field(name=name, value=value, inline=inline)

        timestamp_mode = embed_data.get("timestamp_mode")
        if timestamp_mode == "now":
            embed.timestamp = discord.utils.utcnow()

        has_content = bool(embed.title or embed.description or embed.fields)
        if not has_content:
            return None

        return embed
