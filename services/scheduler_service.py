from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord

from core.placeholder_engine import RenderContext
from core.renderer import TemplateRenderer
from database.repositories.advanced_repo import ScheduleRepository
from database.repositories.template_repo import TemplateRepository
from services.live_events import publish as live_publish

log = logging.getLogger(__name__)

_TICK_INTERVAL = 30  # seconds
# Stored next_run_at format (UTC, naive) - also what list_due compares against.
_DB_FMT = "%Y-%m-%d %H:%M:%S"
MAX_CRON_LENGTH = 100
MAX_LABEL_LENGTH = 100


def _parse_cron(expr: str, after: datetime) -> datetime | None:
    """Return the next datetime after `after` for a cron expression, or None on error."""
    try:
        from croniter import croniter
        it = croniter(expr, after)
        return it.get_next(datetime)
    except Exception:
        return None


def next_runs(expr: str, count: int = 5, after: datetime | None = None) -> list[datetime] | None:
    """The next `count` UTC run times of a cron expression (same parser the
    scheduler uses), or None if the expression is invalid. Used by the
    dashboard's cron preview and schedule list."""
    if not isinstance(expr, str) or not expr.strip() or len(expr) > MAX_CRON_LENGTH:
        return None
    start = after or _now_dt()
    first = _parse_cron(expr, start)
    if first is None:
        return None
    out = [first]
    try:
        from croniter import croniter
        it = croniter(expr, first)
        while len(out) < count:
            out.append(it.get_next(datetime))
    except Exception:
        return out
    return out


def _publish(action: str, **data) -> None:
    """Live `automation` topic (dashboard Automation page). Never raises."""
    live_publish("automation", {"section": "schedules", "action": action, **data})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


class SchedulerService:
    def __init__(
        self,
        schedule_repo: ScheduleRepository,
        template_repo: TemplateRepository,
        renderer: TemplateRenderer,
        bot: discord.Client,
        audit=None,
    ) -> None:
        self._schedules = schedule_repo
        self._templates = template_repo
        self._renderer = renderer
        self._bot = bot
        # services.audit_service.AuditService | None (shared by /schedule and
        # the dashboard Automation page, so rows are written here once).
        self._audit = audit
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="scheduler-loop")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        await self._bot.wait_until_ready()
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("scheduler: unhandled error in tick")
            await asyncio.sleep(_TICK_INTERVAL)

    async def _tick(self) -> None:
        due = await self._schedules.list_due(_now_iso())
        for record in due:
            await self._fire(record)

    async def _fire(self, record: dict) -> None:
        guild = self._bot.get_guild(record["guild_id"])
        if guild is None:
            return

        channel = guild.get_channel(record["channel_id"])
        if channel is None:
            log.warning("scheduler: channel %d not found for schedule %d", record["channel_id"], record["id"])
            self._advance(record)
            return

        template_full = await self._templates.get_full(record["template_id"])
        if not template_full:
            log.warning("scheduler: template %d not found for schedule %d", record["template_id"], record["id"])
            self._advance(record)
            return

        ctx = RenderContext(guild=guild, member=None, channel=channel)
        try:
            rendered = self._renderer.render_message(template_full, ctx)
        except Exception:
            log.exception("scheduler: render error for schedule %d", record["id"])
            self._advance(record)
            return

        content = rendered.get("content") or None
        embeds = rendered.get("embeds") or []

        if content or embeds:
            try:
                await channel.send(content=content, embeds=embeds[:10])
            except discord.HTTPException:
                log.exception("scheduler: send failed for schedule %d", record["id"])

        self._advance(record)

    def _advance(self, record: dict) -> None:
        next_dt = _parse_cron(record["cron_expr"], _now_dt())
        if next_dt:
            next_iso = next_dt.strftime(_DB_FMT)
            asyncio.create_task(self._advance_and_publish(record["id"], next_iso))

    async def _advance_and_publish(self, schedule_id: int, next_iso: str) -> None:
        await self._schedules.set_next_run(schedule_id, next_iso)
        _publish("ran", schedule_id=schedule_id)

    async def _audit_log(self, guild_id: int, actor_id: int | None, action: str, target_id, detail: dict) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.log(guild_id, actor_id or 0, action, "schedule", target_id, detail)
        except Exception:  # the change already happened; never fail it over the audit row
            log.exception("scheduler: could not write %s audit row", action)

    # ------------------------------------------------------------------
    # Management API
    # ------------------------------------------------------------------

    async def add(
        self,
        guild_id: int,
        channel_id: int,
        template_name: str,
        cron_expr: str,
        created_by: int,
        label: str | None = None,
    ) -> tuple[int | None, list[str]]:
        # Validate cron expression
        next_dt = _parse_cron(cron_expr, _now_dt()) if len(cron_expr or "") <= MAX_CRON_LENGTH else None
        if next_dt is None:
            return (None, [f"Invalid cron expression: `{cron_expr}`. Example: `0 9 * * 1` (every Monday 9am UTC)."])
        if label is not None and len(label) > MAX_LABEL_LENGTH:
            return (None, [f"Label must be {MAX_LABEL_LENGTH} characters or fewer."])

        template = await self._templates.get_by_name(guild_id, template_name)
        if not template:
            return (None, [f"No template named '{template_name}' found."])

        next_iso = next_dt.strftime(_DB_FMT)
        schedule_id = await self._schedules.create(
            guild_id=guild_id,
            channel_id=channel_id,
            template_id=template["id"],
            cron_expr=cron_expr,
            created_by=created_by,
            label=label,
            next_run_at=next_iso,
        )
        await self._audit_log(guild_id, created_by, "schedule_add", schedule_id, {
            "template": template_name, "channel_id": str(channel_id), "cron": cron_expr, "label": label,
        })
        _publish("add", schedule_id=schedule_id)
        return (schedule_id, [])

    async def remove(self, guild_id: int, schedule_id: int, actor_id: int | None = None) -> bool:
        removed = await self._schedules.delete(guild_id, schedule_id)
        if removed:
            await self._audit_log(guild_id, actor_id, "schedule_remove", schedule_id, {})
            _publish("remove", schedule_id=schedule_id)
        return removed

    async def toggle(self, guild_id: int, schedule_id: int, enabled: bool, actor_id: int | None = None) -> bool:
        # Guild-scoped: a schedule of another guild is "not found".
        row = await self._schedules.get(schedule_id)
        if not row or row["guild_id"] != guild_id:
            return False
        ok = await self._schedules.set_enabled(schedule_id, enabled)
        if ok:
            await self._audit_log(guild_id, actor_id, "schedule_toggle", schedule_id, {"enabled": bool(enabled)})
            _publish("toggle", schedule_id=schedule_id)
        return ok

    async def list_schedules(self, guild_id: int) -> list[dict]:
        return await self._schedules.list_for_guild(guild_id)
