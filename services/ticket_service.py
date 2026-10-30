from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re

import discord

import config as _config
from database.repositories.ticket_repo import TicketRepository
from database.repositories.template_repo import TemplateRepository
from core.placeholder_engine import RenderContext
from core.renderer import TemplateRenderer
from services.live_events import publish as live_publish

log = logging.getLogger(__name__)

TICKET_OPENED_TEMPLATE_NAME = "ticket-opened"
TICKET_CLOSED_TEMPLATE_NAME = "ticket-closed"
TICKET_CLAIMED_TEMPLATE_NAME = "ticket-claimed"
VERIFY_TICKET_WELCOME_TEMPLATE_NAME = _config.VERIFY_TICKET_WELCOME_TEMPLATE_NAME


def parse_purge_days(value: object) -> int | None:
    """The archived-ticket purge window in days, or None when it is unusable.

    Second line of defence behind the Settings page's write validation: a
    value below 1 (or one that is not a whole number) would put the purge
    cutoff at "now" or in the future and wipe every archived ticket, so the
    purge job skips the run instead."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        days = value
    else:
        try:
            days = int(str(value).strip())
        except (TypeError, ValueError):
            return None
    return days if days >= 1 else None


# Ticket lifecycle: open -> closed -> archived.
#   Close (staff only) locks the opener out at once, posts the closed notice,
#   and CLOSE_DELETE_DELAY_SECONDS later archives the ticket: the Discord
#   channel is deleted and the record + chat log stay on the dashboard.
#   "closed" is only that short in-between state. The delay is timed from
#   tickets.closed_at, so a crash or restart inside the window is caught by
#   finish_overdue_closes (the cog runs it at startup and every few minutes).
CLOSE_DELETE_DELAY_SECONDS = 30

# Weekly sweep: open tickets with no message for STALE_TICKET_DAYS are closed
# by the bot. The last run is the newest STALE_SWEEP_ACTION audit_log row, so
# a restart never resets the week.
STALE_TICKET_DAYS = 7
STALE_SWEEP_INTERVAL = _dt.timedelta(days=7)
STALE_SWEEP_ACTION = "ticket_stale_sweep"


def _parse_utc(value: object) -> _dt.datetime | None:
    """SQLite 'YYYY-MM-DD HH:MM:SS' (or ISO, optional Z) UTC text -> aware datetime."""
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def _db_ts(value: _dt.datetime) -> str:
    """Aware datetime -> the naive-UTC text CURRENT_TIMESTAMP writes, for string comparison."""
    return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class TicketService:
    def __init__(
        self,
        ticket_repo: TicketRepository,
        template_repo: TemplateRepository,
        renderer: TemplateRenderer,
        bot: discord.Client,
        audit=None,
    ) -> None:
        self._tickets = ticket_repo
        self._templates = template_repo
        self._renderer = renderer
        self._bot = bot
        # Optional AuditService: close/archive/delete write an audit_log row
        # naming the acting member. None-safe for tests and old call sites.
        self._audit = audit
        # One lock per (guild, panel): serializes "check open count -> number
        # -> create channel -> insert row", so two quick clicks by the same
        # member cannot both pass the max-open check (the orphan-channel
        # race), and two different members cannot both draw the same
        # ticket_number (UNIQUE per guild+panel). Instance-level is enough:
        # the internal API uses this same instance (cog.ticket_service).
        self._open_locks: dict[tuple[int, int], asyncio.Lock] = {}
        # Instance attribute so tests can shorten it. Pending delayed-delete
        # tasks are kept referenced here so they are not garbage collected.
        self.close_delete_delay = CLOSE_DELETE_DELAY_SECONDS
        self._delete_tasks: set[asyncio.Task] = set()

    def _open_lock(self, guild_id: int, panel_id: int) -> asyncio.Lock:
        key = (guild_id, panel_id)
        lock = self._open_locks.get(key)
        if lock is None:
            lock = self._open_locks[key] = asyncio.Lock()
        return lock

    async def _audit_log(self, guild_id: int, actor_id: int | None, action: str, ticket_id: int, detail: dict) -> None:
        if self._audit is None or actor_id is None:
            return
        try:
            await self._audit.log(guild_id, actor_id, action, target_type="ticket", target_id=ticket_id, detail=detail)
        except Exception:
            log.exception("ticket_service: audit log failed for %s on ticket %s", action, ticket_id)

    # ------------------------------------------------------------------
    # Panel management
    # ------------------------------------------------------------------

    async def create_panel(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        actor_id: int,
        open_category: discord.CategoryChannel | None = None,
        closed_category: discord.CategoryChannel | None = None,
        transcript_channel: discord.TextChannel | None = None,
        template_name: str | None = None,
        max_open_per_user: int = 1,
        naming_pattern: str = "ticket-{number}",
        button_label: str = "Open Ticket",
        button_emoji: str | None = None,
        purpose: str = "support",
    ) -> tuple[int | None, list[str]]:
        template_id = None
        if template_name:
            tmpl = await self._templates.get_by_name(guild.id, template_name)
            if not tmpl:
                return (None, [f"No template named '{template_name}' found."])
            template_id = tmpl["id"]

        panel_id = await self._tickets.create_panel(
            guild_id=guild.id,
            channel_id=channel.id,
            created_by=actor_id,
            template_id=template_id,
            open_category_id=open_category.id if open_category else None,
            closed_category_id=closed_category.id if closed_category else None,
            transcript_channel_id=transcript_channel.id if transcript_channel else None,
            max_open_per_user=max_open_per_user,
            naming_pattern=naming_pattern,
            button_label=button_label,
            button_emoji=button_emoji,
            purpose=purpose,
        )
        return (panel_id, [])

    async def post_panel(
        self,
        panel_id: int,
        guild: discord.Guild,
    ) -> tuple[int | None, list[str]]:
        from cogs.tickets import TicketPanelView

        panel = await self._tickets.get_panel(panel_id)
        if not panel:
            return (None, ["Panel not found."])

        channel = guild.get_channel(panel["channel_id"])
        if not channel:
            return (None, ["Panel channel not found."])

        content = None
        embeds = []

        if panel["template_id"]:
            template_full = await self._templates.get_full(panel["template_id"])
            if template_full:
                ctx = RenderContext(guild=guild, member=None, channel=channel)
                try:
                    rendered = self._renderer.render_message(template_full, ctx)
                    content = rendered.get("content") or None
                    embeds = rendered.get("embeds") or []
                except Exception:
                    log.exception("ticket_service: render error for panel %d", panel_id)

        view = TicketPanelView(panel_id, panel["button_label"], panel.get("button_emoji"))

        try:
            msg = await channel.send(content=content, embeds=embeds[:10], view=view)
        except discord.HTTPException as exc:
            return (None, [f"Failed to send panel: {exc}"])

        await self._tickets.set_panel_message(panel_id, msg.id)
        return (msg.id, [])

    async def list_panels(self, guild_id: int) -> list[dict]:
        return await self._tickets.list_panels(guild_id)

    async def get_panel(self, panel_id: int) -> dict | None:
        return await self._tickets.get_panel(panel_id)

    async def delete_panel(self, panel_id: int, guild: discord.Guild) -> list[str]:
        panel = await self._tickets.get_panel(panel_id)
        if not panel or panel["guild_id"] != guild.id:
            return ["Panel not found."]

        if panel.get("message_id"):
            channel = guild.get_channel(panel["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(panel["message_id"])
                    await msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

        await self._tickets.delete_panel(panel_id)
        return []

    # ------------------------------------------------------------------
    # Panel support roles
    # ------------------------------------------------------------------

    async def add_panel_role(
        self,
        panel_id: int,
        guild: discord.Guild,
        role: discord.Role,
    ) -> list[str]:
        panel = await self._tickets.get_panel(panel_id)
        if not panel or panel["guild_id"] != guild.id:
            return ["Panel not found."]

        await self._tickets.add_panel_role(panel_id, role.id)
        return []

    async def remove_panel_role(
        self,
        panel_id: int,
        guild: discord.Guild,
        role: discord.Role,
    ) -> list[str]:
        panel = await self._tickets.get_panel(panel_id)
        if not panel or panel["guild_id"] != guild.id:
            return ["Panel not found."]

        await self._tickets.remove_panel_role(panel_id, role.id)
        return []

    async def get_panel_roles(self, panel_id: int, guild: discord.Guild) -> tuple[list[dict] | None, list[str]]:
        panel = await self._tickets.get_panel(panel_id)
        if not panel or panel["guild_id"] != guild.id:
            return (None, ["Panel not found."])

        roles = await self._tickets.get_panel_roles(panel_id)
        return (roles, [])

    # ------------------------------------------------------------------
    # Ticket lifecycle message helper
    # ------------------------------------------------------------------

    async def _render_lifecycle_template(
        self,
        guild: discord.Guild,
        template_name: str,
        *,
        member: discord.Member | None,
        channel: discord.abc.GuildChannel | None,
        actor: discord.Member | None = None,
        ticket_number: int | None = None,
    ) -> tuple[str | None, list[discord.Embed]] | None:
        """Look up `template_name` for this guild and render it. Returns None if the
        template doesn't exist, is empty, or fails to render — callers fall back to
        their hardcoded embed in that case (matching cogs/verify.py's pattern)."""
        tmpl_row = await self._templates.get_by_name(guild.id, template_name)
        if not tmpl_row:
            return None

        template_full = await self._templates.get_full(tmpl_row["id"])
        if not template_full:
            return None

        ctx = RenderContext(
            guild=guild,
            member=member,
            channel=channel,
            actor=actor,
            ticket_number=ticket_number,
        )
        try:
            rendered = self._renderer.render_message(template_full, ctx)
        except Exception:
            log.exception("ticket_service: render error for template '%s'", template_name)
            return None

        content = rendered.get("content") or None
        embeds = rendered.get("embeds") or []
        if not content and not embeds:
            return None

        return (content, embeds)

    # ------------------------------------------------------------------
    # Ticket lifecycle
    # ------------------------------------------------------------------

    async def open_ticket(
        self,
        panel_id: int,
        guild: discord.Guild,
        member: discord.Member,
    ) -> tuple[discord.TextChannel | None, list[str]]:
        panel = await self._tickets.get_panel(panel_id)
        if not panel:
            return (None, ["Panel not found."])

        async with self._open_lock(guild.id, panel_id):
            created = await self._create_ticket_locked(panel, panel_id, guild, member)
        if isinstance(created, list):
            return (None, created)
        ticket_channel, ticket_id, ticket_number = created
        await self._post_opened_message(panel, guild, member, ticket_channel, ticket_id, ticket_number)
        return (ticket_channel, [])

    async def _create_ticket_locked(
        self,
        panel: dict,
        panel_id: int,
        guild: discord.Guild,
        member: discord.Member,
    ) -> tuple[discord.TextChannel, int, int] | list[str]:
        """Runs under _open_lock. The open-count check happens HERE, inside
        the lock, so a second click waits for the first ticket's row and then
        sees it. If the row insert fails after the channel was created, the
        channel is deleted so no orphan is left behind. Returns
        (channel, ticket_id, ticket_number) or a list of error strings."""
        open_count = await self._tickets.count_open_by_user(guild.id, member.id, panel_id)
        if open_count >= panel["max_open_per_user"]:
            return [f"You already have {open_count} open ticket(s). Please close it before opening a new one."]

        ticket_number = await self._tickets.next_ticket_number(guild.id, panel_id)
        safe_username = re.sub(r"[^a-z0-9-]", "-", member.name.lower()).strip("-") or str(member.id)

        base_name = (
            panel["naming_pattern"]
            .replace("{number}", str(ticket_number))
            .replace("{username}", safe_username)
        )

        if "{username}" in panel["naming_pattern"]:
            prior_count = await self._tickets.count_all_by_user(guild.id, member.id, panel_id)
            if prior_count > 0:
                channel_name = f"{base_name}-{prior_count + 1}"
            else:
                channel_name = base_name
        else:
            channel_name = base_name

        channel_name = channel_name[:100]  # Discord channel name hard limit, defensive

        # Determine category
        category = None
        if panel.get("open_category_id"):
            category = guild.get_channel(panel["open_category_id"])

        # Build permission overwrites: deny everyone, allow member + support roles
        panel_roles = await self._tickets.get_panel_roles(panel_id)
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
            ),
        }
        for pr in panel_roles:
            role = guild.get_role(pr["role_id"])
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                )

        try:
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                reason=f"Ticket #{ticket_number} opened by {member}",
            )
        except discord.HTTPException as exc:
            return [f"Failed to create ticket channel: {exc}"]

        try:
            ticket_id = await self._tickets.create_ticket(
                guild_id=guild.id,
                panel_id=panel_id,
                channel_id=ticket_channel.id,
                opened_by=member.id,
                ticket_number=ticket_number,
            )
        except Exception:
            log.exception("ticket_service: ticket row insert failed; deleting channel %s", ticket_channel.id)
            try:
                await ticket_channel.delete(reason="Ticket could not be recorded; cleaning up")
            except discord.HTTPException:
                log.warning("ticket_service: could not delete orphan ticket channel %s", ticket_channel.id)
            return ["Failed to open the ticket. Please try again."]

        live_publish("tickets", {
            "kind": "opened", "ticket_id": ticket_id, "guild_id": guild.id, "panel_id": panel_id,
            "channel_id": ticket_channel.id, "opened_by": member.id, "ticket_number": ticket_number,
        })
        return (ticket_channel, ticket_id, ticket_number)

    async def _post_opened_message(
        self,
        panel: dict,
        guild: discord.Guild,
        member: discord.Member,
        ticket_channel: discord.TextChannel,
        ticket_id: int,
        ticket_number: int,
    ) -> None:
        # Post the control message inside the ticket. Verify-purpose panels get the
        # "Verify & Close" control view (no Claim button); regular panels keep the
        # normal Close/Claim view.
        from cogs.tickets import TicketControlView, TicketVerifyControlView
        if panel.get("purpose") == "verify":
            view = TicketVerifyControlView(ticket_id)
        else:
            view = TicketControlView(ticket_id)

        # Verify-purpose panels get a distinct welcome template — "a staff member
        # will verify you here shortly" instead of the generic support-ticket
        # "Support will be with you shortly" copy. Regular support panels are
        # unaffected and keep using TICKET_OPENED_TEMPLATE_NAME as before.
        is_verify_panel = panel.get("purpose") == "verify"
        opened_template_name = (
            VERIFY_TICKET_WELCOME_TEMPLATE_NAME if is_verify_panel else TICKET_OPENED_TEMPLATE_NAME
        )

        rendered = await self._render_lifecycle_template(
            guild,
            opened_template_name,
            member=member,
            channel=ticket_channel,
            ticket_number=ticket_number,
        )
        if rendered is not None:
            content, embeds = rendered
        else:
            content = None
            if is_verify_panel:
                embeds = [
                    discord.Embed(
                        title=f"Ticket #{ticket_number}",
                        description=(
                            f"Welcome, {member.mention}!\n\n"
                            "A staff member will verify you here shortly.\n"
                            "This channel is private to you and the staff team."
                        ),
                        color=discord.Color.green(),
                    )
                ]
            else:
                embeds = [
                    discord.Embed(
                        title=f"Ticket #{ticket_number}",
                        description=(
                            f"Opened by {member.mention}\n\n"
                            "Support will be with you shortly.\n"
                            "Use the buttons below to manage this ticket."
                        ),
                        color=discord.Color.green(),
                    )
                ]

        try:
            control_msg = await ticket_channel.send(content=content, embeds=embeds[:10], view=view)
        except discord.HTTPException:
            control_msg = None

        if control_msg is not None:
            # Stored so claim_ticket can find-and-edit this exact message later (to
            # disable the Claim button) regardless of whether the claim comes from
            # the live Discord button interaction or the dashboard. See migration
            # 023's docstring.
            await self._tickets.set_control_message_id(ticket_id, control_msg.id)

    async def get_ticket(self, ticket_id: int) -> dict | None:
        return await self._tickets.get_ticket(ticket_id)

    async def get_ticket_by_channel(self, channel_id: int) -> dict | None:
        return await self._tickets.get_ticket_by_channel(channel_id)

    async def list_all_tickets(self, guild_id: int, limit: int = 100) -> list[dict]:
        return await self._tickets.list_all(guild_id, limit)

    async def list_open_tickets(self, guild_id: int) -> list[dict]:
        return await self._tickets.list_open_tickets(guild_id)

    async def list_tickets_by_status(self, guild_id: int, status: str) -> list[dict]:
        return await self._tickets.list_by_status(guild_id, status)

    async def close_ticket(
        self,
        ticket_id: int,
        guild: discord.Guild,
        actor: discord.Member,
        *,
        note: str | None = None,
    ) -> list[str]:
        """Close: lock the opener out, post the closed notice (plus the
        transcript if configured), then delete the channel after
        close_delete_delay seconds (archive_ticket). Callers gate who may
        close (staff only: manage_tickets / verify_members / the dashboard).
        `note` is an extra line for the notice (the stale sweep uses it)."""
        ticket = await self._tickets.get_ticket(ticket_id)
        if not ticket or ticket["guild_id"] != guild.id:
            return ["Ticket not found."]
        if ticket["status"] != "open":
            return ["This ticket is already closed."]
        if not await self._tickets.transition_status(ticket_id, ("open",), "closed"):
            return ["This ticket is already closed."]

        panel = await self._tickets.get_panel(ticket["panel_id"])
        channel = guild.get_channel(ticket["channel_id"])

        live_publish("tickets", {"kind": "closed", "ticket_id": ticket_id, "guild_id": guild.id, "actor_user_id": actor.id})
        await self._audit_log(guild.id, actor.id, "ticket_closed", ticket_id, {
            "ticket_number": ticket.get("ticket_number"), "opened_by": ticket.get("opened_by"),
        })

        if channel:
            opener = guild.get_member(ticket["opened_by"]) if ticket.get("opened_by") else None
            await self._lock_opener(channel, opener, actor)

            rendered = await self._render_lifecycle_template(
                guild,
                TICKET_CLOSED_TEMPLATE_NAME,
                member=opener,
                channel=channel,
                actor=actor,
                ticket_number=ticket.get("ticket_number"),
            )
            if rendered is not None:
                content, embeds = rendered
            else:
                content = None
                embeds = [
                    discord.Embed(
                        description=f"Ticket closed by {actor.mention}.",
                        color=discord.Color.red(),
                    )
                ]
            notice = f"This channel will be deleted in {self.close_delete_delay} seconds."
            if note:
                notice = f"{note}\n{notice}"
            embeds = [*embeds[:9], discord.Embed(description=notice, color=discord.Color.greyple())]

            try:
                await channel.send(content=content, embeds=embeds)
            except discord.HTTPException:
                pass

        # Post transcript if configured (reads the channel, so before the delete)
        if panel and panel.get("transcript_channel_id"):
            await self._send_transcript(ticket, guild, panel, actor)

        self._schedule_delete(ticket_id, guild.id, actor.id)
        return []

    async def _lock_opener(
        self,
        channel: discord.abc.GuildChannel,
        opener: discord.Member | None,
        actor: discord.Member,
    ) -> None:
        """The opener keeps reading (they see the closed notice) but can no
        longer post. Best-effort; a departed opener cannot post anyway."""
        if opener is None:
            return
        try:
            await channel.set_permissions(
                opener,
                view_channel=True,
                read_message_history=True,
                send_messages=False,
                send_messages_in_threads=False,
                add_reactions=False,
                attach_files=False,
                reason=f"Ticket closed by {actor}",
            )
        except discord.HTTPException:
            log.warning("ticket_service: could not lock closed ticket channel %s", channel.id)

    def _schedule_delete(self, ticket_id: int, guild_id: int, actor_id: int) -> None:
        """In-process timer for the normal case. Not the only path: if the bot
        stops before it fires, finish_overdue_closes picks the ticket up."""
        task = asyncio.get_running_loop().create_task(self._delete_after_delay(ticket_id, guild_id, actor_id))
        self._delete_tasks.add(task)
        task.add_done_callback(self._delete_tasks.discard)

    async def _delete_after_delay(self, ticket_id: int, guild_id: int, actor_id: int) -> None:
        await asyncio.sleep(self.close_delete_delay)
        try:
            guild = self._bot.get_guild(guild_id) if self._bot is not None else None
            if guild is None:
                return
            actor = guild.get_member(actor_id) or guild.me
            await self.archive_ticket(ticket_id, guild, actor)
        except Exception:
            log.exception("ticket_service: delayed channel delete failed for ticket %d", ticket_id)

    async def finish_overdue_closes(self, guild: discord.Guild, now: _dt.datetime | None = None) -> list[int]:
        """Archive every ticket still 'closed' whose close is older than the
        delete delay (the in-process timer was lost to a crash or restart).
        Returns the archived ticket IDs."""
        now = now or _dt.datetime.now(_dt.timezone.utc)
        cutoff = _db_ts(now - _dt.timedelta(seconds=self.close_delete_delay))
        done: list[int] = []
        for ticket in await self._tickets.list_by_status(guild.id, "closed"):
            closed_at = ticket.get("closed_at")
            if closed_at and str(closed_at) > cutoff:
                continue  # still inside its window; the timer (or the next run) handles it
            if not await self.archive_ticket(ticket["id"], guild, guild.me):
                done.append(ticket["id"])
        return done

    @staticmethod
    def _last_activity(ticket: dict, channel: discord.abc.GuildChannel) -> _dt.datetime | None:
        """Newest of opened_at and the channel's last message (any author)."""
        opened = _parse_utc(ticket.get("opened_at"))
        last_id = getattr(channel, "last_message_id", None)
        last = discord.utils.snowflake_time(last_id) if last_id else None
        candidates = [t for t in (opened, last) if t is not None]
        return max(candidates) if candidates else None

    async def close_stale_open_tickets(
        self,
        guild: discord.Guild,
        idle_days: int = STALE_TICKET_DAYS,
        now: _dt.datetime | None = None,
    ) -> list[int]:
        """Close every open ticket with no message for `idle_days`, as the
        bot. A ticket whose channel was deleted by hand is archived directly.
        Returns the closed/archived ticket IDs."""
        now = now or _dt.datetime.now(_dt.timezone.utc)
        cutoff = now - _dt.timedelta(days=idle_days)
        done: list[int] = []
        for ticket in await self._tickets.list_open_tickets(guild.id):
            channel = guild.get_channel(ticket["channel_id"])
            if channel is None:
                if not await self.archive_ticket(ticket["id"], guild, guild.me):
                    done.append(ticket["id"])
                continue
            last = self._last_activity(ticket, channel)
            if last is None or last > cutoff:
                continue
            errors = await self.close_ticket(
                ticket["id"], guild, guild.me,
                note=f"Closed automatically after {idle_days} days with no activity.",
            )
            if not errors:
                done.append(ticket["id"])
        return done

    async def run_weekly_stale_sweep(self, guild: discord.Guild, now: _dt.datetime | None = None) -> list[int] | None:
        """close_stale_open_tickets at most once per STALE_SWEEP_INTERVAL.
        Returns None when skipped (last sweep too recent, or no audit
        service to remember runs in)."""
        if self._audit is None:
            return None
        now = now or _dt.datetime.now(_dt.timezone.utc)
        page = await self._audit.query(guild.id, action=STALE_SWEEP_ACTION, limit=1)
        entries = page.get("entries") or []
        last = _parse_utc(entries[0].get("created_at")) if entries else None
        if last is not None and now - last < STALE_SWEEP_INTERVAL:
            return None
        done = await self.close_stale_open_tickets(guild, now=now)
        await self._audit.log(
            guild.id, guild.me.id, STALE_SWEEP_ACTION, target_type="ticket",
            detail={"closed_ticket_ids": done, "idle_days": STALE_TICKET_DAYS},
        )
        return done

    async def archive_ticket(
        self,
        ticket_id: int,
        guild: discord.Guild,
        actor: discord.Member,
    ) -> list[str]:
        """Archive = delete the Discord channel now; the record and chat log
        stay on the dashboard until deleted there or purged. Runs after the
        close delay, for departed members, and from the dashboard API."""
        ticket = await self._tickets.get_ticket(ticket_id)
        if not ticket or ticket["guild_id"] != guild.id:
            return ["Ticket not found."]
        if ticket["status"] not in ("open", "closed"):
            return ["Ticket is already archived."]
        if not await self._tickets.transition_status(ticket_id, ("open", "closed"), "archived"):
            return ["Ticket is already archived."]

        live_publish("tickets", {"kind": "archived", "ticket_id": ticket_id, "guild_id": guild.id, "actor_user_id": actor.id})
        await self._audit_log(guild.id, actor.id, "ticket_archived", ticket_id, {
            "ticket_number": ticket.get("ticket_number"), "opened_by": ticket.get("opened_by"),
            "channel": "deleted",
        })
        await self._delete_leftover_channel(guild, ticket.get("channel_id"), f"Ticket archived by {actor}")
        return []

    async def archive_tickets_for_departed_member(
        self,
        guild: discord.Guild,
        user_id: int,
    ) -> list[int]:
        """Archive every open/closed ticket a member opened after they leave or
        are kicked — nobody's left to respond in a channel gated to a member
        who's no longer in the guild. Returns the archived ticket IDs."""
        tickets = await self._tickets.list_unarchived_by_user(guild.id, user_id)
        archived_ids: list[int] = []
        for ticket in tickets:
            errors = await self.archive_ticket(ticket["id"], guild, guild.me)
            if not errors:
                archived_ids.append(ticket["id"])
        return archived_ids

    async def claim_ticket(
        self,
        ticket_id: int,
        guild: discord.Guild,
        actor: discord.Member,
    ) -> list[str]:
        ticket = await self._tickets.get_ticket(ticket_id)
        if not ticket or ticket["guild_id"] != guild.id:
            return ["Ticket not found."]
        if ticket["status"] != "open":
            return ["Can only claim open tickets."]
        if ticket.get("claimed_by") is not None:
            return ["This ticket has already been claimed."]

        # Atomic claim: claim_ticket() only updates the row if claimed_by is
        # still NULL, so two near-simultaneous claims (e.g. a stale cached
        # button plus a fresh click) can't both "win" — the loser gets
        # rejected here rather than silently reassigning the ticket.
        claimed = await self._tickets.claim_ticket(ticket_id, actor.id)
        if not claimed:
            return ["This ticket has already been claimed."]
        live_publish("tickets", {"kind": "claimed", "ticket_id": ticket_id, "guild_id": guild.id, "claimed_by": actor.id})

        channel = guild.get_channel(ticket["channel_id"])
        if channel:
            opener = guild.get_member(ticket["opened_by"]) if ticket.get("opened_by") else None

            rendered = await self._render_lifecycle_template(
                guild,
                TICKET_CLAIMED_TEMPLATE_NAME,
                member=opener,
                channel=channel,
                actor=actor,
                ticket_number=ticket.get("ticket_number"),
            )
            if rendered is not None:
                content, embeds = rendered
            else:
                content = None
                embeds = [
                    discord.Embed(
                        description=f"Ticket claimed by {actor.mention}.",
                        color=discord.Color.blurple(),
                    )
                ]

            try:
                await channel.send(content=content, embeds=embeds[:10])
            except discord.HTTPException:
                pass

            await self._disable_claim_button(channel, ticket, actor)

        return []

    async def _disable_claim_button(
        self,
        channel: discord.abc.GuildChannel,
        ticket: dict,
        claimed_by: discord.Member,
    ) -> None:
        """Edits the ticket's control message (Close/Claim or Verify &
        Close/Claim/Close) in place so the Claim button becomes a disabled
        "Claimed by @X" indicator. Best-effort: control_message_id is only
        populated for tickets opened after migration 023, and the message
        may have been deleted/is otherwise unreachable — either case just
        leaves the button as-is rather than failing the claim itself."""
        control_message_id = ticket.get("control_message_id")
        if not control_message_id:
            return

        from cogs.tickets import TicketControlView, TicketVerifyControlView

        panel = await self._tickets.get_panel(ticket["panel_id"])
        is_verify_panel = bool(panel and panel.get("purpose") == "verify")
        view_cls = TicketVerifyControlView if is_verify_panel else TicketControlView
        new_view = view_cls(ticket["id"], claimed_by_label=claimed_by.display_name)

        try:
            msg = await channel.fetch_message(control_message_id)
            await msg.edit(view=new_view)
        except (discord.NotFound, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Transcript
    # ------------------------------------------------------------------

    async def _send_transcript(
        self,
        ticket: dict,
        guild: discord.Guild,
        panel: dict,
        actor: discord.Member,
    ) -> None:
        transcript_channel = guild.get_channel(panel["transcript_channel_id"])
        if not transcript_channel:
            return

        ticket_channel = guild.get_channel(ticket["channel_id"])
        lines: list[str] = []

        if ticket_channel:
            try:
                async for msg in ticket_channel.history(limit=500, oldest_first=True):
                    if msg.author.bot:
                        continue
                    ts = msg.created_at.strftime("%Y-%m-%d %H:%M")
                    lines.append(f"[{ts}] {msg.author.display_name}: {msg.content}")
            except discord.HTTPException:
                pass

        opened_by = guild.get_member(ticket["opened_by"])
        opened_name = opened_by.display_name if opened_by else str(ticket["opened_by"])

        embed = discord.Embed(
            title=f"Transcript — Ticket #{ticket['ticket_number']}",
            description="\n".join(lines[:50]) if lines else "*No messages recorded.*",
            color=discord.Color.greyple(),
        )
        embed.add_field(name="Opened by", value=opened_name, inline=True)
        embed.add_field(name="Closed by", value=actor.display_name, inline=True)
        embed.add_field(name="Messages", value=str(len(lines)), inline=True)

        try:
            await transcript_channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("ticket_service: failed to send transcript")

    # ------------------------------------------------------------------
    # Archived-ticket deletion (dashboard) + 30-day auto-purge
    # ------------------------------------------------------------------

    async def delete_ticket(self, ticket_id: int, guild: discord.Guild, actor_id: int | None = None) -> list[str]:
        """Hard-deletes an ARCHIVED ticket's DB row (and its ticket_messages).
        If the channel still exists (archived in place under the old rule, or
        a delete that failed), it is deleted too, best-effort. Only archived
        tickets are eligible: open/closed tickets still have an active
        support flow, so deleting their row out from under that would strand
        the channel with no matching DB record."""
        ticket = await self._tickets.get_ticket(ticket_id)
        if not ticket or ticket["guild_id"] != guild.id:
            return ["Ticket not found."]
        if ticket["status"] != "archived":
            return ["Only archived tickets can be deleted."]

        await self._delete_leftover_channel(guild, ticket.get("channel_id"), "Archived ticket deleted")
        await self._tickets.delete_ticket(ticket_id)
        live_publish("tickets", {"kind": "deleted", "ticket_id": ticket_id, "guild_id": guild.id})
        await self._audit_log(guild.id, actor_id, "ticket_deleted", ticket_id, {
            "ticket_number": ticket.get("ticket_number"), "opened_by": ticket.get("opened_by"),
        })
        return []

    @staticmethod
    async def _delete_leftover_channel(guild: discord.Guild | None, channel_id: int | None, reason: str) -> None:
        if guild is None or not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.delete(reason=reason)
        except discord.HTTPException:
            log.warning("ticket_service: could not delete leftover ticket channel %s", channel_id)

    async def purge_old_archived_tickets(self, days: int) -> list[int]:
        """Deletes every archived ticket (across all guilds) whose
        archived_at is older than `days` days ago. Returns the deleted
        ticket IDs so the caller (cogs/tickets.py's daily purge loop) can
        log a count. A channel that still exists (archived in place under
        the old lock-instead-of-delete rule, or a delete that failed) is
        deleted first, best-effort.

        Refuses (returns []) when `days` is not a whole number >= 1. The
        cog's purge loop already checks this; this is the last line."""
        safe_days = parse_purge_days(days)
        if safe_days is None:
            log.warning("ticket_service: refusing to purge archived tickets with days=%r", days)
            return []

        cutoff = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(days=safe_days)
        cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        for row in await self._tickets.list_archived_before(cutoff_iso):
            guild = self._bot.get_guild(row["guild_id"]) if self._bot is not None else None
            await self._delete_leftover_channel(guild, row.get("channel_id"), "Archived ticket purged")
        return await self._tickets.purge_archived_before(cutoff_iso)

    # ------------------------------------------------------------------
    # Chat-log capture (opt-in, plain text only — see config.py's
    # TICKET_CHAT_LOGGING_ENABLED)
    # ------------------------------------------------------------------

    async def record_ticket_message(self, ticket_id: int, author_id: int, content: str) -> None:
        """Appends a plain-text chat-log row for an open ticket. Callers
        (cogs/tickets.py's on_message listener) are responsible for checking
        config.TICKET_CHAT_LOGGING_ENABLED and for stripping everything but
        plain text content before calling this — attachments and voice
        messages must never reach this method."""
        await self._tickets.add_ticket_message(ticket_id, author_id, content)

    async def get_ticket_messages(self, ticket_id: int, guild: discord.Guild) -> tuple[list[dict] | None, list[str]]:
        ticket = await self._tickets.get_ticket(ticket_id)
        if not ticket or ticket["guild_id"] != guild.id:
            return (None, ["Ticket not found."])

        messages = await self._tickets.list_ticket_messages(ticket_id)
        return (messages, [])
