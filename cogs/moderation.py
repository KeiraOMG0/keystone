import logging

import discord
from discord.ext import commands
import re
import asyncio
from datetime import timedelta

import config as _config
from core.placeholder_engine import RenderContext
from core.renderer import TemplateRenderer
from database.repositories.audit_repo import AuditRepository
from database.repositories.embed_repo import EmbedRepository
from database.repositories.template_repo import TemplateRepository
from database.repositories.version_repo import VersionRepository
from services.audit_service import AuditService
from services.template_service import TemplateService
from database.repositories.warning_repo import WarningRepository
from services.warning_service import WarningService
from database.repositories.mod_action_repo import ModActionRepository
from services.moderation_service import (
    ModerationBlocked,
    ModerationInputError,
    ModerationService,
    member_block_reason,
    reason_error,
    timeout_minutes_error,
)
from services.log_router import build_embed
from ui.confirm import ConfirmView

log = logging.getLogger(__name__)

LOG_CHANNEL_ID = _config.LOG_CHANNEL_ID
# Bans/unbans are membership removals -> join-leave channel (see
# services/log_router.py's category mapping). Everything else handled by
# _send_mod_log (timeout/untimeout/warn) stays on the moderation-log channel.
JOIN_LEAVE_CHANNEL_ID = _config.JOIN_LEAVE_CHANNEL_ID

# Ban/unban/timeout/untimeout template-name constants now live in
# services/moderation_service.py alongside the logic that uses them.
# warn/delwarn/purge stay inline here (not yet migrated to the service).
MOD_WARN_LOG_TEMPLATE_NAME = "mod-warn-log"
MOD_DELWARN_LOG_TEMPLATE_NAME = "mod-delwarn-log"
MOD_PURGE_LOG_TEMPLATE_NAME = "mod-purge-log"

# Purge bounds/parsing live in services/purge_service.py (shared with the
# dashboard purge job); re-exported here for existing callers/tests.
from services.purge_service import (  # noqa: E402
    describe_filter,
    PURGE_CONFIRM_ABOVE,
    PURGE_FILTERED_SCAN_LIMIT,
    PURGE_MAX,
    PURGE_MAX_WINDOW,
    PURGE_MIN,
    PURGE_USAGE,
    describe_window,
    make_purge_check,
    parse_purge_amount,
    purge_audit_detail,
    purge_channel,
    purge_count_error,
    purge_log_embed,
)

PURGE_CONFIRM_TIMEOUT = 30


