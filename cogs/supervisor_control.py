"""
cogs/supervisor_control.py - restart parts of the stack through supervisor.py.

    /restart bot [reason]        /restart dashboard [reason]
    /restart tunnel [reason]     /restart all [reason]
    /restart status

Staff only (OWNER_ID or STAFF_ROLE_IDS via PermissionService.is_staff_interaction,
the same check as /bot permissions; no new permission string). Ephemeral replies.
Talks to the supervisor's loopback control API through
services/supervisor_client.py; when the bot is not running under supervisor.py
it says so and does nothing.

Self-restart (bot / all): the request carries a 5s delay, and the
"Restarting..." reply is sent right after the supervisor accepts it (only the
tiny marker write comes first; the audit row is written after the reply), so
the reply lands before the supervisor kills this process. A small marker file
(logs/supervisor_restart_marker.json: channel id, requester id, target, time;
deliberately NO interaction token, which would let anyone who can read logs/
post as the bot on that interaction) lets the next on_ready post a normal,
non-pinging "Back online after Xs" message in the channel /restart was used in.
Markers older than 10 minutes, or in the old token format, are discarded. The
marker is deleted before it is used.

Every accepted request is written to audit_log (action "supervisor_restart").
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from services import supervisor_client
from services.supervisor_client import SupervisorCooldown, SupervisorError, SupervisorUnavailable

log = logging.getLogger(__name__)

MARKER_PATH = Path(__file__).resolve().parent.parent / "logs" / "supervisor_restart_marker.json"
MARKER_MAX_AGE_S = 600
SELF_RESTART_DELAY_S = 5.0  # covers Discord follow-up latency before the kill
BOT_BOOT_ESTIMATE_S = 15  # rough time from launch to on_ready, for the reply text

UNAVAILABLE_TEXT = (
    "The supervisor's control API is not reachable, so nothing was restarted. The bot is not running under "
    "`supervisor.py` (or the supervisor is an older copy without the control API; restart it once to load it)."
)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def write_marker(data: dict, path: Path = MARKER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def build_marker(channel_id: int | None, user_id: int, target: str, now: float | None = None) -> dict:
    """What the back-online message needs, and nothing secret (no token)."""
    return {
        "requested_at": time.time() if now is None else now,
        "channel_id": str(channel_id) if channel_id else None,
        "user_id": str(user_id),
        "target": target,
    }


def back_online_text(marker: dict, now: float | None = None) -> str:
    now = time.time() if now is None else now
    took = max(0, int(now - float(marker["requested_at"])))
    user_id = str(marker.get("user_id") or "")
    who = f" (restart requested by <@{user_id}>)" if user_id.isdigit() else ""
    return f"Back online after {took}s{who}."


def pop_marker(now: float | None = None, path: Path = MARKER_PATH, max_age: float = MARKER_MAX_AGE_S) -> dict | None:
    """Read and DELETE the marker. Returns it only when it is well-formed and
    fresh; never raises."""
    now = time.time() if now is None else now
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    try:
        data = json.loads(raw)
        age = now - float(data["requested_at"])
        if not (0 <= age <= max_age) or not str(data.get("channel_id") or "").isdigit():
            return None
        return data
    except (ValueError, KeyError, TypeError):
        return None


def _epoch(iso_text: str | None) -> int | None:
    if not iso_text:
        return None
    try:
        return int(datetime.fromisoformat(iso_text).timestamp())
    except (TypeError, ValueError):
        return None


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    s = int(seconds)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s % 60}s"
    return f"{s}s"


def describe_service(svc: dict) -> str:
    """Multi-line embed field text for one entry of GET /status `services`."""
    lines = []
    if svc.get("running"):
        lines.append(f"**Running** - pid {svc.get('pid')}, up {fmt_duration(svc.get('uptime_s'))}")
    elif svc.get("skip_reason") == "port in use":
        lines.append("**Not managed** - its port is held by a process the supervisor did not start")
    elif svc.get("waiting_on_dependency"):
        lines.append("**Down** - waiting for the bot's internal API before starting")
    else:
        nxt = svc.get("next_start_in_s")
        lines.append("**Down**" + (f" - starting in {fmt_duration(nxt)}" if nxt else " - starting"))
    if svc.get("restart_pending"):
        lines.append("Restart in progress")
    lines.append(f"Restarts: {svc.get('restarts', 0)}")
    if svc.get("health_fails"):
        lines.append(f"Health check failures: {svc['health_fails']}")
    last_exit = svc.get("last_exit")
    if last_exit:
        ts = _epoch(last_exit.get("at"))
        lines.append(f"Last exit: {str(last_exit.get('reason'))[:200]}" + (f" (<t:{ts}:R>)" if ts else ""))
    req = svc.get("last_request")
    if req:
        ts = _epoch(req.get("at"))
        lines.append(f"Last requested: {str(req.get('by'))[:80]} via {req.get('source')}" + (f" (<t:{ts}:R>)" if ts else ""))
    cooldown = svc.get("cooldown_remaining_s") or 0
    if cooldown > 0:
        lines.append(f"Cooldown: {int(cooldown + 0.999)}s")
    return "\n".join(lines)[:1024]


def build_status_embed(status: dict) -> discord.Embed:
    sup = status.get("supervisor") or {}
    started = _epoch(sup.get("started_at"))
    embed = discord.Embed(title="Supervisor status", color=discord.Color.blurple())
    embed.description = (
        f"supervisor.py v{sup.get('version', '?')}, pid {sup.get('pid', '?')}"
        + (f", up since <t:{started}:R>" if started else "")
        + f". Restart cooldown {int(sup.get('restart_cooldown_s') or 0)}s."
    )
    for svc in status.get("services") or []:
        embed.add_field(name=str(svc.get("name", "?")).capitalize(), value=describe_service(svc) or "-", inline=False)
    return embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class SupervisorControl(commands.Cog, name="SupervisorControl"):
    restart = app_commands.Group(
        name="restart",
        description="Restart the bot, dashboard or tunnel through supervisor.py (staff only).",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot, permission_service, audit) -> None:
        self.bot = bot
        self.permission_service = permission_service
        self._audit = audit
        self._marker_checked = False

    # -- commands -----------------------------------------------------------
    @restart.command(name="bot", description="Restart the bot process (drops music/voice; back in ~20s).")
    @app_commands.describe(reason="Optional reason, shown in the supervisor log and the alert.")
    async def restart_bot(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        await self._restart(interaction, "bot", reason)

    @restart.command(name="dashboard", description="Restart the web dashboard process.")
    @app_commands.describe(reason="Optional reason, shown in the supervisor log and the alert.")
    async def restart_dashboard(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        await self._restart(interaction, "dashboard", reason)

    @restart.command(name="tunnel", description="Restart the Cloudflare tunnel (remote dashboard access drops briefly).")
    @app_commands.describe(reason="Optional reason, shown in the supervisor log and the alert.")
    async def restart_tunnel(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        await self._restart(interaction, "tunnel", reason)

    @restart.command(name="all", description="Restart the bot, dashboard and tunnel.")
    @app_commands.describe(reason="Optional reason, shown in the supervisor log and the alert.")
    async def restart_all(self, interaction: discord.Interaction, reason: str | None = None) -> None:
        await self._restart(interaction, "all", reason)

    @restart.command(name="status", description="Show what supervisor.py is running.")
    async def restart_status(self, interaction: discord.Interaction) -> None:
        if not self.permission_service.is_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            status = await supervisor_client.get_status()
        except SupervisorUnavailable:
            await interaction.followup.send(UNAVAILABLE_TEXT.replace(", so nothing was restarted", ""), ephemeral=True)
            return
        except SupervisorError as exc:
            await interaction.followup.send(f"The supervisor refused the request: {exc.message}", ephemeral=True)
            return
        await interaction.followup.send(embed=build_status_embed(status), ephemeral=True)

    # -- implementation -------------------------------------------------------
    async def _restart(self, interaction: discord.Interaction, target: str, reason: str | None) -> None:
        if not self.permission_service.is_staff_interaction(interaction):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        reason = (reason or "").strip()[:300] or None
        restarts_self = target in ("bot", "all")
        user = interaction.user
        requested_by = f"{getattr(user, 'display_name', None) or user.name} ({user.id})"
        try:
            result = await supervisor_client.request_restart(
                target, reason=reason, requested_by=requested_by, source="discord",
                delay_s=SELF_RESTART_DELAY_S if restarts_self else None,
            )
        except SupervisorUnavailable:
            await interaction.followup.send(UNAVAILABLE_TEXT, ephemeral=True)
            return
        except SupervisorCooldown as exc:
            await interaction.followup.send(
                f"Not restarted: **{target}** was restarted on request moments ago. Try again in {exc.retry_after_s}s.",
                ephemeral=True,
            )
            return
        except SupervisorError as exc:
            await interaction.followup.send(f"Not restarted: {exc.message}", ephemeral=True)
            return

        accepted: list[str] = list(result.get("accepted") or [])
        skipped: list[dict] = list(result.get("skipped") or [])
        eta = float(result.get("eta_s") or 0)

        if "bot" in accepted:
            try:
                write_marker(build_marker(interaction.channel_id, user.id, target))
            except OSError as exc:
                log.warning("supervisor_control: could not write restart marker: %s", exc)

        # Reply FIRST: on a self-restart the kill comes SELF_RESTART_DELAY_S
        # after acceptance, whether or not the reply went out.
        parts = []
        if "bot" in accepted:
            parts.append(
                f"Restarting {'everything' if target == 'all' else 'the bot'} now. "
                f"Back in about {int(eta + BOT_BOOT_ESTIMATE_S)}s; I'll post here when I'm online again. "
                "Music/voice sessions are dropped."
            )
        else:
            parts.append(f"Restart of **{', '.join(accepted)}** requested; it relaunches in about {int(eta + 0.999)}s.")
        for s in skipped:
            parts.append(f"Skipped **{s.get('name')}**: {s.get('message', s.get('error'))}")
        try:
            await interaction.followup.send("\n".join(parts)[:1900], ephemeral=True)
        except discord.HTTPException as exc:
            log.warning("supervisor_control: reply failed: %s", exc)

        await self._log_audit(interaction, target, reason, accepted, skipped)

    async def _log_audit(self, interaction, target, reason, accepted, skipped) -> None:
        if self._audit is None or interaction.guild_id is None:
            return
        try:
            await self._audit.log(
                interaction.guild_id, interaction.user.id, "supervisor_restart",
                target_type="service", target_id=None,
                detail={"target": target, "reason": reason, "via": "discord", "accepted": accepted,
                        "skipped": [s.get("name") for s in skipped]},
            )
        except Exception as exc:  # auditing must never fail the command
            log.warning("supervisor_control: audit log failed: %s", exc)

    # -- back online ----------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._marker_checked:
            return
        self._marker_checked = True
        try:
            marker = pop_marker()
            if not marker:
                return
            channel_id = int(marker["channel_id"])
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            text = back_online_text(marker)
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            log.info("supervisor_control: posted back-online message in %s: %s", channel_id, text)
        except Exception as exc:  # never break on_ready
            log.warning("supervisor_control: back-online follow-up failed: %s", exc)


async def setup(bot: commands.Bot) -> None:
    audit = None
    try:
        from database.repositories.audit_repo import AuditRepository
        from services.audit_service import AuditService

        audit = AuditService(AuditRepository(bot.db))  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - audit is best effort
        log.warning("supervisor_control: audit unavailable: %s", exc)
    await bot.add_cog(SupervisorControl(bot, bot.permission_service, audit))  # type: ignore[attr-defined]
