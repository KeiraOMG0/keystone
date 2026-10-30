from __future__ import annotations

import logging

import discord

from core.placeholder_engine import RenderContext
from core.renderer import TemplateRenderer
from database.repositories.welcome_repo import WelcomeRepository
from services.live_events import publish as live_publish
from services.template_service import TemplateService

log = logging.getLogger(__name__)

WELCOME_COLOR = 0xF2B6CD
WELCOME_TEMPLATE_NAME = "welcome"


class WelcomeService:
    def __init__(
        self,
        repo: WelcomeRepository,
        template_service: TemplateService | None = None,
        renderer: TemplateRenderer | None = None,
    ) -> None:
        self._repo = repo
        self._templates = template_service
        self._renderer = renderer or TemplateRenderer()

    def build_embed(self, member: discord.Member, guild: discord.Guild) -> discord.Embed:
        """Hardcoded fallback embed, used when no 'welcome' template is configured."""
        embed = discord.Embed(
            description=f"Welcome to **{guild.name}**, {member.mention}! Glad you're here.",
            color=WELCOME_COLOR,
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"{guild.member_count} members")
        return embed

    async def send_welcome(self, channel: discord.TextChannel, member: discord.Member) -> discord.Message:
        content = f"Welcome, {member.mention}!"
        embeds: list[discord.Embed] = [self.build_embed(member, member.guild)]

        if self._templates is not None:
            try:
                template_full = await self._templates.get_full(member.guild.id, WELCOME_TEMPLATE_NAME)
            except Exception:
                log.exception("welcome: failed to look up '%s' template", WELCOME_TEMPLATE_NAME)
                template_full = None

            if template_full is not None:
                ctx = RenderContext(guild=member.guild, member=member, channel=channel)
                try:
                    rendered = self._renderer.render_message(template_full, ctx)
                    rendered_content = rendered.get("content") or None
                    rendered_embeds = rendered.get("embeds") or []
                    if rendered_content or rendered_embeds:
                        content = rendered_content
                        embeds = rendered_embeds
                except Exception:
                    log.exception("welcome: failed to render '%s' template — using fallback", WELCOME_TEMPLATE_NAME)

        return await channel.send(content=content, embeds=embeds[:10] if embeds else [])

    async def get_config(self, guild_id: int) -> dict | None:
        return await self._repo.get_config(guild_id)

    # Live `automation` topic (dashboard Automation > Welcome & Boost); publish never raises.
    async def set_channel(self, guild_id: int, channel_id: int) -> None:
        """Setting a channel also turns the messages on (same for /welcome setchannel and the dashboard)."""
        await self._repo.upsert_config(guild_id, channel_id=channel_id, enabled=True)
        live_publish("automation", {"section": "welcome", "action": "setchannel"})

    async def set_enabled(self, guild_id: int, enabled: bool) -> None:
        await self._repo.upsert_config(guild_id, enabled=enabled)
        live_publish("automation", {"section": "welcome", "action": "toggle"})
