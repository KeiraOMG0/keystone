"""
services/autorole_service.py - automatic role assignment (join / boost /
role_add triggers), configured with /autorole.

Self-role safety (services/self_role_policy.py): add_rule refuses a role the
configuring member may not make self-assignable (staff/granted/dangerous/
too high), and every application re-checks, right before add_roles, that the
rule is still enabled in the DB and the role is still safe to hand out
(no actor part). A refused application is skipped with a warning log.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from database.repositories.advanced_repo import AutoroleRepository
from services import self_role_policy
from services.live_events import publish as live_publish

log = logging.getLogger(__name__)

VALID_TRIGGERS = {"join", "boost", "role_add"}
# Upper bound for a rule's delay (the rule waits in memory with asyncio.sleep,
# so a restart during the delay drops that one application).
MAX_DELAY_SECONDS = 7 * 24 * 3600


def _publish(action: str, **data) -> None:
    """Live `automation` topic (dashboard Automation page). Never raises."""
    live_publish("automation", {"section": "autoroles", "action": action, **data})


class AutoroleService:
    def __init__(
        self, autorole_repo: AutoroleRepository, bot: discord.Client, permission_service=None, audit=None,
    ) -> None:
        self._rules = autorole_repo
        self._bot = bot
        self._perms = permission_service if permission_service is not None else getattr(bot, "permission_service", None)
        # services.audit_service.AuditService | None (shared by /autorole and
        # the dashboard Automation page, so rows are written here once).
        self._audit = audit

    async def _audit_log(self, guild_id: int, actor_id: int | None, action: str, target_id, detail: dict) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.log(guild_id, actor_id or 0, action, "autorole", target_id, detail)
        except Exception:  # the change already happened; never fail it over the audit row
            log.exception("autorole: could not write %s audit row", action)

    async def add_rule(
        self,
        guild_id: int,
        trigger_type: str,
        role_id: int,
        created_by: int,
        delay_seconds: int = 0,
        trigger_role_id: int | None = None,
        *,
        guild: discord.Guild,
        actor,
    ) -> tuple[int | None, list[str]]:
        """`guild` + `actor` (the configuring member) are required: the role
        goes through self_role_policy.configure_block_reason first. Used by
        /autorole add AND the dashboard (actor = the session user)."""
        if trigger_type not in VALID_TRIGGERS:
            return (None, [f"Unknown trigger '{trigger_type}'. Valid: {', '.join(sorted(VALID_TRIGGERS))}."])
        if trigger_type == "role_add" and trigger_role_id is None:
            return (None, ["trigger_role is required for role_add trigger."])
        if trigger_type != "role_add":
            # Only role_add rules are matched by trigger role (list_for_trigger
            # looks join/boost rules up with trigger_role_id IS NULL), so a
            # stray trigger role would make the rule never fire.
            trigger_role_id = None
        if not isinstance(delay_seconds, int) or not 0 <= delay_seconds <= MAX_DELAY_SECONDS:
            return (None, [f"Delay must be between 0 and {MAX_DELAY_SECONDS} seconds (7 days)."])
        if trigger_role_id is not None and guild.get_role(trigger_role_id) is None:
            return (None, ["That trigger role no longer exists."])
        reason = await self_role_policy.configure_block_reason(guild.get_role(role_id), guild, actor, self._perms)
        if reason:
            return (None, [reason])

        rule_id = await self._rules.add(
            guild_id=guild_id,
            trigger_type=trigger_type,
            role_id=role_id,
            created_by=created_by,
            delay_seconds=delay_seconds,
            trigger_role_id=trigger_role_id,
        )
        await self._audit_log(guild_id, created_by, "autorole_add", rule_id, {
            "trigger": trigger_type, "role_id": str(role_id), "delay_seconds": delay_seconds,
            "trigger_role_id": str(trigger_role_id) if trigger_role_id is not None else None,
        })
        _publish("add", rule_id=rule_id)
        return (rule_id, [])

    async def remove_rule(self, guild_id: int, rule_id: int, actor_id: int | None = None) -> bool:
        removed = await self._rules.remove(guild_id, rule_id)
        if removed:
            await self._audit_log(guild_id, actor_id, "autorole_remove", rule_id, {})
            _publish("remove", rule_id=rule_id)
        return removed

    async def list_rules(self, guild_id: int) -> list[dict]:
        return await self._rules.list_for_guild(guild_id)

    # ------------------------------------------------------------------
    # Runtime — called from gateway events
    # ------------------------------------------------------------------

    async def fire(
        self,
        guild: discord.Guild,
        member: discord.Member,
        trigger_type: str,
        trigger_role_id: int | None = None,
    ) -> None:
        rules = await self._rules.list_for_trigger(guild.id, trigger_type, trigger_role_id)
        for rule in rules:
            asyncio.create_task(
                self._apply(guild, member, rule),
                name=f"autorole-{rule['id']}-{member.id}",
            )

    async def _apply(self, guild: discord.Guild, member: discord.Member, rule: dict) -> None:
        delay = rule.get("delay_seconds") or 0
        if delay > 0:
            await asyncio.sleep(delay)

        # Re-fetch member to ensure they're still in the guild after any delay
        try:
            member = await guild.fetch_member(member.id)
        except discord.NotFound:
            return
        except discord.HTTPException:
            log.exception("autorole: failed to fetch member %d", member.id)
            return

        # Re-check at grant time: the rule may have been removed/disabled
        # during the delay, or the role may have become privileged since it
        # was configured.
        try:
            current = await self._rules.list_for_guild(guild.id)
        except Exception:
            log.exception("autorole: could not re-read rules; skipping rule %s", rule.get("id"))
            return
        if not any(r.get("id") == rule.get("id") and r.get("enabled") for r in current):
            log.info("autorole: rule %s no longer active; skipped for member %d", rule.get("id"), member.id)
            return

        role = guild.get_role(rule["role_id"])
        if role is None:
            log.warning("autorole: role %d not found in guild %d", rule["role_id"], guild.id)
            return

        reason = await self_role_policy.grant_block_reason(role, guild, self._perms)
        if reason:
            log.warning(
                "autorole: refused rule #%s, role %d (%s) for member %d: %s",
                rule.get("id"), role.id, role.name, member.id, reason,
            )
            return

        try:
            await member.add_roles(role, reason=f"Autorole rule #{rule['id']} ({rule['trigger_type']})")
        except discord.HTTPException:
            log.exception("autorole: failed to add role %d to member %d", role.id, member.id)
