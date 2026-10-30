from __future__ import annotations
import logging

import discord

import config

OWNER_ID = config.OWNER_ID
DEFAULT_STAFF_ROLE_IDS = config.STAFF_ROLE_IDS

VALID_PERMISSIONS = {
    "manage_templates",
    "manage_events",
    "manage_tickets",
    "manage_schedules",
    "manage_autorole",
    "manage_reaction_roles",
    "manage_qotd",
    "view_audit",
    "kick_members",
    "staff_base",
    "verify_members",
    "moderate_members",
    "ban_members",
    "manage_messages_bulk",
    "control_music",  # dashboard Music page: view + control the player (Discord music commands stay open to all)
    "manage_birthdays",  # birthday list/config/override/import-export (cogs/birthday_*, /dashboard/birthdays)
}


log = logging.getLogger(__name__)


class PermissionService:
    def __init__(self, perm_repo=None, audit=None):
        self._repo = perm_repo  # PermissionRepository | None — None = legacy hard-coded only
        # services.audit_service.AuditService | None. Every grant/revoke goes
        # through this class (the /permission slash commands and the dashboard
        # Permissions page), so the audit rows are written here, once.
        self._audit = audit

    async def _audit_change(self, guild_id: int, actor_id: int | None, action: str, role_id: int, detail: dict) -> None:
        if self._audit is None:
            return
        try:
            # audit_log.actor_id is NOT NULL: 0 = actor unknown (a caller that
            # did not pass actor_id).
            await self._audit.log(guild_id, actor_id or 0, action, "role", role_id, detail)
        except Exception:  # the change itself already happened; never fail it over the audit row
            log.exception("could not write %s audit row", action)

    def is_staff(self, member: discord.Member) -> bool:
        if member.id == OWNER_ID:
            return True
        return bool({r.id for r in member.roles} & DEFAULT_STAFF_ROLE_IDS)

    def is_staff_interaction(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return interaction.user.id == OWNER_ID
        return self.is_staff(interaction.user)

    async def has_permission(self, member: discord.Member, permission: str) -> bool:
        """Check fine-grained permission. Owner and default staff always pass."""
        if self.is_staff(member):
            return True
        if self._repo is None:
            return False
        role_ids = [r.id for r in member.roles]
        return await self._repo.has_permission(member.guild.id, role_ids, permission)

    async def is_any_staff(self, member: discord.Member) -> bool:
        """True for owner/default staff, or any role holding the base staff_base grant."""
        return await self.has_permission(member, "staff_base")

    async def has_any_grant(self, member: discord.Member) -> bool:
        """True if any of the member's roles holds ANY DB permission grant (dashboard-defined staff)."""
        if self._repo is None:
            return False
        return await self._repo.has_any_permission(member.guild.id, [r.id for r in member.roles])

    async def is_any_staff_interaction(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return interaction.user.id == OWNER_ID
        return await self.is_any_staff(interaction.user)

    async def grant(self, guild_id: int, role_id: int, permission: str, granted_by: int) -> list[str]:
        if permission not in VALID_PERMISSIONS:
            return [f"Unknown permission '{permission}'. Valid: {', '.join(sorted(VALID_PERMISSIONS))}."]
        if self._repo is None:
            return ["Permission repo not available."]
        already = permission in await self._repo.get_role_permissions(guild_id, role_id)
        await self._repo.grant(guild_id, role_id, permission, granted_by)
        detail = {"permission": permission}
        if already:
            detail["unchanged"] = True  # re-grant: only granted_by/granted_at were refreshed
        await self._audit_change(guild_id, granted_by, "permission_grant", role_id, detail)
        return []

    async def revoke(self, guild_id: int, role_id: int, permission: str, actor_id: int | None = None) -> bool:
        """Remove a grant. `actor_id` (who did it) is only used for the audit row."""
        if self._repo is None:
            return False
        removed = await self._repo.revoke(guild_id, role_id, permission)
        if removed:
            await self._audit_change(guild_id, actor_id, "permission_revoke", role_id, {"permission": permission})
        return removed

    async def list_grants(self, guild_id: int) -> list[dict]:
        if self._repo is None:
            return []
        return await self._repo.list_grants(guild_id)
