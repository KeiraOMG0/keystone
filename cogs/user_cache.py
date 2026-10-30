"""
cogs/user_cache.py — keeps services/user_cache_service.py's own SQLite file
(config.USER_CACHE_PATH) up to date so the dashboard can render members who
left, were kicked or banned (internal_api/routes/resolve.py falls back to it).

Population (all writes buffered, flushed every FLUSH_SECONDS in one
transaction; listeners never raise and never await Discord on the hot path):
  - startup (first run of daily_loop, after the bot is ready) and every 24 h:
    prune past the retention, then sweep every cached member of the home
    guild (chunked executemany; members still marked 'member' but gone are
    marked left with departure_source 'detected_at_startup'), then the ban
    list (banned users get a row; ones no longer banned become 'unbanned').
  - on_member_join / on_member_update (name, nick, avatar, colour changes) /
    on_user_update (username, global name, avatar).
  - on_member_remove -> 'left', then a background task reads the audit log:
    a fresh kick entry for this user upgrades it to 'kicked' with the
    moderator. When the entry's actor is this bot (a !kick, dashboard kick,
    auto-kick or honeypot kick all go through ModerationService), the human
    moderator is taken from the newest matching mod_actions row; moderator ==
    bot means departure_source 'bot'. Same matching rules as
    cogs/logging_moderation.py (pick_actor_entry). Needs View Audit Log;
    without it a kick stays 'left'.
  - on_member_ban -> 'banned' (+ the same attribution); on_member_unban ->
    'unbanned'.
Reasons are never stored.

`!usercache` (staff only: owner / STAFF_ROLE_IDS):
  !usercache                     status (rows by status, file, retention)
  !usercache forget <@user|ID>   delete that user's row now (audited as
                                 `user_cache_forget`); a current member is
                                 re-added by the next event or sweep.

Turn it off with USER_CACHE_ENABLED=false (the cog is not added; resolve then
answers null for departed users as before; delete the file to drop it).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import config as _config
from services import user_cache_service as ucs
from services.user_cache_service import FLUSH_MAX_OPS, UserCacheStore

log = logging.getLogger(__name__)

FLUSH_SECONDS = 10
AUDIT_FIRST_DELAY = 1.5  # seconds before the first audit-log read after a remove/ban
AUDIT_RETRY_DELAY = 2.0
AUDIT_LOOKBACK = 10
ACTOR_MATCH_WINDOW = 30.0
MOD_ACTION_WINDOW = 120.0  # seconds between a mod_actions row and the departure
BAN_SWEEP_LIMIT = 10000
_USER_RE = re.compile(r"^<@!?(\d+)>$")


def home_guild_id() -> int | None:
    """main.py's ALLOWED_GUILD_ID (None when not running under main.py)."""
    value = getattr(sys.modules.get("__main__"), "ALLOWED_GUILD_ID", None)
    return value if isinstance(value, int) else None


def _profile_sig(obj) -> tuple:
    colour = getattr(obj, "color", None)
    avatar = getattr(obj, "display_avatar", None)
    return (
        getattr(obj, "name", None), getattr(obj, "global_name", None), getattr(obj, "nick", None),
        getattr(avatar, "key", None), getattr(colour, "value", 0), getattr(obj, "bot", False),
    )


