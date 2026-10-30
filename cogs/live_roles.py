"""
cogs/live_roles.py: publishes the dashboard's live `roles` topic.

Listens to guild role create / update / delete and feeds them to
services.role_live_service.RoleEventCoalescer, which debounces bursts (a role
reorder fires one update per moved role) and publishes on the `roles` topic via
services.live_events. The dashboard's Self-roles and Permissions pages
invalidate their role lists on any `roles` message.

Separate from cogs/logging_role.py on purpose: that cog is audit logging (it
ignores position-only changes); this one cares about EVERY change to the role
list, positions included. No DB, no Discord output, no commands.
"""

from __future__ import annotations

import discord
from discord.ext import commands

from services import live_events
from services.role_live_service import RoleEventCoalescer


class LiveRolesCog(commands.Cog, name="LiveRoles"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.coalescer = RoleEventCoalescer(
            live_events.publish,
            has_subscribers=live_events.hub.has_subscribers,
        )

    async def cog_unload(self) -> None:
        self.coalescer.cancel()

    def _ours(self, role: discord.Role) -> bool:
        # The bot only stays in ALLOWED_GUILD_ID, but never leak another guild's
        # role events if it is briefly in one before leaving.
        gid = live_events.hub.guild_id
        return gid is None or role.guild.id == gid

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        if self._ours(role):
            self.coalescer.note("created", role.id)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if self._ours(after):
            self.coalescer.note("updated", after.id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self._ours(role):
            self.coalescer.note("deleted", role.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LiveRolesCog(bot))
