import discord
from discord.ext import commands, tasks
import os
import re
import sys
from datetime import datetime, timezone, timedelta
import asyncio
import logging
import tempfile
import config as _config

_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from database.repositories.audit_repo import AuditRepository
    from services.audit_service import AuditService
    _AUDIT_AVAILABLE = True
except ImportError:
    _AUDIT_AVAILABLE = False

try:
    from core.placeholder_engine import RenderContext
    from core.renderer import TemplateRenderer
    from database.repositories.embed_repo import EmbedRepository
    from database.repositories.template_repo import TemplateRepository
    from database.repositories.version_repo import VersionRepository
    from services.template_service import TemplateService
    _TEMPLATES_AVAILABLE = True
except ImportError:
    _TEMPLATES_AVAILABLE = False

from database.repositories.mod_action_repo import ModActionRepository
from services.moderation_service import (
    ModerationBlocked,
    ModerationInputError,
    ModerationService,
    reason_error,
)

_log = logging.getLogger(__name__)

# NOTE: _send_kick_log() below (template-driven, direct `mod-kick-log` lookup)
# is the single source of truth for posting a live kick log message — it's
# called unconditionally from do_kick(). If services/log_router.py's
# "member_kicked" -> "join_leave" category mapping is ever wired up with an
# actual route_and_post("member_kicked", ...) call somewhere, remove/skip it
# for kicks specifically, or this will double-post to the same channel.
MOD_KICK_LOG_TEMPLATE_NAME = "mod-kick-log"

UNVERIFIED_ROLE_ID = _config.UNVERIFIED_ROLE_ID
VERIFIED_ROLE_ID = _config.VERIFIED_ROLE_ID
LOG_CHANNEL_ID = _config.LOG_CHANNEL_ID
# Kicks are membership removals, so their live log goes to the join-leave
# channel (see services/log_router.py's "join_leave" category), not the
# general moderation-log channel that LOG_CHANNEL_ID points to.
JOIN_LEAVE_CHANNEL_ID = _config.JOIN_LEAVE_CHANNEL_ID
CHECK_INTERVAL = _config.CHECK_INTERVAL_MINUTES
AUTO_KICK = _config.AUTO_KICK

LOG_FILE = os.path.join(_DIR, "..", "kicks.log")

AUTO_KICK_REASON = "Unverified for more than 7 days"
MANUAL_KICK_DEFAULT_REASON = "Kicked by a moderator (no reason given)"

# Serializes every read-modify-write of kicks.log (counter bump + history
# line) so two kicks finishing together can't lose a count or interleave a
# rewrite with an append. All kicks.log writers in this process go through it.
_KICKS_LOG_LOCK = asyncio.Lock()


def ts() -> str:
    est = timezone(timedelta(hours=-5))
    return datetime.now(est).strftime("%Y-%m-%d %H:%M:%S EST")


def log(msg: str):
    _print_safe(f"[{ts()}] {msg}")


def _print_safe(line: str):
    try:
        print(line)
    except UnicodeEncodeError:
        # Windows console codepages (e.g. cp1252) can't encode all Unicode
        # (emoji, non-Latin scripts in guild/user names). Fall back instead
        # of letting this crash the background task loop.
        print(line.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))


# kicks.log format (unchanged): the FIRST line is `TOTAL_KICKS:N` (the
# counter), every other line is kick history. Writers below preserve every
# other line byte-for-byte and replace the file atomically (temp file in the
# same folder, then os.replace), so a crash mid-write can't truncate it.

def get_total_kicks(path: str | None = None) -> int:
    path = path or LOG_FILE
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        for line in f:
            if line.startswith("TOTAL_KICKS:"):
                # A corrupt counter raises (as before) instead of silently
                # restarting the count at 1 and overwriting the real total.
                return int(line.strip().split(":", 1)[1])
    return 0


def _atomic_write(path: str, text: str) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".kicks.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_kicks_log(count: int, entry: str | None = None, path: str | None = None) -> None:
    """Set the counter to `count` and optionally append one history `entry`
    (a full line), in ONE atomic replace. Every other line is kept exactly."""
    path = path or LOG_FILE
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            lines = f.readlines()
    for i, line in enumerate(lines):
        if line.startswith("TOTAL_KICKS:"):
            ending = "\r\n" if line.endswith("\r\n") else "\n"
            lines[i] = f"TOTAL_KICKS:{count}{ending}"
            break
    else:
        lines.insert(0, f"TOTAL_KICKS:{count}\n")
    if entry is not None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(entry if entry.endswith("\n") else entry + "\n")
    _atomic_write(path, "".join(lines))


def save_total_kicks(count: int, path: str | None = None) -> None:
    write_kicks_log(count, None, path)


def format_kick_entry(member: discord.Member, guild: discord.Guild, total: int) -> str:
    roles = ", ".join(r.name for r in member.roles if r.name != "@everyone") or "None"
    joined = member.joined_at.strftime('%Y-%m-%d %H:%M:%S UTC') if member.joined_at else "unknown"
    return (
        f"[{ts()}] KICK #{total} | "
        f"User: {member} ({member.id}) | "
        f"Guild: {guild.name} | "
        f"Joined: {joined} | "
        f"Roles: {roles}\n"
    )