def _parse_sqlite_ts(value) -> datetime | None:
    """mod_actions.created_at is CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS', UTC)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class UserCache(commands.Cog):
    def __init__(self, bot: commands.Bot, store: UserCacheStore, mod_actions=None, audit=None) -> None:
        self.bot = bot
        self.store = store
        self._mod_actions = mod_actions
        self._audit = audit
        self._tasks: set[asyncio.Task] = set()

    # -- lifecycle ---------------------------------------------------------
    async def cog_load(self) -> None:
        await self.store.open()
        ucs.set_active(self.store)
        self.flush_loop.start()
        self.daily_loop.start()
        log.info(
            "user cache: %s, departed users kept %s",
            os.path.abspath(self.store.path),
            f"{self.store.retention_days} day(s)" if self.store.retention_days > 0 else "forever",
        )

    async def cog_unload(self) -> None:
        self.flush_loop.cancel()
        self.daily_loop.cancel()
        for task in list(self._tasks):
            task.cancel()
        ucs.set_active(None)
        await self.store.close()

    def _ours(self, guild) -> bool:
        home = home_guild_id()
        return guild is not None and (home is None or guild.id == home)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _maybe_flush(self) -> None:
        if self.store.pending() >= FLUSH_MAX_OPS:
            self._spawn(self.store.flush())

    @tasks.loop(seconds=FLUSH_SECONDS)
    async def flush_loop(self) -> None:
        await self.store.flush()

    @tasks.loop(hours=24)
    async def daily_loop(self) -> None:
        # First run right after the bot is ready (= the startup sweep).
        try:
            removed = await self.store.prune()
            if removed:
                log.info("user cache: pruned %d departed user(s) past %d day(s)", removed, self.store.retention_days)
        except Exception:
            log.exception("user cache: prune failed")
        for guild in list(self.bot.guilds):
            if self._ours(guild):
                await self.sweep(guild)

    @daily_loop.before_loop
    async def _before_daily(self) -> None:
        await self.bot.wait_until_ready()

    async def sweep(self, guild: discord.Guild) -> None:
        try:
            result = await self.store.sweep_members(list(guild.members), complete=bool(guild.chunked))
            log.info(
                "user cache: swept %d member(s), %d marked left while offline", result["members"], result["marked_left"],
            )
        except Exception:
            log.exception("user cache: member sweep failed")
        try:
            users, complete = [], True
            async for entry in guild.bans(limit=BAN_SWEEP_LIMIT):
                users.append(entry.user)
            if len(users) >= BAN_SWEEP_LIMIT:
                complete = False
            result = await self.store.sweep_bans(users, complete=complete)
            log.info("user cache: %d banned user(s), %d unbanned while offline", result["banned"], result["marked_unbanned"])
        except discord.Forbidden:
            log.info("user cache: no Ban Members permission; ban list not swept")
        except Exception:
            log.exception("user cache: ban sweep failed")

    # -- listeners (never raise) ------------------------------------------
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self._ours(member.guild):
            return
        try:
            self.store.upsert_member(member)
            self._maybe_flush()
        except Exception:
            log.exception("user cache: join of %s not recorded", member.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self._ours(after.guild):
            return
        try:
            if _profile_sig(before) != _profile_sig(after):
                self.store.upsert_member(after)
                self._maybe_flush()
        except Exception:
            log.exception("user cache: update of %s not recorded", after.id)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        try:
            if _profile_sig(before) == _profile_sig(after):
                return
            for guild in self.bot.guilds:
                if self._ours(guild):
                    member = guild.get_member(after.id)
                    if member is not None:
                        self.store.upsert_member(member)
                        self._maybe_flush()
        except Exception:
            log.exception("user cache: user update of %s not recorded", after.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not self._ours(member.guild):
            return
        try:
            self.store.touch_profile(member, insert_status="left")
            self.store.departed(member.id, "left")
            self._maybe_flush()
            self._spawn(self._attribute(member.guild, member.id, "kicked"))
        except Exception:
            log.exception("user cache: departure of %s not recorded", member.id)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user) -> None:
        if not self._ours(guild):
            return
        try:
            self.store.touch_profile(user, insert_status="banned")
            self.store.departed(user.id, "banned")
            self._maybe_flush()
            self._spawn(self._attribute(guild, user.id, "banned"))
        except Exception:
            log.exception("user cache: ban of %s not recorded", user.id)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        if not self._ours(guild):
            return
        try:
            self.store.touch_profile(user, insert_status="unbanned")
            self.store.unbanned(user.id)
            self._maybe_flush()
        except Exception:
            log.exception("user cache: unban of %s not recorded", user.id)

    # -- kick / ban attribution -------------------------------------------
    async def _attribute(self, guild: discord.Guild, user_id: int, status: str) -> None:
        """Background: who kicked/banned `user_id` (see module docstring)."""
        try:
            from cogs.logging_moderation import pick_actor_entry

            action = discord.AuditLogAction.kick if status == "kicked" else discord.AuditLogAction.ban
            entry = None
            await asyncio.sleep(AUDIT_FIRST_DELAY)
            for attempt in range(2):
                try:
                    entries = [e async for e in guild.audit_logs(action=action, limit=AUDIT_LOOKBACK)]
                except (discord.Forbidden, discord.HTTPException):
                    entries = []
                entry = pick_actor_entry(entries, user_id, datetime.now(timezone.utc), window=ACTOR_MATCH_WINDOW)
                if entry is not None:
                    break
                if attempt == 0:
                    await asyncio.sleep(AUDIT_RETRY_DELAY)

            bot_id = self.bot.user.id if self.bot.user else None
            moderator_id = entry.user.id if entry is not None else None
            if moderator_id is None or moderator_id == bot_id:
                # Our own action (or no audit access): the mod_actions row has the human.
                row = await self._recent_mod_action(guild.id, user_id, "kick" if status == "kicked" else "ban")
                if row is not None:
                    moderator_id = int(row["moderator_id"])
            if moderator_id is None:
                return  # a kick nobody can see stays 'left'; a ban stays unattributed
            source = "bot" if moderator_id == bot_id else "moderator"
            self.store.attribute(user_id, status, moderator_id, source)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("user cache: could not attribute %s of %s", status, user_id)

    async def _recent_mod_action(self, guild_id: int, user_id: int, action_type: str) -> dict | None:
        if self._mod_actions is None:
            return None
        for attempt in range(2):
            try:
                rows = await self._mod_actions.get_actions(guild_id, user_id, limit=3)
            except Exception:
                return None
            now = datetime.now(timezone.utc)
            for row in rows:
                created = _parse_sqlite_ts(row.get("created_at"))
                if (
                    row.get("action_type") == action_type
                    and created is not None
                    and abs((now - created).total_seconds()) <= MOD_ACTION_WINDOW
                ):
                    return row
            if attempt == 0:
                await asyncio.sleep(AUDIT_RETRY_DELAY)
        return None

    # -- !usercache --------------------------------------------------------
    @commands.command(name="usercache", hidden=True)
    async def usercache_cmd(self, ctx: commands.Context, action: str = None, target: str = None):
        if ctx.guild is None or not self.bot.permission_service.is_staff(ctx.author):  # type: ignore[attr-defined]
            await ctx.reply("You don't have permission to use this command.")
            return
        if action is None:
            s = await self.store.stats()
            size_kb = os.path.getsize(self.store.path) // 1024 if os.path.exists(self.store.path) else 0
            parts = ", ".join(f"{n} {k}" for k, n in sorted(s["by_status"].items())) or "empty"
            keep = f"{self.store.retention_days} day(s)" if self.store.retention_days > 0 else "forever"
            await ctx.reply(
                f"User cache: **{s['total']}** user(s) ({parts}). Departed users kept {keep}, pruned daily. "
                f"File: `{os.path.basename(self.store.path)}` ({size_kb} KB).\n"
                "Usage: `!usercache forget <@user or ID>` deletes one user's cached profile."
            )
            return
        if action.lower() != "forget" or not target:
            await ctx.reply("Usage: `!usercache` (status) or `!usercache forget <@user or ID>`.")
            return
        m = _USER_RE.match(target.strip())
        raw = m.group(1) if m else target.strip()
        if not raw.isdigit():
            await ctx.reply("Give a user mention or ID.")
            return
        removed = await self.store.forget(int(raw))
        if self._audit is not None:
            try:
                await self._audit.log(
                    ctx.guild.id, ctx.author.id, "user_cache_forget", target_type="user", target_id=int(raw),
                    detail={"removed": removed, "source": "command"},
                )
            except Exception:
                log.exception("user cache: audit row for forget failed")
        await ctx.reply(
            f"Forgot the cached profile of `{raw}`." if removed else f"No cached profile for `{raw}`.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    if not _config.USER_CACHE_ENABLED:
        log.info("user cache: disabled (USER_CACHE_ENABLED=false)")
        return
    mod_actions = audit = None
    if getattr(bot, "db", None) is not None:
        from database.repositories.audit_repo import AuditRepository
        from database.repositories.mod_action_repo import ModActionRepository
        from services.audit_service import AuditService

        mod_actions = ModActionRepository(bot.db)
        audit = AuditService(AuditRepository(bot.db))
    store = UserCacheStore(_config.USER_CACHE_PATH, _config.USER_CACHE_RETENTION_DAYS)
    await bot.add_cog(UserCache(bot, store, mod_actions, audit))
