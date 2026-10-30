from __future__ import annotations

import logging

import discord

from core.component_builder import build_view
from core.placeholder_engine import RenderContext
from core.renderer import TemplateRenderer
from database.repositories.managed_repo import ManagedMessageRepository
from database.repositories.template_repo import TemplateRepository
from services.live_events import publish as live_publish

log = logging.getLogger(__name__)


def _publish(action: str, **data) -> None:
    """Live `automation` topic (dashboard Automation > Managed messages). Never raises."""
    live_publish("automation", {"section": "managed", "action": action, **data})

# Template posts/edits never ping @everyone/@here (a template is text anyone
# with manage_templates can write, and there is no per-template "may ping
# everyone" flag). Member and role mentions written in a template still ping.
TEMPLATE_ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=True, users=True)


class ManagedMessageService:
    def __init__(
        self,
        managed_repo: ManagedMessageRepository,
        template_repo: TemplateRepository,
        renderer: TemplateRenderer,
        bot: discord.Client,
        audit=None,
    ) -> None:
        self._managed = managed_repo
        self._templates = template_repo
        self._renderer = renderer
        self._bot = bot
        # services.audit_service.AuditService | None: untrack/sync audit rows
        # (shared by /managed and the dashboard Automation page).
        self._audit = audit

    async def _audit_log(self, guild_id: int, actor_id: int | None, action: str, target_type: str, target_id, detail: dict) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.log(guild_id, actor_id or 0, action, target_type, target_id, detail)
        except Exception:  # the change already happened; never fail it over the audit row
            log.exception("managed: could not write %s audit row", action)

    # ------------------------------------------------------------------
    # Post a managed message
    # ------------------------------------------------------------------

    async def post(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        template_name: str,
        actor_id: int,
    ) -> tuple[int | None, list[str]]:
        template_data = await self._templates.get_by_name(guild.id, template_name)
        if not template_data:
            return (None, [f"No template named '{template_name}' found."])

        template_full = await self._templates.get_full(template_data["id"])
        ctx = RenderContext(guild=guild, member=None, channel=channel)

        try:
            rendered = self._renderer.render_message(template_full, ctx)
        except Exception as exc:
            return (None, [f"Render error: {exc}"])

        content = rendered.get("content") or None
        embeds = rendered.get("embeds") or []

        if not content and not embeds:
            return (None, ["Template renders to nothing. Add content or embeds first."])

        components = await self._managed.get_components(template_data["id"])
        view = build_view(components)

        try:
            msg = await channel.send(
                content=content,
                embeds=embeds[:10],
                view=view,
                allowed_mentions=TEMPLATE_ALLOWED_MENTIONS,
            )
        except discord.HTTPException as exc:
            return (None, [f"Failed to send message: {exc}"])

        await self._managed.create(
            guild_id=guild.id,
            channel_id=channel.id,
            message_id=msg.id,
            template_id=template_data["id"],
            created_by=actor_id,
        )
        _publish("post", template_id=template_data["id"])

        return (msg.id, [])

    # ------------------------------------------------------------------
    # Sync a single template → all managed messages that use it
    # ------------------------------------------------------------------

    async def sync_template(self, template_id: int) -> None:
        managed = await self._managed.list_for_template(template_id)
        if not managed:
            return

        template_full = await self._templates.get_full(template_id)
        if not template_full:
            log.warning("sync_template: template_id=%d no longer exists", template_id)
            return

        components = await self._managed.get_components(template_id)

        for record in managed:
            guild = self._bot.get_guild(record["guild_id"])
            if guild is None:
                continue

            channel = guild.get_channel(record["channel_id"])
            if channel is None:
                await self._managed.mark_failed(record["message_id"])
                log.warning("sync_template: channel %d not found", record["channel_id"])
                continue

            ctx = RenderContext(guild=guild, member=None, channel=channel)
            try:
                rendered = self._renderer.render_message(template_full, ctx)
            except Exception:
                log.exception("sync_template: render error for template_id=%d", template_id)
                await self._managed.mark_failed(record["message_id"])
                continue

            content = rendered.get("content") or None
            embeds = rendered.get("embeds") or []
            view = build_view(components)

            try:
                message = await channel.fetch_message(record["message_id"])
                await message.edit(
                    content=content,
                    embeds=embeds[:10],
                    view=view,
                    allowed_mentions=TEMPLATE_ALLOWED_MENTIONS,
                )
                await self._managed.mark_synced(record["message_id"])
            except discord.NotFound:
                log.warning(
                    "sync_template: message %d not found; removing managed record",
                    record["message_id"],
                )
                await self._managed.delete(record["message_id"])
            except discord.HTTPException:
                log.exception("sync_template: failed to edit message %d", record["message_id"])
                await self._managed.mark_failed(record["message_id"])

        _publish("sync", template_id=template_id)

    async def sync_now(self, guild_id: int, template_id: int, actor_id: int | None = None) -> None:
        """Force re-sync every managed message of one template now (/managed
        sync and the dashboard), with an audit row."""
        await self.sync_template(template_id)
        await self._audit_log(guild_id, actor_id, "managed_sync", "template", template_id, {})

    # ------------------------------------------------------------------
    # Listing / deletion
    # ------------------------------------------------------------------

    async def list_managed(self, guild_id: int) -> list[dict]:
        return await self._managed.list_for_guild(guild_id)

    async def untrack(self, guild_id: int, message_id: int, actor_id: int | None = None) -> bool:
        record = await self._managed.get_by_message_id(message_id)
        if not record or record["guild_id"] != guild_id:
            return False
        removed = await self._managed.delete(message_id)
        if removed:
            await self._audit_log(guild_id, actor_id, "managed_untrack", "message", message_id, {
                "channel_id": str(record["channel_id"]), "template_id": record["template_id"],
            })
            _publish("untrack", message_id=str(message_id))
        return removed
