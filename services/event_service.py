from __future__ import annotations

import logging

import discord

from core.event_bus import EventBus
from core.placeholder_engine import RenderContext
from core.renderer import TemplateRenderer
from database.repositories.event_repo import EventRepository
from database.repositories.template_repo import TemplateRepository
from services.live_events import publish as live_publish

log = logging.getLogger(__name__)

VALID_EVENT_TYPES = {"member_join", "member_leave", "boost", "role_add", "role_remove"}


def _publish(action: str, **data) -> None:
    """Live `automation` topic (dashboard Automation page). Never raises."""
    live_publish("automation", {"section": "events", "action": action, **data})


class EventService:
    def __init__(
        self,
        event_repo: EventRepository,
        template_repo: TemplateRepository,
        renderer: TemplateRenderer,
        bus: EventBus,
        bot: discord.Client,
        audit=None,
    ) -> None:
        self._events = event_repo
        self._templates = template_repo
        self._renderer = renderer
        self._bus = bus
        self._bot = bot
        # services.audit_service.AuditService | None. Shared by /event and the
        # dashboard Automation page, so the audit rows are written here once.
        self._audit = audit

        bus.subscribe("member_join", self._on_member_join)
        bus.subscribe("member_leave", self._on_member_leave)
        bus.subscribe("boost", self._on_boost)
        bus.subscribe("role_add", self._on_role_add)
        bus.subscribe("role_remove", self._on_role_remove)

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        event_type: str,
        guild: discord.Guild,
        member: discord.Member | None,
        *,
        role: discord.Role | None = None,
    ) -> bool:
        binding = await self._events.get_active(guild.id, event_type)
        if not binding:
            return False

        channel = guild.get_channel(binding["channel_id"])
        if not channel:
            log.warning("event_service: channel %d not found for event '%s'", binding["channel_id"], event_type)
            return False

        templates = await self._events.get_templates_for_binding(binding["id"])
        if not templates:
            log.debug("event_service: no templates bound to event '%s'", event_type)
            return False

        ctx = RenderContext(guild=guild, member=member, channel=channel, extra={"role": role})

        for tmpl_row in templates:
            template_data = await self._templates.get_full(tmpl_row["id"])
            if not template_data:
                continue
            try:
                rendered = self._renderer.render_message(template_data, ctx)
            except Exception:
                log.exception("event_service: render error for template '%s'", tmpl_row["name"])
                continue

            content = rendered.get("content") or ""
            embeds = rendered.get("embeds") or []

            if not content and not embeds:
                continue

            try:
                await channel.send(content=content or None, embeds=embeds[:10])
            except discord.HTTPException:
                log.exception("event_service: failed to send to channel %d", channel.id)

            if binding["send_dm"] and member:
                try:
                    await member.send(content=content or None, embeds=embeds[:10])
                except (discord.HTTPException, discord.Forbidden):
                    pass

        return True

    async def _on_member_join(self, member: discord.Member) -> None:
        await self._dispatch("member_join", member.guild, member)

    async def _on_member_leave(self, member: discord.Member) -> None:
        await self._dispatch("member_leave", member.guild, member)

    async def _on_boost(self, member: discord.Member) -> None:
        await self._dispatch("boost", member.guild, member)

    async def _on_role_add(self, member: discord.Member, role_id: int) -> None:
        role = member.guild.get_role(role_id)
        await self._dispatch("role_add", member.guild, member, role=role)

    async def _on_role_remove(self, member: discord.Member, role_id: int) -> None:
        role = member.guild.get_role(role_id)
        await self._dispatch("role_remove", member.guild, member, role=role)

    # ------------------------------------------------------------------
    # Management API (called by the event_commands cog and the internal API's
    # dashboard Automation routes, internal_api/routes/automation.py)
    # ------------------------------------------------------------------

    async def _audit_log(self, guild_id: int, actor_id: int | None, action: str, detail: dict) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.log(guild_id, actor_id or 0, action, "event", None, detail)
        except Exception:  # the change already happened; never fail it over the audit row
            log.exception("event_service: could not write %s audit row", action)

    async def set_binding(
        self,
        guild_id: int,
        event_type: str,
        channel_id: int,
        template_name: str,
        actor_id: int,
        send_dm: bool = False,
    ) -> list[str]:
        if event_type not in VALID_EVENT_TYPES:
            return [f"Unknown event type '{event_type}'. Valid: {', '.join(sorted(VALID_EVENT_TYPES))}."]

        template = await self._templates.get_by_name(guild_id, template_name)
        if not template:
            return [f"No template named '{template_name}' found."]

        binding_id = await self._events.create_binding(guild_id, event_type, channel_id, actor_id, send_dm)
        await self._events.clear_templates(binding_id)
        await self._events.set_template(binding_id, template["id"], position=0)
        await self._audit_log(guild_id, actor_id, "event_binding_set", {
            "event_type": event_type, "channel_id": str(channel_id),
            "template": template_name, "send_dm": bool(send_dm),
        })
        _publish("set", event_type=event_type)
        return []

    async def clear_binding(self, guild_id: int, event_type: str, actor_id: int | None = None) -> bool:
        deleted = await self._events.delete_binding(guild_id, event_type)
        if deleted:
            await self._audit_log(guild_id, actor_id, "event_binding_clear", {"event_type": event_type})
            _publish("clear", event_type=event_type)
        return deleted

    async def list_bindings(self, guild_id: int) -> list[dict]:
        bindings = await self._events.list_bindings(guild_id)
        result = []
        for b in bindings:
            templates = await self._events.get_templates_for_binding(b["id"])
            result.append({**b, "templates": templates})
        return result

    async def test_event(self, event_type: str, guild: discord.Guild, member: discord.Member) -> tuple[bool, list[str]]:
        """Fire `event_type` with `member` as the member (the /event test command
        and the dashboard "Test event" button; posts to the REAL bound channel)."""
        if event_type not in VALID_EVENT_TYPES:
            return (False, [f"Unknown event type '{event_type}'."])
        sent = await self._dispatch(event_type, guild, member)
        if sent:
            await self._audit_log(guild.id, member.id, "event_test", {"event_type": event_type})
        return (sent, [])

    async def active_channel_id(self, guild_id: int, event_type: str) -> int | None:
        """Channel of the enabled binding for `event_type`, or None."""
        binding = await self._events.get_active(guild_id, event_type)
        return binding["channel_id"] if binding else None
