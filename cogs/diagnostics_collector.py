"""
Passive, in-memory metrics collector for the dashboard Diagnostics page.

Mostly passive: it LISTENS to events other cogs already trigger
(command completion/errors, interactions, gateway connect/resume) and runs one
tiny sampler loop. Exception: its on_command_error listener is also the bot's
prefix-command error handler (core/command_errors.py), because registering
any such listener switches off discord.py's default one. It registers no commands, writes nothing to disk/DB and
holds no user content (command names and exception TYPE names only).

Exposed to services/diagnostics_service.py as `bot.diagnostics_collector`.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time

from discord.ext import commands, tasks

from core.command_errors import handle_command_error
from services.psutil_guard import serialized

log = logging.getLogger(__name__)

SAMPLE_SECONDS = 10
HISTORY_LEN = 180  # 30 minutes at 10s


@serialized
def _sample_process(ds) -> dict:
    p = ds.get_proc()
    return {"cpu": ds.cpu_pct(p), "rss": p.memory_info().rss, "threads": p.num_threads()}


class DiagnosticsCollector(commands.Cog, name="DiagnosticsCollector"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.started_at = time.time()
        self.commands_ok: collections.Counter = collections.Counter()
        self.commands_err: collections.Counter = collections.Counter()
        self.app_commands_ok: collections.Counter = collections.Counter()
        self.interactions: collections.Counter = collections.Counter()
        self.gateway: collections.Counter = collections.Counter()
        self.gateway_last: dict[str, float] = {}
        self.recent_errors: collections.deque = collections.deque(maxlen=25)
        self.history: collections.deque = collections.deque(maxlen=HISTORY_LEN)
        bot.diagnostics_collector = self  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        self.sample.start()

    async def cog_unload(self) -> None:
        self.sample.cancel()
        if getattr(self.bot, "diagnostics_collector", None) is self:
            del self.bot.diagnostics_collector  # type: ignore[attr-defined]

    # -- sampler -----------------------------------------------------------
    @tasks.loop(seconds=SAMPLE_SECONDS)
    async def sample(self) -> None:
        try:
            from services import diagnostics_service as ds

            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            lag = round(max(0.0, (time.perf_counter() - t0 - 0.01) * 1000), 2)
            row: dict = {"t": time.time(), "lag_ms": lag, "latency_ms": ds._lat_ms(self.bot.latency)}
            if ds.psutil is not None:
                # In a worker thread under the shared psutil lock (services/psutil_guard.py):
                # concurrent psutil calls crashed the bot natively, and waiting for the
                # lock (or cpu_pct's priming sleep) must not block the event loop.
                row.update(await asyncio.to_thread(_sample_process, ds))
            self.history.append(row)
        except Exception:
            pass  # a metrics sampler must never raise

    # -- listeners ---------------------------------------------------------
    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context) -> None:
        self.commands_ok[ctx.command.qualified_name if ctx.command else "?"] += 1

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # Any on_command_error listener disables discord.py's default handler
        # (Bot.on_command_error returns early when one exists), so this one
        # also does the real handling: core/command_errors.py logs the
        # traceback / replies briefly to user errors / ignores CommandNotFound.
        name = ctx.command.qualified_name if ctx.command else "(unknown)"
        self.commands_err[name] += 1
        self.recent_errors.append({"t": time.time(), "command": name, "error": type(error).__name__})
        try:
            await handle_command_error(ctx, error)
        except Exception:
            log.exception("command error handler failed")

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction, command) -> None:
        self.app_commands_ok[getattr(command, "qualified_name", "?")] += 1

    @commands.Cog.listener()
    async def on_interaction(self, interaction) -> None:
        self.interactions[getattr(interaction.type, "name", str(interaction.type))] += 1

    def _gw(self, name: str) -> None:
        self.gateway[name] += 1
        self.gateway_last[name] = time.time()

    @commands.Cog.listener()
    async def on_connect(self) -> None:
        self._gw("connect")

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        self._gw("disconnect")

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        self._gw("resumed")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        self._gw("ready")

    # -- read side ---------------------------------------------------------
    def snapshot(self) -> dict:
        lags = [h["lag_ms"] for h in self.history if h.get("lag_ms") is not None]
        lags_sorted = sorted(lags)
        stats = None
        if lags:
            stats = {
                "samples": len(lags),
                "min": round(lags_sorted[0], 2),
                "avg": round(sum(lags) / len(lags), 2),
                "p95": round(lags_sorted[min(len(lags) - 1, int(len(lags) * 0.95))], 2),
                "max": round(lags_sorted[-1], 2),
            }
        return {
            "since": self.started_at,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "sample_seconds": SAMPLE_SECONDS,
            "commands": {
                "prefix_ok_total": sum(self.commands_ok.values()),
                "prefix_error_total": sum(self.commands_err.values()),
                "slash_ok_total": sum(self.app_commands_ok.values()),
                "top_prefix": self.commands_ok.most_common(15),
                "top_slash": self.app_commands_ok.most_common(15),
                "errors_by_command": self.commands_err.most_common(15),
                "recent_errors": list(self.recent_errors),
            },
            "interactions": dict(self.interactions),
            "gateway": {"counts": dict(self.gateway), "last": self.gateway_last},
            "loop_lag_stats": stats,
            "history": list(self.history),
        }


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DiagnosticsCollector(bot))
