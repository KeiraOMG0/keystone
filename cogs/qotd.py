"""
QOTD Cog — daily Question of the Day system.

Scheduling flow:
  1. Task loop wakes every 60 seconds.
  2. From the configured post time (UTC) until the end of that UTC day, if no
     question was posted today and today's approval request has not been
     sent yet, generates a question and posts an approval embed to the
     configured approval channel (qotd_config.approval_channel_id; there is
     no hardcoded default) with four buttons:
       ✅ Approve  ❌ Deny  🔄 Regenerate  ✏️ Custom Question
     The "sent" marker is an audit_log row (APPROVAL_SENT_ACTION), so a bot
     restart never sends a second request for the same day, and a bot that
     was down at the exact minute catches up once later the same day.
     Failures never stop the loop: Forbidden / missing channel gives up for
     the day, other errors retry (DailyAttemptTracker).
  3. Staff action finalises the post (or discards it).

Admin slash commands:
  /qotd setchannel         — set the public channel and daily post time
  /qotd setapprovalchannel — set which channel receives approval embeds
  /qotd setmodel           — choose the Ollama model via a select menu
  /qotd now                — generate + post immediately (bypasses approval)
  /qotd preview            — generate without posting, shows ephemeral preview
  /qotd regenerate         — replace previewed question with a new one
  /qotd status             — show current config
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import traceback
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.repositories.qotd_repo import QotdRepository
from database.repositories.audit_repo import AuditRepository
from services.qotd_service import (
    APPROVAL_SENT_ACTION,
    DailyAttemptTracker,
    GenerationJobs,
    HistoryLogError,
    QotdService,
    day_bounds_utc,
    qotd_slot_due,
)
from services.audit_service import AuditService

log = logging.getLogger(__name__)

# No hardcoded guild/channel IDs: the approval channel comes only from
# qotd_config.approval_channel_id (the old fallback pointed at a channel in
# the previous guild). Without one, the daily approval request is skipped
# with a warning until it is set (/qotd setapprovalchannel or the dashboard).

# ---------------------------------------------------------------------------
# Model catalogue — only text-generation models, described for staff
# ---------------------------------------------------------------------------
AVAILABLE_MODELS: list[dict] = [
    {
        "id": "llama3.2:3b",
        "label": "Llama 3.2 3B",
        "desc": "Fastest option. Great reliability for QOTD. Low VRAM usage (~2 GB). Recommended default.",
    },
    {
        "id": "llama3.1:8b",
        "label": "Llama 3.1 8B",
        "desc": "Better question variety and creativity than 3B. ~5 GB VRAM. Good balance of speed and quality.",
    },
    {
        "id": "phi4-mini:latest",
        "label": "Phi-4 Mini",
        "desc": "Microsoft's compact 3.8B model. Very instruction-following, clean output. ~2.5 GB VRAM.",
    },
    {
        "id": "gemma2:9b",
        "label": "Gemma 2 9B",
        "desc": "Google's 9B model. Strong reasoning, excellent at varied icebreaker topics. ~5.4 GB VRAM.",
    },
    {
        "id": "phi4:14b",
        "label": "Phi-4 14B",
        "desc": "Highest quality option. Best question creativity but slowest and uses ~9 GB VRAM.",
    },
]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Custom Question modal
# ---------------------------------------------------------------------------

class CustomQuestionModal(discord.ui.Modal, title="Custom QOTD Question"):
    question = discord.ui.TextInput(
        label="Your question",
        placeholder="What is your favourite hobby?",
        min_length=10,
        max_length=280,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, view: "ApprovalView") -> None:
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Re-check: the modal outlives the button click that opened it.
        if not await self._view.interaction_check(interaction):
            return
        text = self.question.value.strip()
        if not text.endswith("?"):
            text += "?"
        await self._view.apply_custom(interaction, text)


def _failure_embed(step: str, error: Exception) -> discord.Embed:
    embed = discord.Embed(
        title="QOTD Post Failed",
        color=discord.Color.red(),
    )
    embed.add_field(name="Step", value=step, inline=False)
    embed.add_field(
        name="Error",
        value=f"```\n{type(error).__name__}: {error}\n```",
        inline=False,
    )
    return embed


def _history_log_embed(channel: discord.TextChannel, error: HistoryLogError) -> discord.Embed:
    embed = discord.Embed(
        title="QOTD Posted — History Save Failed",
        description=f"The question was posted to {channel.mention}, but saving it to history failed.",
        color=discord.Color.orange(),
    )
    embed.add_field(
        name="Error",
        value=f"```\n{type(error.original).__name__}: {error.original}\n```",
        inline=False,
    )
    return embed


# ---------------------------------------------------------------------------
# Approval view (posted in staff lounge)
# ---------------------------------------------------------------------------

class ApprovalView(discord.ui.View):
    """
    Persistent-ish approval view. Not registered as a true persistent view
    (no stable custom_id needed — the bot restores pending approvals from
    the _pending dict on cog load). Timeout of 23 hours so it cleans up
    automatically if staff ignores it.
    """

    def __init__(self, cog: "QotdCog", guild_id: int, question: str, source: str) -> None:
        super().__init__(timeout=82800)  # 23 hours
        self._cog = cog
        self._guild_id = guild_id
        self._question = question
        self._source = source
        self._resolved = False
        self.message: discord.Message | None = None  # set after send

    # ------------------------------------------------------------------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Same gate as the dashboard's POST /api/qotd/post: seeing the approval
        channel is not enough to approve, deny, regenerate or post a custom question."""
        if await self._cog.bot.permission_service.has_permission(interaction.user, "manage_qotd"):
            return True
        await interaction.response.send_message(
            "You need the `manage_qotd` permission to use these buttons.", ephemeral=True
        )
        return False

    async def _disable_all(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        self.stop()

    def _build_embed(self, question: str, source: str, *, status: str | None = None) -> discord.Embed:
        source_label = "AI-generated" if source == "ai" else "fallback list"
        colour = discord.Color.blurple()
        if status == "approved":
            colour = discord.Color.green()
        elif status == "denied":
            colour = discord.Color.red()
        elif status == "custom":
            colour = discord.Color.orange()

        embed = discord.Embed(
            title="QOTD Approval Required",
            description=f"> {question}",
            color=colour,
        )
        embed.add_field(name="Source", value=source_label, inline=True)
        if status:
            embed.add_field(name="Status", value=status.capitalize(), inline=True)
        embed.set_footer(text="Use the buttons below to approve, deny, regenerate, or set a custom question.")
        return embed

    # ------------------------------------------------------------------

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._resolved:
            await interaction.response.send_message("Already resolved.", ephemeral=True)
            return
        self._resolved = True

        cfg = await self._cog._repo.get_config(self._guild_id)
        if not cfg or not cfg["channel_id"]:
            await interaction.response.send_message("No public QOTD channel configured.", ephemeral=True)
            return

        guild = interaction.guild
        channel = guild.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Public channel not found.", ephemeral=True)
            return

        await self._disable_all(interaction)
        await interaction.response.edit_message(
            embed=self._build_embed(self._question, self._source, status="approved"), view=self
        )

        try:
            await self._cog._service.post_qotd(
                guild, channel, self._question, self._source,
                use_emojis=bool(cfg.get("use_emojis", False)),
                ping_role_id=cfg.get("ping_role_id"),
            )
            await interaction.followup.send(
                f"QOTD approved and posted to {channel.mention}.", ephemeral=True
            )
            await self._cog._audit.log(
                self._guild_id, interaction.user.id, "qotd_approved",
                detail={"question": self._question, "source": self._source},
            )
        except HistoryLogError as e:
            await interaction.followup.send(embed=_history_log_embed(channel, e), ephemeral=True)
        except discord.Forbidden as e:
            await interaction.followup.send(
                embed=_failure_embed("Posting message to channel", e), ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.followup.send(
                embed=_failure_embed("Posting message to channel", e), ephemeral=True
            )

        self._cog._pending.pop(self._guild_id, None)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._resolved:
            await interaction.response.send_message("Already resolved.", ephemeral=True)
            return
        self._resolved = True
        await self._disable_all(interaction)
        await interaction.response.edit_message(
            embed=self._build_embed(self._question, self._source, status="denied"), view=self
        )
        await interaction.followup.send("QOTD denied and discarded.", ephemeral=True)
        self._cog._pending.pop(self._guild_id, None)
        log.info("qotd: approval denied for guild %d by %s", self._guild_id, interaction.user)
        await self._cog._audit.log(
            self._guild_id, interaction.user.id, "qotd_denied",
            detail={"question": self._question, "source": self._source},
        )

    @discord.ui.button(label="Regenerate", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def regenerate(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._resolved:
            await interaction.response.send_message("Already resolved.", ephemeral=True)
            return
        await interaction.response.defer()

        cfg = await self._cog._repo.get_config(self._guild_id)
        model = cfg["model"] if cfg else "llama3.2:3b"

        new_q, new_src, _ = await self._cog._service.generate_question(self._guild_id, model=model)
        self._question = new_q
        self._source = new_src

        await interaction.message.edit(embed=self._build_embed(new_q, new_src), view=self)
        await interaction.followup.send("Regenerated a new question.", ephemeral=True)
        log.info("qotd: regenerated question for guild %d", self._guild_id)
        await self._cog._audit.log(
            self._guild_id, interaction.user.id, "qotd_regenerated",
            detail={"question": new_q, "source": new_src},
        )

    @discord.ui.button(label="Custom Question", style=discord.ButtonStyle.primary, emoji="✏️")
    async def custom(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._resolved:
            await interaction.response.send_message("Already resolved.", ephemeral=True)
            return
        await interaction.response.send_modal(CustomQuestionModal(self))

    async def apply_custom(self, interaction: discord.Interaction, text: str) -> None:
        """Called by the modal on submit. The modal interaction is fresh — use response directly."""
        self._resolved = True

        cfg = await self._cog._repo.get_config(self._guild_id)
        if not cfg or not cfg["channel_id"]:
            await interaction.response.send_message("No public QOTD channel configured.", ephemeral=True)
            return

        guild = interaction.guild
        channel = guild.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Public channel not found.", ephemeral=True)
            return

        # Disable all buttons on the approval message
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        self.stop()
        if self.message:
            await self.message.edit(
                embed=self._build_embed(text, "custom", status="custom"), view=self
            )

        try:
            await self._cog._service.post_qotd(
                guild, channel, text, "custom",
                use_emojis=bool(cfg.get("use_emojis", False)),
                ping_role_id=cfg.get("ping_role_id"),
            )
            await interaction.response.send_message(
                f"Custom QOTD posted to {channel.mention}.", ephemeral=True
            )
            await self._cog._audit.log(
                self._guild_id, interaction.user.id, "qotd_custom",
                detail={"question": text},
            )
        except HistoryLogError as e:
            await interaction.response.send_message(embed=_history_log_embed(channel, e), ephemeral=True)
        except discord.Forbidden as e:
            await interaction.response.send_message(
                embed=_failure_embed("Posting message to channel", e), ephemeral=True
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(
                embed=_failure_embed("Posting message to channel", e), ephemeral=True
            )

        self._cog._pending.pop(self._guild_id, None)

    async def on_timeout(self) -> None:
        self._cog._pending.pop(self._guild_id, None)
        log.info("qotd: approval timed out for guild %d", self._guild_id)


# ---------------------------------------------------------------------------
# Model select command view
# ---------------------------------------------------------------------------

class ModelSelectView(discord.ui.View):
    def __init__(self, cog: "QotdCog", guild_id: int) -> None:
        super().__init__(timeout=120)
        self._cog = cog
        self._guild_id = guild_id

        options = [
            discord.SelectOption(
                label=m["label"],
                value=m["id"],
                description=m["desc"][:100],
            )
            for m in AVAILABLE_MODELS
        ]

        select = discord.ui.Select(
            placeholder="Choose a model...",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        model_id = interaction.data["values"][0]
        model_info = next((m for m in AVAILABLE_MODELS if m["id"] == model_id), None)

        await self._cog._repo.upsert_config(self._guild_id, model=model_id)
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            content=f"Model set to **{model_info['label']}**.",
            view=self,
        )
        log.info("qotd: model changed to %s for guild %d by %s", model_id, self._guild_id, interaction.user)
        await self._cog._audit.log(
            self._guild_id, interaction.user.id, "qotd_setmodel",
            detail={"model": model_id},
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class QotdCog(commands.Cog, name="QOTD"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._repo: QotdRepository = QotdRepository(bot.db)  # type: ignore[attr-defined]
        self._audit: AuditService = AuditService(AuditRepository(bot.db))  # type: ignore[attr-defined]
        self._service: QotdService = QotdService(
            self._repo,
            ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
            ollama_exe=os.getenv("OLLAMA_EXE"),
        )
        # guild_id -> (question, source) — used by /qotd preview + /qotd now
        self._previews: dict[int, tuple[str, str]] = {}
        # guild_id -> ApprovalView — one pending approval per guild at a time
        self._pending: dict[int, ApprovalView] = {}
        # Per-guild, per-UTC-day retry budget for the daily approval request.
        self._attempts = DailyAttemptTracker()
        # Dashboard generation jobs (internal_api/routes/qotd.py).
        self._jobs = GenerationJobs()
        self._scheduler.start()

    def cog_unload(self) -> None:
        self._scheduler.cancel()
        self._jobs.cancel_all()

    # ------------------------------------------------------------------
    # Scheduler loop — ticks every 60 seconds
    # ------------------------------------------------------------------

    @tasks.loop(seconds=60)
    async def _scheduler(self) -> None:
        # Every failure is caught per guild, so one bad run (Forbidden, a DB
        # hiccup, a generation error) never stops the loop.
        now = _now_utc()
        for guild in list(self.bot.guilds):
            try:
                await self._maybe_queue_approval(guild, now)
            except Exception:
                log.exception("qotd: scheduler error for guild %d", guild.id)
                # Never leave a bare reservation behind (it would block every
                # later day until a restart).
                if guild.id in self._pending and self._pending[guild.id] is None:
                    self._pending.pop(guild.id, None)

    @_scheduler.before_loop
    async def _before_scheduler(self) -> None:
        await self.bot.wait_until_ready()

    @_scheduler.error
    async def _scheduler_error(self, error: BaseException) -> None:
        # Last resort: anything escaping the per-guild handler would stop a
        # tasks.loop for good. Log it and restart the loop.
        log.error("qotd: scheduler loop crashed, restarting: %r", error)
        self._scheduler.restart()

    async def _approval_sent_today(self, guild_id: int, now: datetime) -> bool:
        start, end = day_bounds_utc(now.date())
        return await self._repo.approval_sent_between(guild_id, APPROVAL_SENT_ACTION, start, end)

    async def _maybe_queue_approval(self, guild: discord.Guild, now: datetime) -> None:
        cfg = await self._repo.get_config(guild.id)
        if not cfg or not cfg["enabled"] or not cfg["channel_id"]:
            return

        # Already have a pending approval (or one is being generated)
        if guild.id in self._pending:
            return
        if not self._attempts.may_try(guild.id, now):
            return

        # Due from the post time until the end of the same UTC day (catch-up),
        # unless already posted today or today's approval was already sent.
        last_posted = await self._repo.get_last_posted_at(guild.id)
        if not qotd_slot_due(now, cfg["post_hour"], cfg["post_minute"], last_posted, False):
            return
        if await self._approval_sent_today(guild.id, now):
            self._attempts.record_success(guild.id, now)
            return

        approval_channel_id = cfg.get("approval_channel_id")
        approval_channel = guild.get_channel(approval_channel_id) if approval_channel_id else None
        if not isinstance(approval_channel, discord.TextChannel):
            log.warning(
                "qotd: no usable approval channel (approval_channel_id=%s) for guild %d; "
                "skipping today's approval request. Set it with /qotd setapprovalchannel.",
                approval_channel_id, guild.id,
            )
            self._attempts.record_failure(guild.id, now, permanent=True)
            return

        catch_up = (now.hour, now.minute) != (cfg["post_hour"], cfg["post_minute"])

        # Reserve the slot before awaiting the slow Ollama call so later ticks don't double-fire
        self._pending[guild.id] = None  # type: ignore[assignment]
        try:
            question, source, ai_errors = await self._service.generate_question(guild.id, model=cfg["model"])
        except Exception:
            log.exception("qotd: generation failed for guild %d", guild.id)
            self._pending.pop(guild.id, None)
            self._attempts.record_failure(guild.id, now)
            return
        view = ApprovalView(self, guild.id, question, source)

        source_label = "AI-generated" if source == "ai" else "fallback list"
        embed = discord.Embed(
            title="QOTD Approval Required",
            description=f"> {question}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Source", value=source_label, inline=True)
        if ai_errors:
            error_block = "\n".join(ai_errors)[:1000]
            embed.add_field(
                name="AI Failures",
                value=f"```\n{error_block}\n```",
                inline=False,
            )
        if catch_up:
            embed.add_field(
                name="Late",
                value=f"Scheduled for {cfg['post_hour']:02d}:{cfg['post_minute']:02d} UTC; sent late (the bot was offline or busy).",
                inline=False,
            )
        embed.set_footer(text="Approve, deny, regenerate, or submit a custom question below.")

        ping_content = f"<@&{cfg['staff_ping_role_id']}>" if cfg.get("staff_ping_role_id") else None
        try:
            msg = await approval_channel.send(content=ping_content, embed=embed, view=view)
        except discord.Forbidden:
            # Previously this left _pending set forever, so QOTD silently
            # stopped until a restart. Give up for today and log it.
            log.error("qotd: no permission to send to approval channel %d (guild %d); giving up for today",
                      approval_channel.id, guild.id)
            view.stop()
            self._pending.pop(guild.id, None)
            self._attempts.record_failure(guild.id, now, permanent=True)
            return
        except discord.HTTPException as e:
            log.error("qotd: failed to send approval for guild %d: %s (will retry)", guild.id, e)
            view.stop()
            self._pending.pop(guild.id, None)
            self._attempts.record_failure(guild.id, now)
            return

        view.message = msg
        self._pending[guild.id] = view
        self._attempts.record_success(guild.id, now)
        log.info("qotd: approval sent to channel %d for guild %d (catch_up=%s)", approval_channel.id, guild.id, catch_up)
        try:
            await self._audit.log(
                guild.id, self.bot.user.id if self.bot.user else 0, APPROVAL_SENT_ACTION,
                target_type="channel", target_id=approval_channel.id,
                detail={"date": now.date().isoformat(), "message_id": str(msg.id), "catch_up": catch_up,
                        "question": question, "source": source},
            )
        except Exception:
            # The in-memory tracker still prevents a resend until a restart.
            log.exception("qotd: could not record the approval-sent marker for guild %d", guild.id)

    # ------------------------------------------------------------------
    # Slash command group: /qotd
    # ------------------------------------------------------------------

    qotd_group = app_commands.Group(name="qotd", description="Question of the Day management.")

    @qotd_group.command(name="setchannel", description="Set the public channel, post time, and optional ping roles for QOTD")
    @app_commands.describe(
        channel="Channel where the QOTD will be posted publicly",
        hour="UTC hour to post (0–23, default 9)",
        minute="Minute of the hour (0–59, default 0)",
        ping_role="Role to mention in the public channel with each post (optional)",
        staff_ping_role="Role to ping in the staff lounge when approval is needed (optional)",
    )
    async def qotd_setchannel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        hour: app_commands.Range[int, 0, 23] = 9,
        minute: app_commands.Range[int, 0, 59] = 0,
        ping_role: discord.Role | None = None,
        staff_ping_role: discord.Role | None = None,
    ) -> None:
        if not await self.bot.permission_service.has_permission(interaction.user, "manage_qotd"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._repo.upsert_config(
            interaction.guild_id,
            channel_id=channel.id,
            post_hour=hour,
            post_minute=minute,
            enabled=True,
            ping_role_id=ping_role.id if ping_role else None,
            clear_ping_role=ping_role is None,
            staff_ping_role_id=staff_ping_role.id if staff_ping_role else None,
            clear_staff_ping_role=staff_ping_role is None,
        )
        log.info(
            "qotd: setchannel — guild=%d channel=%d time=%02d:%02d UTC role=%s staff_role=%s by %s",
            interaction.guild_id, channel.id, hour, minute,
            ping_role.id if ping_role else None,
            staff_ping_role.id if staff_ping_role else None,
            interaction.user,
        )
        role_str = f" • Pinging {ping_role.mention} with each public post." if ping_role else " • No public role ping configured."
        staff_role_str = f" • Pinging {staff_ping_role.mention} in the staff lounge for approvals." if staff_ping_role else " • No staff ping configured."
        cfg_after = await self._repo.get_config(interaction.guild_id)
        approval_ch_id = (cfg_after or {}).get("approval_channel_id")
        approval_str = (
            f"An approval message will be sent to <#{approval_ch_id}> first."
            if approval_ch_id
            else "No approval channel is set yet, so nothing is sent until you run `/qotd setapprovalchannel`."
        )
        await interaction.followup.send(
            f"QOTD will post in {channel.mention} every day at **{hour:02d}:{minute:02d} UTC**.\n"
            f"{role_str}\n"
            f"{staff_role_str}\n"
            f"{approval_str}",
            ephemeral=True,
        )
        await self._audit.log(
            interaction.guild_id, interaction.user.id, "qotd_setchannel",
            target_type="channel", target_id=channel.id,
            detail={
                "hour": hour,
                "minute": minute,
                "ping_role_id": ping_role.id if ping_role else None,
                "staff_ping_role_id": staff_ping_role.id if staff_ping_role else None,
            },
        )

    @qotd_group.command(name="setapprovalchannel", description="Set the channel where QOTD approval requests are posted")
    @app_commands.describe(
        channel="Channel to receive approval embeds (leave blank to clear it; daily requests then pause)",
    )
    async def qotd_setapprovalchannel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self.bot.permission_service.has_permission(interaction.user, "manage_qotd"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if channel is None:
            await self._repo.upsert_config(interaction.guild_id, clear_approval_channel=True)
            await interaction.followup.send(
                "Approval channel cleared. The daily approval request is paused until you set one again.",
                ephemeral=True,
            )
        else:
            await self._repo.upsert_config(interaction.guild_id, approval_channel_id=channel.id)
            await interaction.followup.send(
                f"QOTD approval messages will now be sent to {channel.mention}.",
                ephemeral=True,
            )
        await self._audit.log(
            interaction.guild_id, interaction.user.id, "qotd_setapprovalchannel",
            target_type="channel", target_id=channel.id if channel else None,
            detail={"approval_channel_id": channel.id if channel else None},
        )

    @qotd_group.command(name="setmodel", description="Choose which Ollama model generates the QOTD")
    async def qotd_setmodel(self, interaction: discord.Interaction) -> None:
        if not await self.bot.permission_service.has_permission(interaction.user, "manage_qotd"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        lines = ["**Available QOTD models:**\n"]
        for m in AVAILABLE_MODELS:
            lines.append(f"**{m['label']}** (`{m['id']}`)\n{m['desc']}\n")

        cfg = await self._repo.get_config(interaction.guild_id)
        current = cfg["model"] if cfg else "llama3.2:3b"
        current_info = next((m for m in AVAILABLE_MODELS if m["id"] == current), None)
        current_label = current_info["label"] if current_info else current

        view = ModelSelectView(self, interaction.guild_id)
        await interaction.followup.send(
            "\n".join(lines) + f"\n*Currently using: **{current_label}***",
            view=view,
            ephemeral=True,
        )

    @qotd_group.command(name="now", description="Generate and post the QOTD immediately (bypasses approval)")
    async def qotd_now(self, interaction: discord.Interaction) -> None:
        if not await self.bot.permission_service.has_permission(interaction.user, "manage_qotd"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        cfg = await self._repo.get_config(interaction.guild_id)
        if not cfg or not cfg["channel_id"]:
            await interaction.followup.send(
                "No QOTD channel set. Use `/qotd setchannel` first.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send("Configured channel not found or is not a text channel.", ephemeral=True)
            return

        if interaction.guild_id in self._previews:
            question, source = self._previews.pop(interaction.guild_id)
        else:
            question, source, _ = await self._service.generate_question(
                interaction.guild_id, model=cfg["model"]
            )

        try:
            await self._service.post_qotd(
                interaction.guild, channel, question, source,
                use_emojis=bool(cfg.get("use_emojis", False)),
                ping_role_id=cfg.get("ping_role_id"),
            )
            source_label = "AI-generated" if source == "ai" else "fallback list"
            await interaction.followup.send(
                f"QOTD posted to {channel.mention}. ({source_label})", ephemeral=True
            )
            await self._audit.log(
                interaction.guild_id, interaction.user.id, "qotd_posted_now",
                target_type="channel", target_id=channel.id,
                detail={"question": question, "source": source},
            )
        except HistoryLogError as e:
            await interaction.followup.send(embed=_history_log_embed(channel, e), ephemeral=True)
        except discord.Forbidden as e:
            await interaction.followup.send(
                embed=_failure_embed("Posting message to channel", e), ephemeral=True
            )
        except discord.HTTPException as e:
            log.error("qotd: /qotd now failed for guild %d: %s", interaction.guild_id, e)
            await interaction.followup.send(
                embed=_failure_embed("Posting message to channel", e), ephemeral=True
            )

    @qotd_group.command(name="preview", description="Generate and preview the next QOTD without posting")
    async def qotd_preview(self, interaction: discord.Interaction) -> None:
        if not await self.bot.permission_service.has_permission(interaction.user, "manage_qotd"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        cfg = await self._repo.get_config(interaction.guild_id)
        model = cfg["model"] if cfg else "llama3.2:3b"

        question, source, _ = await self._service.generate_question(
            interaction.guild_id, model=model
        )
        self._previews[interaction.guild_id] = (question, source)

        source_label = "AI-generated" if source == "ai" else "fallback list"
        await interaction.followup.send(
            f"**Preview** ({source_label}):\n> {question}\n\n"
            "Use `/qotd now` to post this, or `/qotd regenerate` for a new one.",
            ephemeral=True,
        )

    @qotd_group.command(name="regenerate", description="Discard the previewed question and generate a new one")
    async def qotd_regenerate(self, interaction: discord.Interaction) -> None:
        if not await self.bot.permission_service.has_permission(interaction.user, "manage_qotd"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        cfg = await self._repo.get_config(interaction.guild_id)
        model = cfg["model"] if cfg else "llama3.2:3b"

        self._previews.pop(interaction.guild_id, None)

        question, source, _ = await self._service.generate_question(
            interaction.guild_id, model=model
        )
        self._previews[interaction.guild_id] = (question, source)

        source_label = "AI-generated" if source == "ai" else "fallback list"
        await interaction.followup.send(
            f"**New Preview** ({source_label}):\n> {question}\n\n"
            "Use `/qotd now` to post, or `/qotd regenerate` again for another.",
            ephemeral=True,
        )

    @qotd_group.command(name="status", description="Show current QOTD configuration and last post time")
    async def qotd_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        cfg = await self._repo.get_config(interaction.guild_id)
        if not cfg:
            await interaction.followup.send(
                "QOTD is not configured. Use `/qotd setchannel` to set it up.", ephemeral=True
            )
            return

        channel_mention = f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "not set"
        enabled_str = "enabled" if cfg["enabled"] else "disabled"
        last = await self._repo.get_last_posted_at(interaction.guild_id)
        last_str = (
            f"<t:{int(datetime.fromisoformat(last).replace(tzinfo=timezone.utc).timestamp())}:R>"
            if last else "never"
        )
        model_info = next((m for m in AVAILABLE_MODELS if m["id"] == cfg["model"]), None)
        model_label = model_info["label"] if model_info else cfg["model"]
        pending_str = "Yes" if interaction.guild_id in self._pending else "No"
        ping_role_str = f"<@&{cfg['ping_role_id']}>" if cfg.get("ping_role_id") else "None"
        staff_ping_role_str = f"<@&{cfg['staff_ping_role_id']}>" if cfg.get("staff_ping_role_id") else "None"
        approval_ch_id = cfg.get("approval_channel_id")
        approval_ch_str = f"<#{approval_ch_id}>" if approval_ch_id else "not set (daily approvals paused)"

        embed = discord.Embed(title="QOTD Configuration", color=discord.Color.blurple())
        embed.add_field(name="Status", value=enabled_str, inline=True)
        embed.add_field(name="Channel", value=channel_mention, inline=True)
        embed.add_field(name="Post Time", value=f"{cfg['post_hour']:02d}:{cfg['post_minute']:02d} UTC", inline=True)
        embed.add_field(name="Approval Channel", value=approval_ch_str, inline=True)
        embed.add_field(name="Ping Role (public)", value=ping_role_str, inline=True)
        embed.add_field(name="Ping Role (staff)", value=staff_ping_role_str, inline=True)
        embed.add_field(name="Model", value=model_label, inline=True)
        embed.add_field(name="Last Posted", value=last_str, inline=True)
        embed.add_field(name="Pending Approval", value=pending_str, inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @qotd_group.command(name="history", description="Browse past QOTD questions (staff only)")
    @app_commands.describe(limit="How many entries to load (default 50, max 200)")
    async def qotd_history(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 200] = 50,
    ) -> None:
        if not await self.bot.permission_service.has_permission(interaction.user, "manage_qotd"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        rows = await self._repo.get_history(interaction.guild_id, limit=limit)
        if not rows:
            await interaction.followup.send("No QOTD history found yet.", ephemeral=True)
            return

        view = HistoryView(rows, interaction.guild_id)
        await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)

    @qotd_group.command(
        name="debug",
        description="Full QOTD pipeline dump with raw output, payloads, and per-rule validation (staff only, not logged)",
    )
    async def qotd_debug(self, interaction: discord.Interaction) -> None:
        if not await self.bot.permission_service.has_permission(interaction.user, "manage_qotd"):
            await interaction.response.send_message("No permission.", ephemeral=True)
            return

        cfg = await self._repo.get_config(interaction.guild_id)
        model = cfg["model"] if cfg else "llama3.2:3b"
        use_emojis = bool(cfg.get("use_emojis", False)) if cfg else False
        model_info = next((m for m in AVAILABLE_MODELS if m["id"] == model), None)
        model_label = model_info["label"] if model_info else model
        ollama_url = self._service._ollama_url

        lines: list[str] = []

        def _console_embed() -> discord.Embed:
            body = "\n".join(lines or ["Starting..."])
            # Discord embed description cap is 4096 chars; trim from top if needed
            if len(body) + 8 > 4096:
                body = "...(truncated)\n" + body[-(4096 - 24):]
            embed = discord.Embed(
                title="QOTD Debug Console",
                description="```\n" + body + "\n```",
                color=discord.Color.og_blurple(),
            )
            embed.set_footer(text=f"model: {model_label} • url: {ollama_url} • not saved to DB • not logged")
            return embed

        async def _tick(*parts: str) -> None:
            for part in parts:
                lines.append(part)
            await interaction.edit_original_response(embed=_console_embed())

        await interaction.response.send_message(embed=_console_embed())

        # ── Step 1: recent history ────────────────────────────────────────
        await _tick("[1/6] Fetching recent question history...")
        recent = await self._repo.get_recent_questions(interaction.guild_id, limit=60)
        await _tick(
            f"[1/6] Done — {len(recent)} recent question(s) loaded",
            f"      last 3: {recent[:3]!r}" if recent else "      (none)",
        )

        # ── Step 2: Ollama availability ───────────────────────────────────
        await _tick(f"[2/6] Checking Ollama at {ollama_url} ...")
        ollama_ok = False
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as s:
                async with s.get(f"{ollama_url}/api/tags") as r:
                    if r.status == 200:
                        tags_data = await r.json()
                        model_names = [m.get("name", "?") for m in tags_data.get("models", [])]
                        ollama_ok = True
                        await _tick(
                            f"[2/6] Ollama running — {len(model_names)} model(s) available",
                            f"      installed: {', '.join(model_names) or '(none)'}",
                            f"      target model '{model}' {'FOUND' if model in model_names else 'NOT FOUND — will error on generate'}",
                        )
                    else:
                        await _tick(f"[2/6] Ollama responded HTTP {r.status} (unexpected)")
        except Exception as e:
            tb = traceback.format_exc().strip()
            await _tick(
                "[2/6] Ollama not reachable — attempting to start...",
                f"      connection error: {type(e).__name__}: {e}",
                f"      traceback:",
                *[f"        {ln}" for ln in tb.splitlines()],
            )
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: subprocess.Popen(
                        ["ollama", "start"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                    )
                )
                await _tick("[2/6] Process launched — waiting up to 15s...")
                for i in range(15):
                    await asyncio.sleep(1)
                    try:
                        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as s:
                            async with s.get(f"{ollama_url}/api/tags") as r:
                                if r.status == 200:
                                    ollama_ok = True
                                    await _tick(f"[2/6] Ollama ready after {i + 1}s")
                                    break
                    except Exception:
                        pass
                else:
                    await _tick("[2/6] Ollama did not respond in time — will attempt generation anyway")
            except FileNotFoundError:
                await _tick("[2/6] ERROR: ollama executable not found on PATH")

        # ── Step 3: Ollama request + raw response ─────────────────────────
        payload = {
            "model": model,
            "system": "(see _SYSTEM_PROMPT in qotd_service.py)",
            "prompt": "Generate one QOTD question now.",
            "stream": False,
            "options": {
                "temperature": 0.85,
                "top_p": 0.9,
                "top_k": 40,
                "num_predict": 80,
                "stop": ["\n\n", "\n1.", "\nA:", "\n-"],
            },
        }
        import json as _json
        payload_pretty = _json.dumps(payload, indent=2)
        await _tick(
            f"[3/6] Calling Ollama ({model_label})...",
            f"      POST {ollama_url}/api/generate",
            f"      payload:",
            *[f"        {ln}" for ln in payload_pretty.splitlines()],
        )

        raw_text: str | None = None
        raw_error_tb: str | None = None
        try:
            raw_text = await self._service._call_ollama(model)
            raw_repr = repr(raw_text)
            char_count = len(raw_text)
            lines_count = raw_text.count("\n") + 1
            await _tick(
                f"[3/6] Ollama responded OK",
                f"      length: {char_count} chars, {lines_count} line(s)",
                f"      raw repr: {raw_repr[:400]}{'...' if len(raw_repr) > 400 else ''}",
                f"      stripped text:",
                *[f"        {ln}" for ln in (raw_text.strip() or "(empty)").splitlines()],
            )
        except Exception as e:
            raw_error_tb = traceback.format_exc().strip()
            await _tick(
                f"[3/6] ERROR — {type(e).__name__}: {e}",
                f"      full traceback:",
                *[f"        {ln}" for ln in raw_error_tb.splitlines()],
            )

        # ── Step 4: per-rule validation ───────────────────────────────────
        validated_question: str | None = None
        if raw_text is not None:
            await _tick("[4/6] Running validation pipeline...")

            import unicodedata as _ud, re as _re
            from difflib import SequenceMatcher as _SM

            def _norm(t: str) -> str:
                return _ud.normalize("NFKC", t).strip().strip('"\'').strip()

            def _wset(t: str) -> set[str]:
                return set(_re.findall(r"\b[a-z]{3,}\b", t.lower()))

            text = _norm(raw_text)
            await _tick(f"      after normalise: {text!r}")

            for prefix in ("question:", "qotd:", "here is", "here's", "sure!", "sure,", "of course"):
                if text.lower().startswith(prefix):
                    text = text[len(prefix):].strip()
                    await _tick(f"      stripped prefix '{prefix}': {text!r}")
                    break

            text = text.split("\n")[0].strip()
            await _tick(f"      first-line only: {text!r}")

            text = text.rstrip("!.?") + "?"
            await _tick(f"      normalised punctuation: {text!r}")

            rules_passed = True

            # length min
            if len(text) < 15:
                await _tick(f"      FAIL length-min: {len(text)} chars < 15 — {text!r}")
                rules_passed = False
            else:
                await _tick(f"      PASS length-min: {len(text)} chars")

            # length max
            if rules_passed and len(text) > 280:
                await _tick(f"      FAIL length-max: {len(text)} chars > 280")
                rules_passed = False
            elif rules_passed:
                await _tick(f"      PASS length-max: {len(text)} chars")

            # question mark placement
            if rules_passed:
                qmarks = text.count("?")
                if qmarks != 1 or not text.endswith("?"):
                    await _tick(f"      FAIL question-mark: count={qmarks} ends_with_?={text.endswith('?')} — {text!r}")
                    rules_passed = False
                else:
                    await _tick(f"      PASS question-mark: exactly 1 at end")

            # multiple sentences
            if rules_passed:
                sent_ends = len(_re.findall(r"[.!?]", text))
                if sent_ends > 1:
                    await _tick(f"      FAIL multi-sentence: {sent_ends} sentence-enders found — {text!r}")
                    rules_passed = False
                else:
                    await _tick(f"      PASS multi-sentence: {sent_ends} sentence-ender")

            # banned keywords
            if rules_passed:
                from services.qotd_service import _BANNED_KEYWORDS
                words = _wset(text)
                hits = words & _BANNED_KEYWORDS
                if hits:
                    await _tick(f"      FAIL banned-keywords: {hits}")
                    rules_passed = False
                else:
                    await _tick(f"      PASS banned-keywords: none found")

            # similarity against history
            if rules_passed:
                worst_sim: tuple[float, str] | None = None
                for prev in recent:
                    sim = _SM(None, text.lower(), prev.lower()).ratio()
                    if worst_sim is None or sim > worst_sim[0]:
                        worst_sim = (sim, prev)
                if worst_sim and worst_sim[0] > 0.75:
                    await _tick(
                        f"      FAIL similarity: {worst_sim[0]:.2f} > 0.75",
                        f"        matched: {worst_sim[1]!r}",
                    )
                    rules_passed = False
                else:
                    sim_str = f"{worst_sim[0]:.2f}" if worst_sim else "n/a"
                    await _tick(f"      PASS similarity: worst={sim_str} (threshold 0.75)")

            # word overlap against recent 20
            if rules_passed:
                _STOP = {"the","and","for","you","your","that","this","with","have","would","what","which"}
                sig = _wset(text) - _STOP
                worst_ov: tuple[float, str] | None = None
                for prev in recent[:20]:
                    prev_sig = _wset(prev) - _STOP
                    ov = len(sig & prev_sig) / max(len(sig), 1)
                    if worst_ov is None or ov > worst_ov[0]:
                        worst_ov = (ov, prev)
                if worst_ov and worst_ov[0] > 0.65:
                    await _tick(
                        f"      FAIL word-overlap: {worst_ov[0]:.2f} > 0.65",
                        f"        matched: {worst_ov[1]!r}",
                    )
                    rules_passed = False
                else:
                    ov_str = f"{worst_ov[0]:.2f}" if worst_ov else "n/a"
                    await _tick(f"      PASS word-overlap: worst={ov_str} (threshold 0.65)")

            if rules_passed:
                validated_question = text
                await _tick(f"[4/6] Validation PASSED — final question: {text!r}")
            else:
                await _tick(f"[4/6] Validation FAILED — see rule above")
        else:
            await _tick("[4/6] Skipped — no Ollama output")

        # ── Step 5: final question source ─────────────────────────────────
        await _tick("[5/6] Determining final question...")
        if validated_question:
            final_question = validated_question
            final_source = "ai"
            await _tick(f"[5/6] Source: AI")
            await _tick(f"      {final_question!r}")
        else:
            final_question = self._service._pick_fallback(recent)
            final_source = "fallback"
            await _tick(f"[5/6] Source: fallback (AI failed or was skipped)")
            await _tick(f"      {final_question!r}")

        # ── Step 6: preview embed ─────────────────────────────────────────
        await _tick("[6/6] Sending preview embed to this channel...")
        public_embed = self._service._build_embed(final_question, use_emojis=use_emojis)
        public_embed.set_footer(text="[DEBUG PREVIEW — not posted to QOTD channel]")
        await interaction.channel.send(embed=public_embed)

        outcome = "AI" if final_source == "ai" else "fallback"
        await _tick(f"[6/6] Done — outcome: {outcome}")
        await _tick("━━━ Simulation complete ━━━")


# ---------------------------------------------------------------------------
# History paginator view
# ---------------------------------------------------------------------------

_HISTORY_PAGE_SIZE = 5


class HistoryView(discord.ui.View):
    def __init__(self, rows: list[dict], guild_id: int) -> None:
        super().__init__(timeout=120)
        self._rows = rows
        self._guild_id = guild_id
        self._page = 0
        self._total_pages = max(1, (len(rows) + _HISTORY_PAGE_SIZE - 1) // _HISTORY_PAGE_SIZE)
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.prev_btn.disabled = self._page == 0
        self.next_btn.disabled = self._page >= self._total_pages - 1

    def build_embed(self) -> discord.Embed:
        start = self._page * _HISTORY_PAGE_SIZE
        page_rows = self._rows[start : start + _HISTORY_PAGE_SIZE]

        embed = discord.Embed(
            title="📋 QOTD History",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Page {self._page + 1} of {self._total_pages} • {len(self._rows)} total entries")

        for row in page_rows:
            source = row["source"]
            if source == "ai":
                badge = "🤖 AI"
            elif source == "fallback":
                badge = "📝 Fallback"
            else:
                badge = f"✏️ {source.capitalize()}"

            posted_ts = int(
                datetime.fromisoformat(row["posted_at"]).replace(tzinfo=timezone.utc).timestamp()
            )
            time_str = f"<t:{posted_ts}:D> (<t:{posted_ts}:R>)"

            jump = ""
            if row.get("channel_id") and row.get("message_id"):
                jump = f"\n[Jump to message](https://discord.com/channels/{self._guild_id}/{row['channel_id']}/{row['message_id']})"

            embed.add_field(
                name=f"#{row['id']} • {badge}",
                value=f"> {row['question']}\n{time_str}{jump}",
                inline=False,
            )

        if not page_rows:
            embed.description = "No history found."

        return embed

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


async def setup(bot: commands.Bot) -> None:
    if not hasattr(bot, "db") or bot.db is None:
        log.warning("bot.db not available — qotd cog not loaded.")
        return
    await bot.add_cog(QotdCog(bot))
