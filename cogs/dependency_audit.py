"""
cogs/dependency_audit.py — weekly dependency audit (services/dependency_audit_service.py).

Every Monday at RUN_HOUR local time (and, if the bot was down then, at the next
check after it is overdue), runs pip-audit on the bot's venv and `npm audit` on
the dashboard frontend and the PO-token server. When anything needs updating
(a vulnerability, an outdated yt-dlp, or a check that could not run) it posts
one embed to DEPENDENCY_AUDIT_WEBHOOK_URL, else SUPERVISOR_WEBHOOK_URL (the
crash-alert webhook). A clean week posts nothing.

The last run is remembered as an `audit_log` row `dependency_audit_run` (detail:
counts, errors), so restarts never skip or repeat a week (same pattern as the
weekly stale-ticket sweep).

`!depaudit` (staff only): run it now, reply with the summary, and post to the
webhook when something needs action.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import sys

import discord
from discord.ext import commands, tasks

from services import dependency_audit_service as svc

log = logging.getLogger(__name__)

RUN_WEEKDAY = 0  # Monday
RUN_HOUR = 5  # local time; log rotation runs at 04:00
INTERVAL = _dt.timedelta(days=7)


def _home_guild_id() -> int | None:
    return getattr(sys.modules.get("__main__"), "ALLOWED_GUILD_ID", None)


def _parse_utc(value: object) -> _dt.datetime | None:
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def is_due(last_run: _dt.datetime | None, now_local: _dt.datetime) -> bool:
    """Due at/after this week's Monday RUN_HOUR slot unless that slot already ran,
    or whenever the last run is older than a week (catch-up)."""
    slot = (now_local - _dt.timedelta(days=(now_local.weekday() - RUN_WEEKDAY) % 7)).replace(
        hour=RUN_HOUR, minute=0, second=0, microsecond=0)
    if slot > now_local:
        slot -= INTERVAL
    if last_run is None:
        return True
    return last_run < slot  # both aware: compared as instants


class DependencyAudit(commands.Cog):
    def __init__(self, bot: commands.Bot, audit=None) -> None:
        self.bot = bot
        self.audit = audit
        self._lock = asyncio.Lock()
        self.scheduler.start()

    def cog_unload(self) -> None:
        self.scheduler.cancel()

    async def _last_run(self, guild_id: int) -> _dt.datetime | None:
        if self.audit is None:
            return None
        page = await self.audit.query(guild_id, action=svc.AUDIT_ACTION, limit=1)
        entries = page.get("entries") or []
        return _parse_utc(entries[0].get("created_at")) if entries else None

    async def run(self, *, source: str) -> tuple[svc.AuditReport, bool]:
        """Run every check, record it, post to the webhook when needed."""
        async with self._lock:
            report = await svc.run_all()
            posted = await svc.send_webhook(report) if report.needs_action else False
            guild_id = _home_guild_id()
            if self.audit is not None and guild_id and self.bot.user is not None:
                await self.audit.log(
                    guild_id, self.bot.user.id, svc.AUDIT_ACTION, target_type="system",
                    detail={**report.summary(), "source": source, "webhook_posted": posted},
                )
            log.info("dependency audit (%s): %s, webhook=%s", source, report.summary(), posted)
            return report, posted

    @tasks.loop(minutes=30)
    async def scheduler(self) -> None:
        try:
            guild_id = _home_guild_id()
            if not guild_id or self._lock.locked():
                return
            if is_due(await self._last_run(guild_id), _dt.datetime.now().astimezone()):
                await self.run(source="weekly")
        except Exception:
            log.exception("dependency audit: scheduled run failed")

    @scheduler.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(120)  # let startup settle first

    @commands.command(name="depaudit", hidden=True)
    async def depaudit(self, ctx: commands.Context) -> None:
        """Run the dependency audit now (staff only)."""
        if ctx.guild is None or not self.bot.permission_service.is_staff(ctx.author):  # type: ignore[attr-defined]
            return
        if self._lock.locked():
            await ctx.reply("An audit is already running.", mention_author=False)
            return
        msg = await ctx.reply("Running pip-audit and npm audit (up to a few minutes)...", mention_author=False)
        report, posted = await self.run(source=f"command:{ctx.author.id}")
        if not report.needs_action:
            text = f"All clear: no known vulnerabilities ({', '.join(report.checked) or 'nothing checked'})."
        else:
            title, body, _ = svc.build_report_text(report, limit=1700)
            text = f"**{title}**\n{body}\n" + ("Posted to the webhook." if posted else "No webhook configured or it failed.")
        await msg.edit(content=text[:2000], allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    audit = None
    if getattr(bot, "db", None) is not None:
        from database.repositories.audit_repo import AuditRepository
        from services.audit_service import AuditService

        audit = AuditService(AuditRepository(bot.db))
    await bot.add_cog(DependencyAudit(bot, audit))