async def next_kick_number(path: str | None = None) -> int:
    """Reserve the next kick number: bump the counter under the lock and
    return the new total."""
    async with _KICKS_LOG_LOCK:
        total = get_total_kicks(path) + 1
        write_kicks_log(total, None, path)
        return total


async def record_kick(entry: str, path: str | None = None) -> None:
    """Append one history line under the lock (counter left as it is)."""
    async with _KICKS_LOG_LOCK:
        write_kicks_log(get_total_kicks(path), entry, path)


async def release_kick_number(total: int, path: str | None = None) -> None:
    """Undo next_kick_number() when the kick did not happen, but only if no
    other kick has bumped the counter since."""
    async with _KICKS_LOG_LOCK:
        if get_total_kicks(path) == total:
            write_kicks_log(total - 1, None, path)


def build_kick_embed(
    member: discord.Member, total: int, *, reason: str = AUTO_KICK_REASON, moderator_id: int | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Member Kicked  |  Total Kicks: {total}",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="User", value=f"<@{member.id}>", inline=True)
    embed.add_field(name="User ID", value=str(member.id), inline=True)
    if moderator_id is not None:
        embed.add_field(name="Moderator", value=f"<@{moderator_id}>", inline=True)
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    if member.joined_at is not None:
        embed.add_field(name="Joined At", value=f"<t:{int(member.joined_at.timestamp())}:F>", inline=True)
    roles = [r.mention for r in member.roles if r.name != "@everyone"]
    embed.add_field(name="Roles", value=" ".join(roles) if roles else "None", inline=False)
    embed.set_footer(text=f"Kicked from {member.guild.name}")
    return embed


class KickCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._audit: "AuditService | None" = None
        self._templates: "TemplateService | None" = None
        self._renderer: "TemplateRenderer | None" = None
        self.moderation_service: "ModerationService | None" = None
        if hasattr(bot, "db") and bot.db is not None:
            if _AUDIT_AVAILABLE:
                self._audit = AuditService(AuditRepository(bot.db))
            if _TEMPLATES_AVAILABLE:
                self._templates = TemplateService(
                    TemplateRepository(bot.db), EmbedRepository(bot.db),
                    VersionRepository(bot.db), AuditRepository(bot.db),
                )
                self._renderer = TemplateRenderer()
            self.moderation_service = ModerationService(
                ModActionRepository(bot.db), self._audit, self._templates, self._renderer,
            )
        self._loop_restarts = 0
        self._unloaded = False
        self._restart_task: asyncio.Task | None = None
        if AUTO_KICK:
            self.check_unverified.start()

    def cog_unload(self):
        self._unloaded = True
        if self._restart_task is not None:
            self._restart_task.cancel()
        self.check_unverified.cancel()

    async def do_kick(
        self,
        member: discord.Member,
        guild: discord.Guild,
        log_channel: discord.TextChannel | None,
        *,
        actor_id: int | None = None,
        reason: str | None = None,
    ):
        """Kick `member` and record it (kicks.log counter + history line,
        mod_actions, audit_log, live kick log).

        actor_id None = the auto-kick loop (reason AUTO_KICK_REASON, bot as
        actor, no rank check: Discord enforces the bot's own rank). A manual
        `!kick` passes the moderator's id and their own reason; it is checked
        against services/hierarchy first and logged with source "manual"."""
        manual = actor_id is not None
        actor_member = guild.get_member(actor_id) if manual else None
        actor = actor_member or self.bot.user
        kick_reason = (reason or MANUAL_KICK_DEFAULT_REASON) if manual else AUTO_KICK_REASON
        if manual:
            if self.moderation_service is None:
                # Fail closed: a human kick is never done without the rank check.
                raise ModerationBlocked("Moderation system is unavailable.")
            # Rank check BEFORE a kick number is reserved.
            self.moderation_service.ensure_allowed(guild, actor_member or discord.Object(id=actor_id), member)

        total = await next_kick_number()
        try:
            if self.moderation_service is not None:
                await self.moderation_service.kick(
                    member, guild, actor, kick_reason,
                    log_channel_id=log_channel.id if log_channel else None,
                    fallback_embed=build_kick_embed(
                        member, total, reason=kick_reason, moderator_id=actor_id if manual else None,
                    ),
                    audit_detail={
                        "reason": kick_reason, "total_kicks": total,
                        "source": "manual" if manual else "auto_unverified",
                    },
                    log_extra={"kick_number": total, "source": "command" if manual else "automatic"},
                )
            else:
                # moderation_service unavailable (no bot.db): only the
                # auto-kick loop gets here (manual kicks are refused above);
                # direct kick with no DB writes.
                await member.kick(reason=kick_reason)
        except BaseException:
            await release_kick_number(total)
            raise

        entry = format_kick_entry(member, guild, total)
        await record_kick(entry)
        _print_safe(entry.strip())

    @tasks.loop(minutes=CHECK_INTERVAL)
    async def check_unverified(self):
        # Any unexpected error in one guild/member is logged and skipped so
        # the loop keeps running; check_unverified.error below restarts the
        # loop if something still escapes.
        try:
            await self._run_unverified_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("auto-kick: unverified check iteration failed")
            log("  Unverified check failed with an unexpected error (see bot log); will retry next interval.")

    @check_unverified.error
    async def check_unverified_error(self, error: BaseException):
        # Called from inside the dying loop task, so the restart is scheduled
        # in a separate task that waits for the old one to finish first.
        _log.error("auto-kick: loop stopped by %r; scheduling a restart", error, exc_info=error)
        self._loop_restarts += 1
        if self._loop_restarts > 20 or self._unloaded:
            _log.error("auto-kick: loop failed %d times; not restarting until the bot restarts", self._loop_restarts)
            return
        delay = min(60, 5 * self._loop_restarts)
        self._restart_task = asyncio.get_running_loop().create_task(self._restart_loop_later(delay))

    async def _restart_loop_later(self, delay: float):
        await asyncio.sleep(delay)
        for _ in range(100):
            if not self.check_unverified.is_running():
                break
            await asyncio.sleep(0.1)
        if not self._unloaded and not self.check_unverified.is_running():
            log("Auto-kick loop restarted after an error.")
            self.check_unverified.start()

    async def _run_unverified_check(self):
        log("--- Running unverified check ---")
        for guild in self.bot.guilds:
            unverified_role = guild.get_role(UNVERIFIED_ROLE_ID)
            if unverified_role is None:
                log(f"[{guild.name}] Unverified role not found (ID {UNVERIFIED_ROLE_ID})")
                continue

            log_channel = guild.get_channel(JOIN_LEAVE_CHANNEL_ID)
            if log_channel is None:
                log(f"[{guild.name}] Join-leave log channel not found (ID {JOIN_LEAVE_CHANNEL_ID})")

            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            verified_role = guild.get_role(VERIFIED_ROLE_ID)
            candidates = unverified_role.members
            log(f"[{guild.name}] {len(candidates)} member(s) with unverified role")

            skipped_verified = 0
            skipped_too_new = 0
            kicked = 0

            for member in candidates:
                if member.joined_at is None:
                    log(f"  Skipping {member} ({member.id}) — no join date")
                    continue
                if verified_role and verified_role in member.roles:
                    skipped_verified += 1
                    continue
                if member.joined_at > cutoff:
                    skipped_too_new += 1
                    continue
                log(f"  Kicking {member} ({member.id}) — joined {member.joined_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                try:
                    await self.do_kick(member, guild, log_channel)
                    kicked += 1
                except discord.RateLimited as e:
                    log(f"  Rate limited — retry after {e.retry_after:.2f}s. Pausing.")
                    await asyncio.sleep(e.retry_after)
                except discord.Forbidden as e:
                    log(f"  FORBIDDEN kicking {member} ({member.id}): code={e.code} status={e.status} text={e.text}")
                except discord.HTTPException as e:
                    log(f"  HTTP error kicking {member} ({member.id}): code={e.code} status={e.status} text={e.text}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    _log.exception("auto-kick: unexpected error kicking %s", member.id)
                    log(f"  Unexpected error kicking {member} ({member.id}): {type(e).__name__}: {e}")

            log(f"[{guild.name}] Done — kicked: {kicked} | skipped (verified): {skipped_verified} | skipped (too new): {skipped_too_new}")

        log("--- Check complete ---")

    @check_unverified.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    @commands.command(name="kick", hidden=True)
    async def kick_cmd(self, ctx: commands.Context, target: str = None, *, reason: str = None):
        if ctx.guild is None or not await self.bot.permission_service.has_permission(ctx.author, "kick_members"):
            await ctx.reply("You don't have permission to use this command.")
            return

        if target is None:
            await ctx.reply("Usage: `!kick <@mention or user ID> [reason]`")
            return

        if reason_error(reason):
            await ctx.reply(reason_error(reason))
            return

        match = re.match(r"<@!?(\d+)>", target.strip())
        raw_id = match.group(1) if match else (target.strip() if target.strip().isdigit() else None)

        if raw_id is None:
            await ctx.reply("Invalid user. Provide a mention or user ID.")
            return

        member = ctx.guild.get_member(int(raw_id)) if ctx.guild else None
        if member is None:
            await ctx.reply("Could not find that member in this server.")
            return

        log_channel = ctx.guild.get_channel(JOIN_LEAVE_CHANNEL_ID) if ctx.guild else None

        try:
            await self.do_kick(member, ctx.guild, log_channel, actor_id=ctx.author.id, reason=reason)
            await ctx.reply(f"Kicked <@{member.id}>.")
        except (ModerationBlocked, ModerationInputError) as e:
            await ctx.reply(str(e))
        except discord.Forbidden as e:
            log(f"FORBIDDEN kicking {member} ({member.id}): code={e.code} status={e.status} text={e.text}")
            await ctx.reply(f"Missing permissions to kick that member. (code {e.code})")
        except discord.HTTPException as e:
            log(f"HTTP error kicking {member} ({member.id}): code={e.code} status={e.status} text={e.text}")
            await ctx.reply(f"Failed to kick: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(KickCog(bot))