def _parse_user_id(target: str) -> int | None:
    """Parse a raw user ID from a mention (<@123>, <@!123>) or a bare numeric ID."""
    match = re.match(r"<@!?(\d+)>", target.strip())
    if match:
        return int(match.group(1))
    if target.strip().isdigit():
        return int(target.strip())
    return None


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._audit: "AuditService | None" = None
        self._warnings: "WarningService | None" = None
        self._templates: "TemplateService | None" = None
        self._renderer: "TemplateRenderer | None" = None
        self.moderation_service: "ModerationService | None" = None
        if hasattr(bot, "db") and bot.db is not None:
            self._audit = AuditService(AuditRepository(bot.db))
            # WarningService writes the member_warned / warning_removed
            # audit rows itself (same as the dashboard path).
            self._warnings = WarningService(WarningRepository(bot.db), self._audit)
            self._templates = TemplateService(
                TemplateRepository(bot.db), EmbedRepository(bot.db),
                VersionRepository(bot.db), AuditRepository(bot.db),
            )
            self._renderer = TemplateRenderer()
            self.moderation_service = ModerationService(
                ModActionRepository(bot.db), self._audit, self._templates, self._renderer,
            )

    # ------------------------------------------------------------------
    # Mod log helper — channel-log-only (no DMs, per approved design).
    # Sends `template_name` to `channel_id` (join-leave for membership
    # removals like ban/unban, moderation-log for everything else — see
    # each call site); falls back to `fallback_embed` if the template is
    # missing/empty/fails to render (matches cogs/verify.py's fallback
    # pattern).
    # ------------------------------------------------------------------
    async def _send_mod_log(
        self,
        guild: discord.Guild,
        template_name: str,
        member: discord.Member | None,
        actor: discord.Member,
        fallback_embed: discord.Embed,
        channel_id: int = LOG_CHANNEL_ID,
        extra: dict | None = None,
    ) -> None:
        log_channel = guild.get_channel(channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            log.warning("moderation: log channel %d not found in guild %d", channel_id, guild.id)
            return

        content = None
        embeds: list[discord.Embed] = []

        if self._templates is not None and self._renderer is not None:
            try:
                template_full = await self._templates.get_full(guild.id, template_name)
            except Exception:
                log.exception("moderation: failed to look up '%s' template", template_name)
                template_full = None

            if template_full is not None:
                ctx = RenderContext(guild=guild, member=member, channel=log_channel, actor=actor,
                                    extra={"source": "command", **(extra or {})})
                try:
                    rendered = self._renderer.render_message(template_full, ctx)
                    content = rendered.get("content") or None
                    embeds = rendered.get("embeds") or []
                except Exception:
                    log.exception("moderation: failed to render '%s' template — using fallback", template_name)
                    content = None
                    embeds = []

        if not content and not embeds:
            embeds = [fallback_embed]

        try:
            await log_channel.send(content=content, embeds=embeds[:10])
        except discord.HTTPException:
            log.exception("moderation: failed to send mod log to channel %d", LOG_CHANNEL_ID)

    # ------------------------------------------------------------------
    # !timeout
    # ------------------------------------------------------------------
    @commands.command(name="timeout", hidden=True)
    async def timeout_cmd(self, ctx: commands.Context, target: str = None, duration_minutes: int = None, *, reason: str = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "moderate_members"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if target is None or duration_minutes is None:
            await ctx.reply("Usage: `!timeout <@user> <duration_minutes> [reason]`")
            return

        raw_id = _parse_user_id(target)
        if raw_id is None:
            await ctx.reply("Invalid user. Provide a mention or user ID.")
            return

        input_error = timeout_minutes_error(duration_minutes) or reason_error(reason)
        if input_error:
            await ctx.reply(input_error)
            return

        member = ctx.guild.get_member(raw_id)
        if member is None:
            await ctx.reply("Could not find that member in this server.")
            return

        if self.moderation_service is None:
            await ctx.reply("Moderation system is unavailable.")
            return

        try:
            await self.moderation_service.timeout(
                member, ctx.guild, ctx.author, duration_minutes, reason,
                log_channel_id=LOG_CHANNEL_ID, check_hierarchy=True, log_extra={"source": "command"},
            )
            await ctx.reply(f"Timed out <@{member.id}> for {duration_minutes} minute(s).")
        except (ModerationBlocked, ModerationInputError) as e:
            await ctx.reply(str(e))
        except discord.Forbidden as e:
            await ctx.reply(f"Missing permissions to timeout that member. (code {e.code})")
        except discord.HTTPException as e:
            await ctx.reply(f"Failed to timeout: {e}")

    # ------------------------------------------------------------------
    # !untimeout
    # ------------------------------------------------------------------
    @commands.command(name="untimeout", hidden=True)
    async def untimeout_cmd(self, ctx: commands.Context, target: str = None, *, reason: str = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "moderate_members"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if target is None:
            await ctx.reply("Usage: `!untimeout <@user> [reason]`")
            return

        raw_id = _parse_user_id(target)
        if raw_id is None:
            await ctx.reply("Invalid user. Provide a mention or user ID.")
            return

        if reason_error(reason):
            await ctx.reply(reason_error(reason))
            return

        member = ctx.guild.get_member(raw_id)
        if member is None:
            await ctx.reply("Could not find that member in this server.")
            return

        if self.moderation_service is None:
            await ctx.reply("Moderation system is unavailable.")
            return

        try:
            await self.moderation_service.untimeout(
                member, ctx.guild, ctx.author, reason,
                log_channel_id=LOG_CHANNEL_ID, check_hierarchy=True, log_extra={"source": "command"},
            )
            await ctx.reply(f"Removed timeout from <@{member.id}>.")
        except (ModerationBlocked, ModerationInputError) as e:
            await ctx.reply(str(e))
        except discord.Forbidden as e:
            await ctx.reply(f"Missing permissions to remove timeout from that member. (code {e.code})")
        except discord.HTTPException as e:
            await ctx.reply(f"Failed to remove timeout: {e}")

    # ------------------------------------------------------------------
    # !ban
    # ------------------------------------------------------------------
    @commands.command(name="ban", hidden=True)
    async def ban_cmd(self, ctx: commands.Context, target: str = None, *, reason: str = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "ban_members"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if target is None:
            await ctx.reply("Usage: `!ban <@user or user ID> [reason]`")
            return

        raw_id = _parse_user_id(target)
        if raw_id is None:
            await ctx.reply("Invalid user. Provide a mention or user ID.")
            return

        if reason_error(reason):
            await ctx.reply(reason_error(reason))
            return

        if self.moderation_service is None:
            await ctx.reply("Moderation system is unavailable.")
            return

        try:
            member_obj = ctx.guild.get_member(raw_id)
            await self.moderation_service.ban(
                ctx.guild, raw_id, ctx.author, reason,
                member=member_obj, log_channel_id=JOIN_LEAVE_CHANNEL_ID,
                delete_message_days=0, check_hierarchy=True, log_extra={"source": "command"},
            )
            await ctx.reply(f"Banned <@{raw_id}>.")
        except (ModerationBlocked, ModerationInputError) as e:
            await ctx.reply(str(e))
        except discord.Forbidden as e:
            await ctx.reply(f"Missing permissions to ban that user. (code {e.code})")
        except discord.HTTPException as e:
            await ctx.reply(f"Failed to ban: {e}")

    # ------------------------------------------------------------------
    # !unban
    # ------------------------------------------------------------------
    @commands.command(name="unban", hidden=True)
    async def unban_cmd(self, ctx: commands.Context, user_id: str = None, *, reason: str = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "ban_members"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if user_id is None or not user_id.strip().isdigit():
            await ctx.reply("Usage: `!unban <user ID> [reason]`")
            return

        raw_id = int(user_id.strip())

        if reason_error(reason):
            await ctx.reply(reason_error(reason))
            return

        if self.moderation_service is None:
            await ctx.reply("Moderation system is unavailable.")
            return

        try:
            await self.moderation_service.unban(
                ctx.guild, raw_id, ctx.author, reason,
                log_channel_id=JOIN_LEAVE_CHANNEL_ID, log_extra={"source": "command"},
            )
            await ctx.reply(f"Unbanned <@{raw_id}>.")
        except ModerationInputError as e:
            await ctx.reply(str(e))
        except discord.NotFound:
            await ctx.reply(f"<@{raw_id}> is not banned.")
        except discord.Forbidden as e:
            await ctx.reply(f"Missing permissions to unban that user. (code {e.code})")
        except discord.HTTPException as e:
            await ctx.reply(f"Failed to unban: {e}")

    # ------------------------------------------------------------------
    # !purge
    # ------------------------------------------------------------------
    @commands.command(name="purge", hidden=True)
    async def purge_cmd(self, ctx: commands.Context, amount: str = None, target: str = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "manage_messages_bulk"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if amount is None:
            await ctx.reply(PURGE_USAGE)
            return

        count, window, error = parse_purge_amount(amount)
        if error:
            await ctx.reply(error)
            return

        # The filter is by ID only, so it also works for someone who already
        # left or was banned (the usual state after a spam raid).
        user_id = None
        if target is not None:
            user_id = _parse_user_id(target)
            if user_id is None:
                await ctx.reply("Invalid user. Provide a mention or user ID.")
                return
        member = ctx.guild.get_member(user_id) if user_id else None

        whose = f" from <@{user_id}>" if user_id else ""
        if window is not None:
            scope = f"up to {PURGE_MAX} messages{whose} posted in the last {describe_window(window)}"
        else:
            scope = f"the last {count} messages{whose}"

        prompt = None
        if window is not None or count > PURGE_CONFIRM_ABOVE:
            view = ConfirmView(timeout=PURGE_CONFIRM_TIMEOUT, allowed_user_id=ctx.author.id)
            prompt = await ctx.reply(
                f"Delete {scope} in {ctx.channel.mention}? This can't be undone.",
                view=view, allowed_mentions=discord.AllowedMentions.none(),
            )
            await view.wait()
            if not view.confirmed:
                try:
                    await prompt.edit(content="Purge cancelled.", view=None)
                except discord.HTTPException:
                    pass
                return

        try:
            # before=ctx.message keeps the command message out of the count
            # (it used to be counted, so `!purge 5` removed only 4 messages);
            # it is deleted separately below, as is the confirm prompt (posted
            # after it, so never purged). With a user filter, scan further
            # back so N of THAT user's messages are found, not N scanned. A
            # time window walks newest-first, so hitting the PURGE_MAX cap
            # leaves the oldest messages of the window, not the newest.
            reason = f"!purge by {ctx.author} ({ctx.author.id})"
            deleted = await purge_channel(
                ctx.channel, count=count, window=window, user_id=user_id,
                reason=reason, before=ctx.message,
            )
            for leftover in (prompt, ctx.message):
                if leftover is None:
                    continue
                try:
                    await leftover.delete()
                except discord.HTTPException:
                    pass
            if self._audit:
                await self._audit.log(
                    ctx.guild.id, ctx.author.id, "messages_purged",
                    target_type="channel", target_id=ctx.channel.id,
                    detail=purge_audit_detail(len(deleted), user_id, window),
                )
            fallback = purge_log_embed(ctx.channel, ctx.author.id, len(deleted), user_id, window)
            await self._send_mod_log(
                ctx.guild, MOD_PURGE_LOG_TEMPLATE_NAME, member, ctx.author, fallback, LOG_CHANNEL_ID,
                extra={"action": "Purge", "purge_count": len(deleted), "purge_channel": ctx.channel,
                       "target_id": user_id, "purge_filter": describe_filter(count, window, user_id)},
            )
            confirmation = await ctx.send(f"Deleted {len(deleted)} message(s).")
            await asyncio.sleep(3)
            try:
                await confirmation.delete()
            except discord.HTTPException:
                pass
        except discord.Forbidden as e:
            await ctx.reply(f"Missing permissions to purge messages. (code {e.code})")
        except discord.HTTPException as e:
            await ctx.reply(f"Failed to purge: {e}")

    # ------------------------------------------------------------------
    # !warn
    # ------------------------------------------------------------------
    @commands.command(name="warn", hidden=True)
    async def warn_cmd(self, ctx: commands.Context, target: str = None, *, reason: str = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "moderate_members"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if target is None:
            await ctx.reply("Usage: `!warn <@user> [reason]`")
            return

        raw_id = _parse_user_id(target)
        if raw_id is None:
            await ctx.reply("Invalid user. Provide a mention or user ID.")
            return

        member = ctx.guild.get_member(raw_id)
        if member is None:
            await ctx.reply("Could not find that member in this server.")
            return

        if reason_error(reason):
            await ctx.reply(reason_error(reason))
            return

        blocked = member_block_reason(ctx.guild, ctx.author, member)
        if blocked:
            await ctx.reply(blocked)
            return

        if self._warnings is None:
            await ctx.reply("Warnings system is unavailable.")
            return

        try:
            # add_warning also writes the member_warned audit row.
            warning_id = await self._warnings.add_warning(ctx.guild.id, member.id, ctx.author.id, reason)
            await ctx.reply(f"Warned <@{member.id}> (warning `#{warning_id}`).")
            if self.moderation_service is not None:
                await self.moderation_service.record_warn_action(
                    ctx.guild.id, member.id, ctx.author.id, reason, warning_id,
                )
            fallback = build_embed(
                "member_warned", member=member, actor_id=ctx.author.id, reason=reason,
                extra_fields={"Warning ID": f"#{warning_id}"},
            )
            try:
                warning_count = len(await self._warnings.get_warnings(ctx.guild.id, member.id))
            except Exception:
                warning_count = None
            await self._send_mod_log(
                ctx.guild, MOD_WARN_LOG_TEMPLATE_NAME, member, ctx.author, fallback, LOG_CHANNEL_ID,
                extra={"action": "Warning", "reason": reason, "warning_id": warning_id,
                       "warning_count": warning_count},
            )
        except discord.HTTPException as e:
            await ctx.reply(f"Failed to warn: {e}")

    # ------------------------------------------------------------------
    # !warnings
    # ------------------------------------------------------------------
    @commands.command(name="warnings", hidden=True)
    async def warnings_cmd(self, ctx: commands.Context, target: str = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "moderate_members"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if target is None:
            await ctx.reply("Usage: `!warnings <@user>`")
            return

        raw_id = _parse_user_id(target)
        if raw_id is None:
            await ctx.reply("Invalid user. Provide a mention or user ID.")
            return

        if self._warnings is None:
            await ctx.reply("Warnings system is unavailable.")
            return

        rows = await self._warnings.get_warnings(ctx.guild.id, raw_id)
        if not rows:
            await ctx.reply(f"<@{raw_id}> has no warnings.")
            return

        lines = []
        for i, row in enumerate(rows, start=1):
            reason = row.get("reason") or "No reason given"
            lines.append(
                f"**{i}.** `#{row['id']}` — {reason} "
                f"(by <@{row['moderator_id']}> on {row['created_at']})"
            )
        embed = discord.Embed(
            title=f"Warnings for {raw_id}",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        await ctx.reply(embed=embed)

    # ------------------------------------------------------------------
    # !delwarn
    # ------------------------------------------------------------------
    @commands.command(name="delwarn", hidden=True)
    async def delwarn_cmd(self, ctx: commands.Context, target: str = None, warning_id: int = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "moderate_members"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if target is None or warning_id is None:
            await ctx.reply("Usage: `!delwarn <@user> <warning_id>`")
            return

        raw_id = _parse_user_id(target)
        if raw_id is None:
            await ctx.reply("Invalid user. Provide a mention or user ID.")
            return

        if self._warnings is None:
            await ctx.reply("Warnings system is unavailable.")
            return

        # Only a warning that belongs to <user> is removed (the user argument
        # used to be ignored, so a mistyped id could erase anyone's warning).
        # remove_warning also writes the warning_removed audit row with the
        # command author as the actor.
        removed = await self._warnings.remove_warning(
            ctx.guild.id, warning_id, user_id=raw_id, actor_id=ctx.author.id,
        )
        if removed:
            await ctx.reply(f"Removed warning `#{warning_id}` from <@{raw_id}>.")
            if self.moderation_service is not None:
                await self.moderation_service.record_warning_removed_action(
                    ctx.guild.id, raw_id, ctx.author.id, warning_id,
                )
            member_obj = ctx.guild.get_member(raw_id)
            fallback = build_embed(
                "warning_removed", member_id=raw_id, actor_id=ctx.author.id,
                extra_fields={"Warning ID": f"#{warning_id}"},
            )
            try:
                warning_count = len(await self._warnings.get_warnings(ctx.guild.id, raw_id))
            except Exception:
                warning_count = None
            await self._send_mod_log(
                ctx.guild, MOD_DELWARN_LOG_TEMPLATE_NAME, member_obj, ctx.author, fallback, LOG_CHANNEL_ID,
                extra={"action": "Warning removed", "warning_id": warning_id, "target_id": raw_id,
                       "warning_count": warning_count},
            )
        else:
            await ctx.reply(f"Warning `#{warning_id}` not found for <@{raw_id}>.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))
