"""
cogs/verify.py — staff-invoked manual verification.

!verify <@mention or user ID> removes the Unverified role, adds the
Verified role, and sends a customizable message using the bot's template
system (the same one /template create/edit drives) — so staff can change
the verification message wording/embed at any time without a code change.

Role IDs come from config.py (UNVERIFIED_ROLE_ID / VERIFIED_ROLE_ID), the
same source cogs/kick.py's auto-kick system uses. The message template is
looked up by name (config.VERIFY_TEMPLATE_NAME, default "verify") via
/template create — if no such template exists yet, a plain fallback
message is sent instead so the command still works before staff configure
one.

The actual role-swap + messaging logic lives in
services/verify_service.verify_member() (re-exported below) so it is shared
by `!verify`, the ticket system's "Verify & Close" button (cogs/tickets.py)
and the dashboard's member profile Verify button, and can't drift out of sync
between them. The verify message is sent into a caller-supplied channel
(never a DM).
"""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

import config as _config
from core.placeholder_engine import RenderContext
from core.renderer import TemplateRenderer
from database.repositories.audit_repo import AuditRepository
from database.repositories.embed_repo import EmbedRepository
from database.repositories.template_repo import TemplateRepository
from database.repositories.version_repo import VersionRepository
from services.audit_service import AuditService
from services.template_service import TemplateService

log = logging.getLogger(__name__)

UNVERIFIED_ROLE_ID = _config.UNVERIFIED_ROLE_ID
VERIFIED_ROLE_ID = _config.VERIFIED_ROLE_ID
VERIFY_TEMPLATE_NAME = _config.VERIFY_TEMPLATE_NAME
UNVERIFIED_CATEGORY_ID = _config.UNVERIFIED_CATEGORY_ID

_MENTION_RE = re.compile(r"<@!?(\d+)>")


# The role-swap + message + audit logic lives in services/verify_service.py
# (shared with the ticket "Verify & Close" button and the dashboard's member
# profile Verify button); re-exported here so `from cogs.verify import
# verify_member` keeps working.
from services.verify_service import VerifyResult, verify_member  # noqa: E402,F401


class VerifyCog(commands.Cog, name="Verify"):
    def __init__(
        self,
        bot: commands.Bot,
        template_service: TemplateService,
        renderer: TemplateRenderer,
        audit: AuditService,
    ) -> None:
        self.bot = bot
        self._templates = template_service
        self._renderer = renderer
        self._audit = audit

    @commands.command(name="verify", hidden=True)
    async def verify_cmd(self, ctx: commands.Context, *, target: str = None) -> None:
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "verify_members"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if ctx.channel.category_id != UNVERIFIED_CATEGORY_ID:
            await ctx.reply("This command can only be used in the Unverified category.")
            return

        if target is None:
            await ctx.reply("Usage: `!verify <@mention or user ID>`")
            return

        match = _MENTION_RE.match(target.strip())
        raw_id = match.group(1) if match else (target.strip() if target.strip().isdigit() else None)
        if raw_id is None:
            await ctx.reply("Invalid user. Provide a mention or user ID.")
            return

        member = ctx.guild.get_member(int(raw_id))
        if member is None:
            await ctx.reply("Could not find that member in this server.")
            return

        result = await verify_member(member, ctx.author, ctx.channel, self._templates, self._renderer, self._audit,
                                     source="command")

        if not result.ok:
            if result.already_verified:
                await ctx.reply(f"{member.mention} is already verified.")
            else:
                await ctx.reply(result.error)
            return

        await ctx.reply(f"Verified {member.mention} — {result.message_status}.")


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        log.warning("bot.db not available — verify cog not loaded.")
        return

    template_repo = TemplateRepository(bot.db)
    embed_repo = EmbedRepository(bot.db)
    version_repo = VersionRepository(bot.db)
    audit_repo = AuditRepository(bot.db)

    template_service = TemplateService(template_repo, embed_repo, version_repo, audit_repo)
    renderer = TemplateRenderer()
    audit = AuditService(audit_repo)

    await bot.add_cog(VerifyCog(bot, template_service, renderer, audit))
