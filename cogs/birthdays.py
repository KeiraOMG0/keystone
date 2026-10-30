# SPDX-License-Identifier: AGPL-3.0-only
# Python port of BirthdayBot by NoiTheCat (https://github.com/NoiTheCat/BirthdayBot),
# licensed under the GNU AGPL v3.0. See CREDITS.md and LICENSES/AGPL-3.0.txt.
"""
Birthday cog — /birthday commands + precision asyncio scheduler.
Shared state (repo, service, scheduled tasks) is stored on bot.birthday_core
so the sibling cogs (birthday_config, birthday_override, birthday_io) can reach it.
"""

import asyncio
import io
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.repositories.birthday_repo import BirthdayRepository
from services.birthday_service import (
    BirthdayService,
    current_occurrence_year,
    format_birthday,
    nearest_birthdays,
    next_celebration,
    parse_timezone,
    should_celebrate,
    should_remove_birthday_role,
)
from core.logging_setup import get_logger
from core.placeholder_engine import resolve_text, RenderContext
from services.self_role_policy import grant_block_reason

log = get_logger("birthdays")

_SCHEDULE_WINDOW_DAYS = 14
_UTC = ZoneInfo("UTC")

# Bot permission for every birthday management command (/config, /override,
# /birthdays export|import) and the dashboard Birthdays page. Staff (OWNER_ID /
# STAFF_ROLE_IDS) bypass it, as everywhere.
MANAGE_PERMISSION = "manage_birthdays"


def _tz_choices(current: str) -> list[app_commands.Choice[str]]:
    low = current.lower()
    return [
        app_commands.Choice(name=tz, value=tz)
        for tz in sorted(available_timezones())
        if low in tz.lower()
    ][:25]


def _member_tz(row: dict, guild_tz: ZoneInfo) -> ZoneInfo:
    """The member's own timezone, else the guild's. An unparseable stored
    timezone falls back to the guild's instead of crashing the scheduler."""
    if row.get("user_timezone"):
        return parse_timezone(row["user_timezone"]) or guild_tz
    return guild_tz


def _ephemeral(settings: dict, *, force: bool = False) -> bool:
    return force or bool(settings.get("private_confirms"))


def _format_name(member: discord.Member, ping: bool) -> str:
    if ping:
        return member.mention
    return discord.utils.escape_markdown(member.nick or member.display_name)


# ------------------------------------------------------------------ #
# BirthdayCore — shared object stored at bot.birthday_core            #
# ------------------------------------------------------------------ #

