"""
cogs/anti_spam.py — automatic spam detection and cleanup.

Every member message goes through services/spam_detector.py (flood,
duplicate text across channels, mention spam). On a hit the bot:
  1. deletes the flagged messages (bulk per channel),
  2. times the member out for `antispam_timeout_minutes` through
     ModerationService.timeout (bot as moderator, so it lands in mod_actions
     and the member's dashboard history; 0 = delete only),
  3. writes a `spam_detected` audit row (actor = the bot),
  4. posts a "Spam Detected" embed to the moderation-log channel.
Messages the member sends during the 30 s cooldown after a hit are deleted
silently (no second timeout, log or audit row).

User-installed apps: a member can make an app they installed on their own
account post publicly here (no server authorisation needed while @everyone
has "Use External Apps"). Such a reply carries interaction_metadata, so it is
counted against the MEMBER who ran the command (flood/duplicate/mentions as
usual; a hit deletes the app's messages and times that member out). Replies
from the server's own bots and plain webhooks are never checked (the server
installed them, and log webhooks legitimately post in bursts).

Never checked: bots, webhooks, system messages, the owner / STAFF_ROLE_IDS,
the guild owner, Discord administrators (Discord won't time them out anyway)
and members holding the bot permission `moderate_members` or
`manage_messages_bulk` (looked up only when a rule fires, cached 60 s).

Settings are the dashboard's Anti-spam group (services/settings_service.py
antispam_* fields, all LIVE): read through SettingsService.get_effective()
with the shared 5 s cache, so a save applies within seconds.

Commands: `!antispam` (status, needs manage_messages_bulk) and
`!antispam on|off` (staff only, like the Settings page; writes the same
guild_settings row and settings_update audit row as a dashboard save).
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands

import config as _config
from services import settings_service as settings_module
from services.log_router import build_embed, route_and_post
from services.message_source import app_source, describe_app, message_text
from services.moderation_service import ModerationInputError
from services.spam_detector import SpamDetector, SpamRules, SpamVerdict

log = logging.getLogger(__name__)

_CHECKED_TYPES = (discord.MessageType.default, discord.MessageType.reply)
EXEMPT_PERMISSIONS = ("moderate_members", "manage_messages_bulk")
EXEMPT_CACHE_SECONDS = 60.0
RULES_CACHE_KEY = "antispam_rules"
SAMPLE_MAX = 300
DELETE_REASON = "Anti-spam"

_TIMEOUT_RESULT_TEXT = {
    "off": "Deleted only (timeout is set to 0)",
    "already": "Deleted (member was already timed out)",
    "forbidden": "Deleted; could not time out (bot lacks Moderate Members or the member is above the bot)",
    "failed": "Deleted; the timeout failed",
    "unavailable": "Deleted; moderation service unavailable",
}


def rules_from_config() -> SpamRules:
    """The .env defaults, used if the settings read fails."""
    return SpamRules(
        enabled=_config.ANTISPAM_ENABLED,
        flood_messages=_config.ANTISPAM_FLOOD_MESSAGES,
        flood_seconds=_config.ANTISPAM_FLOOD_SECONDS,
        duplicate_messages=_config.ANTISPAM_DUPLICATE_MESSAGES,
        duplicate_seconds=_config.ANTISPAM_DUPLICATE_SECONDS,
        mention_limit=_config.ANTISPAM_MENTION_LIMIT,
        timeout_minutes=_config.ANTISPAM_TIMEOUT_MINUTES,
    )


def mention_count(message, author_id: int | None = None) -> int:
    """Distinct users (not the author / invoking member) and roles mentioned in the text."""
    exclude = {message.author.id if author_id is None else author_id}
    users = set(getattr(message, "raw_mentions", []) or []) - exclude
    roles = set(getattr(message, "raw_role_mentions", []) or [])
    return len(users) + len(roles)


def describe_rules(rules: SpamRules) -> list[str]:
    def rule(count: int, text: str) -> str:
        return text if count > 0 else "off"

    action = (
        f"delete + {rules.timeout_minutes} minute timeout" if rules.timeout_minutes > 0 else "delete only"
    )
    return [
        f"Anti-spam: **{'on' if rules.enabled else 'off'}**",
        f"Flood: {rule(rules.flood_messages, f'{rules.flood_messages} messages in {rules.flood_seconds}s')}",
        f"Duplicates: {rule(rules.duplicate_messages, f'{rules.duplicate_messages} copies in {rules.duplicate_seconds}s')}",
        f"Mentions: {rule(rules.mention_limit, f'{rules.mention_limit} per message')}",
        f"Action: {action}",
    ]


class AntiSpam(commands.Cog):
    def __init__(self, bot: commands.Bot, settings, moderation_service, audit) -> None:
        self.bot = bot
        self._settings = settings
        self._moderation = moderation_service
        self._audit = audit
        self.detector = SpamDetector()
        self._exempt_cache: dict[int, tuple[float, bool]] = {}

    # ------------------------------------------------------------------
    # Settings / exemptions
    # ------------------------------------------------------------------
    async def rules(self, guild_id: int) -> SpamRules:
        cached = settings_module.get_cached(RULES_CACHE_KEY)
        if cached is not None:
            return cached
        try:
            rules = SpamRules.from_settings(await self._settings.get_effective(guild_id))
        except Exception:
            log.exception("anti_spam: could not read settings; using .env defaults")
            rules = rules_from_config()
        settings_module.set_cached(RULES_CACHE_KEY, rules)
        return rules

    def _always_exempt(self, member: discord.Member) -> bool:
        """Cheap, synchronous checks done before every message."""
        if member.id == getattr(member.guild, "owner_id", None):
            return True
        if self.bot.permission_service.is_staff(member):  # type: ignore[attr-defined]
            return True
        return bool(getattr(member.guild_permissions, "administrator", False))

    async def _granted_exempt(self, member: discord.Member) -> bool:
        """Bot-permission exemption (a DB lookup), only asked when a rule fires."""
        now = time.monotonic()
        hit = self._exempt_cache.get(member.id)
        if hit is not None and hit[0] > now:
            return hit[1]
        exempt = False
        for perm in EXEMPT_PERMISSIONS:
            if await self.bot.permission_service.has_permission(member, perm):  # type: ignore[attr-defined]
                exempt = True
                break
        self._exempt_cache = {k: v for k, v in self._exempt_cache.items() if v[0] > now}
        self._exempt_cache[member.id] = (now + EXEMPT_CACHE_SECONDS, exempt)
        return exempt

    # ------------------------------------------------------------------
    # Listener
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        # App replies carry a webhook_id too, so look for an invoking member first.
        via_app = app_source(message)
        if via_app is not None:
            if not via_app["user_installed"]:
                return  # the server's own bots answering a command
            member = message.guild.get_member(via_app["invoker"].id)
            if member is None:
                return
        else:
            via_app = None
            if message.author.bot or message.webhook_id is not None:
                return
            if message.type not in _CHECKED_TYPES or not isinstance(message.author, discord.Member):
                return
            member = message.author
        if self._always_exempt(member):
            return
        rules = await self.rules(message.guild.id)
        if not rules.enabled:
            return

        content = message_text(message) if via_app else message.content
        verdict = self.detector.observe(
            member.id, message.channel.id, message.id, content, mention_count(message, member.id), rules,
        )
        if verdict is None:
            return
        try:
            if await self._granted_exempt(member):
                self.detector.forget(member.id)
                return
            if verdict.first:
                await self._act(message, member, verdict, rules, via_app)
            else:
                await self.delete_messages(message.guild, verdict.messages)
        except Exception:
            log.exception("anti_spam: handling a %s verdict for %s failed", verdict.rule, member.id)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    async def delete_messages(self, guild: discord.Guild, pairs: list[tuple[int, int]]) -> int:
        """Delete (channel_id, message_id) pairs, bulk per channel. Returns how
        many were deleted (best effort; already-gone messages are skipped)."""
        by_channel: dict[int, list[int]] = {}
        for channel_id, message_id in pairs:
            by_channel.setdefault(channel_id, []).append(message_id)

        deleted = 0
        for channel_id, ids in by_channel.items():
            channel = guild.get_channel_or_thread(channel_id)
            if channel is None or not hasattr(channel, "delete_messages"):
                continue
            for start in range(0, len(ids), 100):
                chunk = ids[start:start + 100]
                try:
                    await channel.delete_messages([discord.Object(id=i) for i in chunk], reason=DELETE_REASON)
                    deleted += len(chunk)
                except discord.Forbidden:
                    log.warning("anti_spam: no permission to delete messages in channel %s", channel_id)
                    break
                except discord.HTTPException:
                    # A bulk delete fails as a whole (e.g. one message already
                    # gone); retry one by one so the rest still go.
                    for message_id in chunk:
                        try:
                            await channel.get_partial_message(message_id).delete()
                            deleted += 1
                        except discord.HTTPException:
                            pass
        return deleted

    async def _timeout(self, member: discord.Member, guild: discord.Guild, verdict: SpamVerdict, rules: SpamRules) -> str:
        if rules.timeout_minutes <= 0:
            return "off"
        if member.is_timed_out():
            return "already"
        if self._moderation is None:
            return "unavailable"
        try:
            await self._moderation.timeout(
                member, guild, guild.me, rules.timeout_minutes, f"Anti-spam: {verdict.detail}",
            )
            return "ok"
        except discord.Forbidden:
            return "forbidden"
        except (discord.HTTPException, ModerationInputError):
            log.exception("anti_spam: timing out %s failed", member.id)
            return "failed"

    async def _act(
        self, message: discord.Message, member: discord.Member, verdict: SpamVerdict, rules: SpamRules,
        via_app: dict | None = None,
    ) -> None:
        guild = message.guild
        deleted = await self.delete_messages(guild, verdict.messages)
        result = await self._timeout(member, guild, verdict, rules)
        channel_ids = sorted({c for c, _ in verdict.messages})
        log.info(
            "anti_spam: %s (%s) rule=%s deleted=%d timeout=%s", member, member.id, verdict.rule, deleted, result,
        )

        if self._audit is not None:
            try:
                await self._audit.log(
                    guild.id, guild.me.id, "spam_detected",
                    target_type="member", target_id=member.id,
                    detail={
                        "rule": verdict.rule, "detail": verdict.detail,
                        "channel_id": message.channel.id, "channels": len(channel_ids),
                        "messages_deleted": deleted,
                        "timeout_minutes": rules.timeout_minutes if result == "ok" else 0,
                        "timeout_result": result,
                        "via_app_id": via_app["app_id"] if via_app else None,
                        "via_app_name": via_app["app_name"] if via_app else None,
                    },
                )
            except Exception:
                log.exception("anti_spam: failed to write the audit row")

        where = message.channel.mention
        if len(channel_ids) > 1:
            where += f" and {len(channel_ids) - 1} other channel(s)"
        action = (
            f"Deleted and timed out for {rules.timeout_minutes} minute(s)" if result == "ok"
            else _TIMEOUT_RESULT_TEXT.get(result, result)
        )
        fields = {
            "Rule": verdict.detail,
            "Channel": where,
            "Messages Deleted": str(deleted),
            "Action": action,
        }
        if via_app:
            fields["Posted via app"] = discord.utils.escape_markdown(describe_app(via_app))
        text = message_text(message) if via_app else message.content
        sample = discord.utils.escape_markdown(text or "")[:SAMPLE_MAX]
        if sample:
            fields["Sample"] = sample
        embed = build_embed("spam_detected", member=member, extra_fields=fields)
        await route_and_post(self.bot, guild, "spam_detected", embed)

    # ------------------------------------------------------------------
    # !antispam [on|off]
    # ------------------------------------------------------------------
    @commands.command(name="antispam", hidden=True)
    async def antispam_cmd(self, ctx: commands.Context, state: str = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "manage_messages_bulk"):  # type: ignore[attr-defined]
            await ctx.reply("You don't have permission to use this command.")
            return

        if state is not None:
            value = {"on": True, "off": False}.get(state.strip().lower())
            if value is None:
                await ctx.reply("Usage: `!antispam` (status) or `!antispam on|off`")
                return
            if not self.bot.permission_service.is_staff(ctx.author):  # type: ignore[attr-defined]
                await ctx.reply("Only staff can turn anti-spam on or off (same as the dashboard Settings page).")
                return
            try:
                await self._settings.update(ctx.guild.id, {"antispam_enabled": value}, ctx.author.id, guild=ctx.guild)
            except Exception as exc:
                log.exception("anti_spam: toggle failed")
                await ctx.reply(f"Could not save the setting: {exc}")
                return

        rules = await self.rules(ctx.guild.id)
        lines = describe_rules(rules)
        lines.append("Never checked: staff, administrators, and members with `moderate_members` or `manage_messages_bulk`.")
        lines.append("Change the thresholds on the dashboard Settings page (Anti-spam tab).")
        await ctx.reply("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        log.warning("bot.db not available - anti_spam cog not loaded.")
        return

    from database.repositories.audit_repo import AuditRepository
    from database.repositories.mod_action_repo import ModActionRepository
    from database.repositories.settings_repo import SettingsRepository
    from services.audit_service import AuditService
    from services.moderation_service import ModerationService
    from services.settings_service import SettingsService

    audit = AuditService(AuditRepository(bot.db))
    # Reuse cogs/moderation.py's ModerationService (loaded earlier), same as the honeypot.
    moderation_service = getattr(bot.get_cog("ModerationCog"), "moderation_service", None)
    if moderation_service is None:
        moderation_service = ModerationService(ModActionRepository(bot.db), audit)
    settings = SettingsService(SettingsRepository(bot.db), audit=audit)
    await bot.add_cog(AntiSpam(bot, settings, moderation_service, audit))