class BirthdayCore:
    """Holds repo, service, and scheduled tasks. Shared across all birthday cogs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.repo = BirthdayRepository(bot.db)
        self.svc = BirthdayService(self.repo)
        self.scheduled: dict[tuple[int, int], asyncio.Task] = {}

    async def manage_check(self, interaction: discord.Interaction) -> bool:
        """Gate for the birthday management commands: the `manage_birthdays`
        bot permission (staff bypass). Answers the interaction when refused."""
        user = interaction.user
        allowed = isinstance(user, discord.Member) and await self.bot.permission_service.has_permission(
            user, MANAGE_PERMISSION
        )
        if not allowed:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return False
        return True

    async def audit(
        self,
        guild_id: int,
        actor_id: int,
        action: str,
        target_type: str | None = None,
        target_id: int | None = None,
        detail: dict | None = None,
    ) -> None:
        """Best-effort audit_log row (+ live `audit` event) for a birthday
        management action, from the slash commands and the dashboard alike."""
        try:
            from database.repositories.audit_repo import AuditRepository
            from services.audit_service import AuditService

            await AuditService(AuditRepository(self.bot.db)).log(
                guild_id, actor_id or 0, action, target_type, target_id, detail
            )
        except Exception:
            log.exception("could not write %s audit row", action)

    def scheduled_count(self, guild_id: int) -> int:
        return sum(1 for k in self.scheduled if k[0] == guild_id)

    def cancel_scheduled(self, guild_id: int, user_id: int) -> None:
        task = self.scheduled.pop((guild_id, user_id), None)
        if task is not None:
            task.cancel()

    async def send_test_announcement(self, guild: discord.Guild, members: list[discord.Member]) -> str | None:
        """/config announce test and the dashboard button: send the configured
        announcement for `members` to the configured channel. Nothing is
        marked celebrated and no role is given. Returns None on success or an
        error code: channel_not_set | channel_missing | channel_no_permission."""
        settings = await self.svc.get_settings(guild.id)
        ch_id = settings.get("announce_channel_id")
        if not ch_id:
            return "channel_not_set"
        channel = guild.get_channel(ch_id)
        if channel is None:
            return "channel_missing"
        if not channel.permissions_for(guild.me).send_messages:
            return "channel_no_permission"
        await self._send_announcement(settings, channel, members)
        return None

    async def schedule_guild(self, guild_id: int) -> None:
        for key in [k for k in self.scheduled if k[0] == guild_id]:
            self.scheduled.pop(key).cancel()

        settings = await self.svc.get_settings(guild_id)
        guild_tz = parse_timezone(settings.get("guild_timezone") or "UTC") or _UTC
        now_utc = datetime.now(_UTC)

        # svc.get_upcoming covers yesterday (UTC- zones) and Feb 29 in
        # non-leap years (celebrated on Feb 28).
        rows = await self.svc.get_upcoming(guild_id, days=_SCHEDULE_WINDOW_DAYS)
        for row in rows:
            tz = _member_tz(row, guild_tz)
            nxt = next_celebration(row["birth_month"], row["birth_day"], tz, now_utc, _SCHEDULE_WINDOW_DAYS)
            if nxt is None:
                continue
            fire_at, occurrence_year = nxt
            # last_celebrated holds the LOCAL year of the last announced
            # occurrence, so an occurrence straddling Dec 31/Jan 1 UTC is
            # still recognised as already done.
            if not should_celebrate(row["last_celebrated"], occurrence_year):
                continue
            key = (guild_id, row["user_id"])
            member = guild_obj.get_member(row["user_id"]) if (guild_obj := self.bot.get_guild(guild_id)) else None
            log.info(
                "Scheduling birthday: guild=%s user=%s display_name=%s date=%s tz=%s fire_at_utc=%s",
                guild_id, row["user_id"],
                member.display_name if member else "<unknown>",
                format_birthday(row["birth_month"], row["birth_day"]),
                str(tz), fire_at.isoformat(),
            )
            self.scheduled[key] = asyncio.get_event_loop().create_task(
                self._wait_and_fire(guild_id, row["user_id"], fire_at),
                name=f"bday-{guild_id}-{row['user_id']}",
            )

    async def schedule_all(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self.schedule_guild(guild.id)
            except Exception as e:
                log.error("Schedule error for guild %s: %s", guild.id, e)
        log.info(
            "Birthday scheduler: %d task(s) across %d guild(s)",
            len(self.scheduled), len(self.bot.guilds),
        )

    async def reset_timers(self, guild_id: int) -> int:
        """Reset last_celebrated only for users whose birthday is today (the
        server's timezone), so they re-fire, then reschedule. Returns how many
        birthdays were reset."""
        settings = await self.svc.get_settings(guild_id)
        guild_tz = parse_timezone(settings.get("guild_timezone") or "UTC") or _UTC
        now_local = datetime.now(guild_tz)
        # Includes Feb 29 on Feb 28 of a non-leap year.
        count = await self.svc.reset_today(guild_id, now_local.date())
        await self.schedule_guild(guild_id)
        return count

    async def _wait_and_fire(self, guild_id: int, user_id: int, fire_at: datetime) -> None:
        now_utc = datetime.now(_UTC)
        delay = (fire_at - now_utc).total_seconds()
        log.info(
            "_wait_and_fire starting: guild=%s user=%s fire_at_utc=%s now_utc=%s delay_s=%.1f",
            guild_id, user_id, fire_at.isoformat(), now_utc.isoformat(), delay,
        )
        # The event loop may wake a few ms early (clock resolution); firing
        # before local midnight would look like "not their birthday yet", so
        # sleep until fire_at has really passed.
        while delay > 0:
            await asyncio.sleep(delay + 0.5)
            delay = (fire_at - datetime.now(_UTC)).total_seconds()
        self.scheduled.pop((guild_id, user_id), None)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        try:
            await self._fire_birthday(guild, user_id)
        except Exception as e:
            log.exception("Error firing birthday user=%s guild=%s: %s", user_id, guild_id, e)

    async def _fire_birthday(self, guild: discord.Guild, user_id: int) -> None:
        row = await self.repo.get_birthday(guild.id, user_id)
        if row is None:
            return

        settings = await self.svc.get_settings(guild.id)
        member = guild.get_member(user_id)
        guild_tz = parse_timezone(settings.get("guild_timezone") or "UTC") or _UTC
        tz = _member_tz(row, guild_tz)
        # Local year of the occurrence happening now (None = not their
        # birthday in their timezone right now, e.g. the date was edited).
        current_year = current_occurrence_year(row["birth_month"], row["birth_day"], tz, datetime.now(_UTC))

        log.info(
            "_fire_birthday start: guild=%s user=%s row=%s member_id=%s display_name=%s mention=%s "
            "current_year=%s last_celebrated=%s",
            guild.id, user_id, row,
            member.id if member else None,
            member.display_name if member else "<not in cache>",
            member.mention if member else "<not in cache>",
            current_year, row["last_celebrated"],
        )

        if not should_celebrate(row["last_celebrated"], current_year):
            return

        if member is None:
            return

        birthday_role = guild.get_role(settings.get("birthday_role_id") or 0)
        if birthday_role:
            if birthday_role.is_default() or birthday_role.managed:
                await self.svc.set_birthday_role(guild.id, None)
                log.warning("Birthday role in %s is invalid, cleared.", guild.name)
            elif guild.me.top_role <= birthday_role:
                log.warning("Cannot assign birthday role in %s — bot role too low.", guild.name)
            elif policy_block := await grant_block_reason(
                birthday_role, guild, getattr(self.bot, "permission_service", None)
            ):
                # Same grant-time re-check as self-roles: a role that became a
                # staff / moderator / bot-permission role is never handed out.
                log.warning("Birthday role %s not given in %s: %s", birthday_role.id, guild.name, policy_block)
            else:
                log.info(
                    "Adding birthday role: role_id=%s role_name=%s member_id=%s display_name=%s",
                    birthday_role.id, birthday_role.name, member.id, member.display_name,
                )
                try:
                    await member.add_roles(birthday_role, reason="Birthday")
                    log.info(
                        "Birthday role added: role_id=%s member_id=%s display_name=%s",
                        birthday_role.id, member.id, member.display_name,
                    )
                except discord.Forbidden:
                    log.exception("No permission to add birthday role in %s", guild.name)
                except discord.HTTPException as e:
                    log.exception("Failed to add birthday role to %s: %s", member, e)

        ch_id = settings.get("announce_channel_id")
        if ch_id:
            channel = guild.get_channel(ch_id)
            if channel and channel.permissions_for(guild.me).send_messages:
                log.info(
                    "Calling _send_announcement: member_id=%s display_name=%s channel_id=%s "
                    "announce_settings={announce_message: %r, announce_message_pl: %r, announce_ping: %s, ping_user: %s}",
                    member.id, member.display_name, channel.id,
                    settings.get("announce_message"), settings.get("announce_message_pl"),
                    settings.get("announce_ping"), settings.get("ping_user"),
                )
                await self._send_announcement(settings, channel, [member])

        log.info("Before mark_celebrated: guild=%s user=%s year=%s", guild.id, user_id, current_year)
        await self.svc.mark_celebrated(guild.id, user_id, current_year)
        log.info("After mark_celebrated: guild=%s user=%s year=%s", guild.id, user_id, current_year)
        log.info("Fired birthday: user=%s guild=%s", user_id, guild.name)

    async def _send_announcement(
        self,
        settings: dict,
        channel: discord.TextChannel,
        members: list[discord.Member],
    ) -> None:
        if not members:
            return

        log.info(
            "_send_announcement start: members=%s",
            [(m.id, m.display_name) for m in members],
        )

        default = "Please wish a happy birthday to {mention}!"
        if len(members) == 1:
            template = settings.get("announce_message") or default
        else:
            template = (
                settings.get("announce_message_pl")
                or settings.get("announce_message")
                or "Please wish a happy birthday to our esteemed members: {mention}"
            )

        log.info("_send_announcement template before placeholder replacement: %r", template)

        ping = bool(settings.get("announce_ping"))

        # Normalise line endings — modals send \r\n, old messages may have literal \n
        template = template.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")

        log.info("_send_announcement template after line-ending normalisation: %r", template)

        if len(members) == 1:
            ctx = RenderContext(guild=channel.guild, member=members[0], channel=channel)
            message = resolve_text(template, ctx)
        else:
            mentions = ", ".join(
                resolve_text("{mention}", RenderContext(guild=channel.guild, member=m, channel=channel))
                for m in members
            )
            names_only = ", ".join(
                resolve_text("{user}", RenderContext(guild=channel.guild, member=m, channel=channel))
                for m in members
            )
            message = template.replace("{mention}", mentions).replace("{user}", names_only)
            ctx = RenderContext(guild=channel.guild, member=members[0], channel=channel)
            message = resolve_text(message, ctx)

        # Legacy %n / %e support for messages saved before the placeholder system
        if "%n" in message:
            legacy_names = ", ".join(_format_name(m, ping) for m in members)
            message = message.replace("%n", legacy_names)
        if "%e" in message:
            everyone = channel.guild.default_role
            message = message.replace("%e", everyone.mention)

        log.info("_send_announcement final rendered message: %r", message)

        if settings.get("ping_user"):
            ping_msg = await channel.send(" ".join(m.mention for m in members))
            log.info(
                "Sent ping_user message: discord_message_id=%s channel_id=%s",
                ping_msg.id, channel.id,
            )
        # The bot's global allowed_mentions blocks @everyone (core/bot_factory.py).
        # `%e` is the one deliberate, staff-configured @everyone ping: a message
        # containing %e can only be SAVED by full staff (OWNER_ID / STAFF_ROLE_IDS,
        # services.birthday_service.everyone_block_reason, cog modal and dashboard),
        # so opt in only when the saved template itself contains it (not when a
        # member's name renders as "%e"). Role mentions typed into the template
        # never ping (roles=False): manage_birthdays holders must not be able to
        # mass-ping arbitrary roles; users still ping ({mention}).
        allow_everyone = "%e" in template
        sent_msg = await channel.send(
            message,
            allowed_mentions=discord.AllowedMentions(everyone=allow_everyone, roles=False, users=True),
        )
        log.info(
            "Sent announcement message: discord_message_id=%s channel_id=%s",
            sent_msg.id, channel.id,
        )

    async def cleanup_birthday_role(self, guild: discord.Guild) -> None:
        settings = await self.svc.get_settings(guild.id)
        role_id = settings.get("birthday_role_id")
        if not role_id:
            return
        birthday_role = guild.get_role(role_id)
        if birthday_role is None:
            return

        guild_tz = parse_timezone(settings.get("guild_timezone") or "UTC") or _UTC
        now_utc = datetime.now(_UTC)

        for member in birthday_role.members:
            row = await self.repo.get_birthday(guild.id, member.id)
            if row is None:
                # Role given by hand to someone with no birthday on file:
                # not ours to remove.
                continue
            tz = _member_tz(row, guild_tz)
            # Removed as soon as it is no longer their birthday locally (the
            # hourly loop, so within an hour of their local midnight). No
            # last_celebrated check: comparing it with the UTC year kept the
            # role on for a year across Dec 31/Jan 1.
            if should_remove_birthday_role(row["birth_month"], row["birth_day"], tz, now_utc):
                try:
                    await member.remove_roles(birthday_role, reason="Birthday ended")
                except (discord.Forbidden, discord.HTTPException):
                    pass


# ------------------------------------------------------------------ #
# BirthdayCog — /birthday group + scheduler loops                     #
# ------------------------------------------------------------------ #

class BirthdayCog(commands.Cog, name="Birthday"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.core: BirthdayCore = bot.birthday_core  # type: ignore[attr-defined]
        self.reschedule_loop.start()
        self.role_cleanup_loop.start()

    def cog_unload(self):
        self.reschedule_loop.cancel()
        self.role_cleanup_loop.cancel()
        for task in self.core.scheduled.values():
            task.cancel()
        self.core.scheduled.clear()

    @tasks.loop(hours=168)
    async def reschedule_loop(self):
        log.info("Weekly birthday reschedule")
        await self.core.schedule_all()

    @reschedule_loop.before_loop
    async def before_reschedule(self):
        await self.bot.wait_until_ready()
        await self.core.schedule_all()

    @tasks.loop(hours=1)
    async def role_cleanup_loop(self):
        for guild in self.bot.guilds:
            try:
                await self.core.cleanup_birthday_role(guild)
            except Exception as e:
                log.error("Role cleanup error guild=%s: %s", guild.id, e)

    @role_cleanup_loop.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # /birthday                                                            #
    # ------------------------------------------------------------------ #

    birthday_group = app_commands.Group(
        name="birthday",
        description="Commands relating to birthdays.",
    )

    @birthday_group.command(name="get", description="Gets a user's birthday.")
    @app_commands.describe(member="The user to look up (leave blank for yourself)")
    async def cmd_get(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ):
        settings = await self.core.svc.get_settings(interaction.guild.id)
        target = member or interaction.user
        row = await self.core.svc.get_birthday(interaction.guild.id, target.id)
        if row is None:
            if member is None:
                await interaction.response.send_message(
                    "You do not have your birthday registered.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "The given user does not have their birthday registered.", ephemeral=True
                )
            return
        date_str = format_birthday(row["birth_month"], row["birth_day"])
        tz_note = f" — {row['user_timezone']}" if row.get("user_timezone") else ""
        await interaction.response.send_message(
            f"{target.mention}: `{date_str}`{tz_note}",
            ephemeral=_ephemeral(settings),
        )

    @birthday_group.command(
        name="show-nearest",
        description="Get a list of users who recently had or will have a birthday.",
    )
    async def cmd_show_nearest(self, interaction: discord.Interaction):
        await interaction.response.defer()
        settings = await self.core.svc.get_settings(interaction.guild.id)
        guild_tz = parse_timezone(settings.get("guild_timezone") or "UTC") or _UTC
        now_local = datetime.now(guild_tz)

        all_rows = await self.core.svc.get_all_for_guild(interaction.guild.id)
        groups = nearest_birthdays(all_rows, now_local.date())

        if not groups:
            await interaction.followup.send(
                "There are no recent or upcoming birthdays (within the last 7 days and/or next 14 days)."
            )
            return

        lines: list[str] = []
        for group in groups:
            date_str = format_birthday(group["month"], group["day"])
            names = []
            for row in group["rows"]:
                mem = interaction.guild.get_member(row["user_id"])
                names.append(mem.display_name if mem else f"<@{row['user_id']}>")
            lines.append(f"● `{date_str}`: {', '.join(names)}")

        # Paginate at 800 chars like the original
        pages: list[str] = []
        current_page: list[str] = []
        current_len = 0
        for line in lines:
            if current_len + len(line) + 1 > 800 and current_page:
                pages.append("\n".join(current_page))
                current_page = []
                current_len = 0
            current_page.append(line)
            current_len += len(line) + 1
        if current_page:
            pages.append("\n".join(current_page))

        first_embed = discord.Embed(
            title="Recent & Upcoming Birthdays",
            description=pages[0],
            color=discord.Color.pink(),
        )
        await interaction.followup.send(embed=first_embed)
        for page in pages[1:]:
            await interaction.followup.send(embed=discord.Embed(description=page, color=discord.Color.pink()))

    set_group = app_commands.Group(
        name="set",
        description="Set your birthday or timezone.",
        parent=birthday_group,
    )

    @set_group.command(name="date", description="Sets or updates your birthday.")
    @app_commands.describe(
        day="Day of the month (e.g. 14)",
        month="Month name or number (e.g. February or 2)",
        timezone="Your timezone (e.g. Europe/London) — skipping clears it",
    )
    async def cmd_set_date(
        self,
        interaction: discord.Interaction,
        day: str,
        month: str,
        timezone: str | None = None,
    ):
        settings = await self.core.svc.get_settings(interaction.guild.id)
        ok, msg = await self.core.svc.set_birthday(
            interaction.guild.id, interaction.user.id, day, month, tz_str=timezone
        )
        if ok:
            await self.core.schedule_guild(interaction.guild.id)
            # Tip if no timezone set (matches OG bot behaviour)
            if not timezone:
                row = await self.core.svc.get_birthday(interaction.guild.id, interaction.user.id)
                if not (row and row.get("user_timezone")):
                    msg += "\nTip: Use `/birthday set timezone` to ensure your birthday is recognised on time."
        await interaction.response.send_message(
            f"✓ {msg}" if ok else f":x: {msg}",
            ephemeral=not ok or _ephemeral(settings),
        )

    @cmd_set_date.autocomplete("timezone")
    async def tz_ac_date(self, interaction: discord.Interaction, current: str):
        return _tz_choices(current)

    @set_group.command(name="timezone", description="Sets or updates your timezone if your birthday is already set.")
    @app_commands.describe(timezone="Your timezone (e.g. America/New_York)")
    async def cmd_set_timezone(self, interaction: discord.Interaction, timezone: str):
        settings = await self.core.svc.get_settings(interaction.guild.id)
        ok, msg = await self.core.svc.set_timezone(
            interaction.guild.id, interaction.user.id, timezone
        )
        if ok:
            await self.core.schedule_guild(interaction.guild.id)
        await interaction.response.send_message(
            f"✓ {msg}" if ok else f":x: {msg}",
            ephemeral=not ok or _ephemeral(settings),
        )

    @cmd_set_timezone.autocomplete("timezone")
    async def tz_ac_tz(self, interaction: discord.Interaction, current: str):
        return _tz_choices(current)

    @birthday_group.command(name="remove", description="Removes your birthday information from this bot.")
    async def cmd_remove(self, interaction: discord.Interaction):
        settings = await self.core.svc.get_settings(interaction.guild.id)
        removed = await self.core.svc.remove_birthday(interaction.guild.id, interaction.user.id)
        if removed:
            self.core.cancel_scheduled(interaction.guild.id, interaction.user.id)
            await interaction.response.send_message(
                "✓ Your birthday in this server has been removed.",
                ephemeral=_ephemeral(settings),
            )
        else:
            await interaction.response.send_message(
                "✓ Your birthday is not registered.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    bot.birthday_core = BirthdayCore(bot)  # type: ignore[attr-defined]
    await bot.add_cog(BirthdayCog(bot))
