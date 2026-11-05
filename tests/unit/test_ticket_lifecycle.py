"""Ticket open race / close + delayed delete / stale sweep / purge guard (services/ticket_service.py,
cogs/tickets.py). Pure: mocked repos and Discord objects, no network."""
import asyncio
import datetime as _dt
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.ticket_service import TicketService, parse_purge_days

# async tests run via asyncio_mode=auto (see CLAUDE.md test command)

GUILD_ID = 100000000000000001
PANEL_ID = 7
OPENER_ID = 111111111111111111
STAFF_ID = 222222222222222222


class FakeTicketRepo:
    """Just enough of TicketRepository for open/archive/delete/purge."""

    def __init__(self, panel=None, fail_insert=False):
        self.panel = panel or {
            "id": PANEL_ID, "guild_id": GUILD_ID, "max_open_per_user": 1,
            "naming_pattern": "ticket-{username}", "open_category_id": None,
            "closed_category_id": None, "purpose": "support",
        }
        self.tickets: dict[int, dict] = {}
        self.fail_insert = fail_insert
        self.status_calls = []
        self.deleted = []
        self.archived_before = []
        self.purged = []

    async def get_panel(self, panel_id):
        return dict(self.panel) if panel_id == PANEL_ID else None

    async def count_open_by_user(self, guild_id, opened_by, panel_id):
        await asyncio.sleep(0)
        return sum(1 for t in self.tickets.values() if t["opened_by"] == opened_by and t["status"] == "open")

    async def next_ticket_number(self, guild_id, panel_id):
        await asyncio.sleep(0)
        return max((t["ticket_number"] for t in self.tickets.values()), default=0) + 1

    async def count_all_by_user(self, guild_id, opened_by, panel_id):
        return sum(1 for t in self.tickets.values() if t["opened_by"] == opened_by)

    async def get_panel_roles(self, panel_id):
        return []

    async def create_ticket(self, guild_id, panel_id, channel_id, opened_by, ticket_number):
        await asyncio.sleep(0)
        if self.fail_insert:
            raise RuntimeError("UNIQUE constraint failed")
        tid = len(self.tickets) + 1
        self.tickets[tid] = {
            "id": tid, "guild_id": guild_id, "panel_id": panel_id, "channel_id": channel_id,
            "opened_by": opened_by, "ticket_number": ticket_number, "status": "open",
        }
        return tid

    async def set_control_message_id(self, ticket_id, message_id):
        pass

    async def get_ticket(self, ticket_id):
        t = self.tickets.get(ticket_id)
        return dict(t) if t else None

    async def set_status(self, ticket_id, status, claimed_by=None):
        self.status_calls.append((ticket_id, status))
        self.tickets[ticket_id]["status"] = status

    async def transition_status(self, ticket_id, from_statuses, to_status):
        await asyncio.sleep(0)
        t = self.tickets.get(ticket_id)
        if t is None or t["status"] not in from_statuses:
            return False
        self.status_calls.append((ticket_id, to_status))
        t["status"] = to_status
        return True

    async def list_by_status(self, guild_id, status):
        return [dict(t) for t in self.tickets.values() if t["status"] == status]

    async def list_open_tickets(self, guild_id):
        return await self.list_by_status(guild_id, "open")

    async def delete_ticket(self, ticket_id):
        self.deleted.append(ticket_id)
        self.tickets.pop(ticket_id, None)
        return True

    async def list_archived_before(self, cutoff_iso):
        self.archived_before.append(cutoff_iso)
        return []

    async def purge_archived_before(self, cutoff_iso):
        self.purged.append(cutoff_iso)
        return []


def make_templates():
    t = MagicMock()
    t.get_by_name = AsyncMock(return_value=None)
    return t


def make_guild(channels=None):
    guild = MagicMock()
    guild.id = GUILD_ID
    created = []
    counter = iter(range(900000000000000001, 900000000000000999))

    async def create_text_channel(name, category=None, overwrites=None, reason=None):
        await asyncio.sleep(0)
        ch = MagicMock()
        ch.id = next(counter)
        ch.name = name
        ch.send = AsyncMock(return_value=MagicMock(id=1))
        ch.delete = AsyncMock()
        created.append(ch)
        return ch

    guild.create_text_channel = AsyncMock(side_effect=create_text_channel)
    guild.created = created
    channels = channels or {}
    guild.get_channel = MagicMock(side_effect=lambda cid: channels.get(cid))
    guild.get_role = MagicMock(return_value=None)
    return guild


def make_member(uid, name="opener"):
    m = MagicMock()
    m.id = uid
    m.name = name
    m.mention = f"<@{uid}>"
    return m


def make_service(repo, audit=None):
    return TicketService(repo, make_templates(), MagicMock(), MagicMock(), audit=audit)


# ---------------------------------------------------------------------------
# Open race
# ---------------------------------------------------------------------------
async def test_two_quick_opens_create_one_channel():
    repo = FakeTicketRepo()
    svc = make_service(repo)
    guild = make_guild()
    member = make_member(OPENER_ID)

    results = await asyncio.gather(
        svc.open_ticket(PANEL_ID, guild, member),
        svc.open_ticket(PANEL_ID, guild, member),
    )
    successes = [r for r in results if r[0] is not None]
    failures = [r for r in results if r[0] is None]
    assert len(successes) == 1 and len(failures) == 1
    assert "already have" in failures[0][1][0]
    assert guild.create_text_channel.await_count == 1
    assert len(repo.tickets) == 1


async def test_two_members_get_distinct_ticket_numbers():
    repo = FakeTicketRepo()
    svc = make_service(repo)
    guild = make_guild()
    await asyncio.gather(
        svc.open_ticket(PANEL_ID, guild, make_member(OPENER_ID, "a")),
        svc.open_ticket(PANEL_ID, guild, make_member(OPENER_ID + 1, "b")),
    )
    numbers = sorted(t["ticket_number"] for t in repo.tickets.values())
    assert numbers == [1, 2]


async def test_failed_insert_deletes_the_new_channel():
    repo = FakeTicketRepo(fail_insert=True)
    svc = make_service(repo)
    guild = make_guild()
    channel, errors = await svc.open_ticket(PANEL_ID, guild, make_member(OPENER_ID))
    assert channel is None and errors
    assert len(guild.created) == 1
    guild.created[0].delete.assert_awaited_once()


# ---------------------------------------------------------------------------
# Close -> delayed delete (archive), restart recovery, weekly stale sweep
# ---------------------------------------------------------------------------
def _ticket(repo, channel_id, status="closed", tid=1, **extra):
    repo.tickets[tid] = {
        "id": tid, "guild_id": GUILD_ID, "panel_id": PANEL_ID, "channel_id": channel_id,
        "opened_by": OPENER_ID, "ticket_number": tid, "status": status,
        "opened_at": "2026-10-01 12:00:00", "closed_at": None, **extra,
    }


def make_channel(cid, last_message_id=None):
    ch = MagicMock(id=cid)
    ch.delete = AsyncMock()
    ch.set_permissions = AsyncMock()
    ch.send = AsyncMock()
    ch.last_message_id = last_message_id
    return ch


def make_audit(entries=None):
    audit = MagicMock()
    audit.log = AsyncMock()
    audit.query = AsyncMock(return_value={"entries": entries or [], "next_before_id": None})
    return audit


async def test_archive_deletes_channel_and_audits():
    repo = FakeTicketRepo()
    _ticket(repo, 555)
    channel = make_channel(555)
    guild = make_guild({555: channel})
    audit = make_audit()
    svc = make_service(repo, audit=audit)

    assert await svc.archive_ticket(1, guild, make_member(STAFF_ID, "staff")) == []
    assert repo.tickets[1]["status"] == "archived"
    channel.delete.assert_awaited_once()
    a_args, a_kwargs = audit.log.await_args
    assert a_args[1] == STAFF_ID and a_args[2] == "ticket_archived"
    assert a_kwargs["detail"]["channel"] == "deleted"


async def test_archive_runs_once_when_two_callers_race():
    repo = FakeTicketRepo()
    _ticket(repo, 556)
    channel = make_channel(556)
    guild = make_guild({556: channel})
    svc = make_service(repo)
    staff = make_member(STAFF_ID, "staff")
    results = await asyncio.gather(svc.archive_ticket(1, guild, staff), svc.archive_ticket(1, guild, staff))
    assert sorted(len(r) for r in results) == [0, 1]
    channel.delete.assert_awaited_once()


async def test_close_locks_opener_posts_notice_and_deletes_after_delay():
    repo = FakeTicketRepo()
    _ticket(repo, 557, status="open")
    channel = make_channel(557)
    guild = make_guild({557: channel})
    opener = make_member(OPENER_ID)
    guild.get_member = MagicMock(return_value=opener)
    guild.me = make_member(999, "bot")
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    svc = TicketService(repo, make_templates(), MagicMock(), bot)
    svc.close_delete_delay = 0

    assert await svc.close_ticket(1, guild, make_member(STAFF_ID, "staff")) == []
    assert repo.tickets[1]["status"] == "closed"
    args, kwargs = channel.set_permissions.await_args
    assert args[0] is opener and kwargs["send_messages"] is False and kwargs["view_channel"] is True
    sent = channel.send.await_args.kwargs
    assert "view" not in sent  # no Archive button any more
    assert "will be deleted in 0 seconds" in sent["embeds"][-1].description
    channel.delete.assert_not_awaited()

    await asyncio.gather(*svc._delete_tasks)
    assert repo.tickets[1]["status"] == "archived"
    channel.delete.assert_awaited_once()


async def test_close_refuses_a_second_close():
    repo = FakeTicketRepo()
    _ticket(repo, 558, status="open")
    guild = make_guild({})
    svc = make_service(repo)
    svc.close_delete_delay = 3600
    staff = make_member(STAFF_ID, "staff")
    assert await svc.close_ticket(1, guild, staff) == []
    assert await svc.close_ticket(1, guild, staff) == ["This ticket is already closed."]
    for task in list(svc._delete_tasks):
        task.cancel()


async def test_finish_overdue_closes_after_restart():
    repo = FakeTicketRepo()
    _ticket(repo, 601, tid=1, closed_at="2026-10-17 20:00:00")  # overdue
    _ticket(repo, 602, tid=2, closed_at="2026-10-17 20:59:50")  # still inside its 30 s window
    _ticket(repo, 603, tid=3, status="open")
    channels = {601: make_channel(601), 602: make_channel(602), 603: make_channel(603)}
    guild = make_guild(channels)
    guild.me = make_member(999, "bot")
    svc = make_service(repo)
    now = _dt.datetime(2026, 10, 17, 21, 0, 0, tzinfo=_dt.timezone.utc)

    assert await svc.finish_overdue_closes(guild, now=now) == [1]
    channels[601].delete.assert_awaited_once()
    channels[602].delete.assert_not_awaited()
    assert repo.tickets[2]["status"] == "closed" and repo.tickets[3]["status"] == "open"


async def test_stale_sweep_closes_only_idle_tickets():
    import discord

    now = _dt.datetime(2026, 10, 17, 21, 0, 0, tzinfo=_dt.timezone.utc)
    repo = FakeTicketRepo()
    # idle: opened long ago, last message 10 days ago
    _ticket(repo, 701, tid=1, status="open")
    # active: last message yesterday
    _ticket(repo, 702, tid=2, status="open")
    # channel deleted by hand
    _ticket(repo, 703, tid=3, status="open")
    channels = {
        701: make_channel(701, discord.utils.time_snowflake(now - _dt.timedelta(days=10))),
        702: make_channel(702, discord.utils.time_snowflake(now - _dt.timedelta(days=1))),
    }
    guild = make_guild(channels)
    guild.me = make_member(999, "bot")
    guild.get_member = MagicMock(return_value=None)
    svc = make_service(repo)
    svc.close_delete_delay = 3600

    assert sorted(await svc.close_stale_open_tickets(guild, now=now)) == [1, 3]
    assert repo.tickets[1]["status"] == "closed"
    assert "no activity" in channels[701].send.await_args.kwargs["embeds"][-1].description
    assert repo.tickets[2]["status"] == "open"
    assert repo.tickets[3]["status"] == "archived"
    for task in list(svc._delete_tasks):
        task.cancel()


async def test_weekly_sweep_waits_a_week_between_runs():
    now = _dt.datetime(2026, 10, 17, 21, 0, 0, tzinfo=_dt.timezone.utc)
    repo = FakeTicketRepo()
    guild = make_guild({})
    guild.me = make_member(999, "bot")

    recent = make_audit([{"created_at": "2026-10-14T09:00:00Z"}])
    svc = make_service(repo, audit=recent)
    assert await svc.run_weekly_stale_sweep(guild, now=now) is None
    recent.log.assert_not_awaited()

    old = make_audit([{"created_at": "2026-10-09T09:00:00Z"}])
    svc = make_service(repo, audit=old)
    assert await svc.run_weekly_stale_sweep(guild, now=now) == []
    assert old.log.await_args.args[2] == "ticket_stale_sweep"

    never = make_audit([])
    svc = make_service(repo, audit=never)
    assert await svc.run_weekly_stale_sweep(guild, now=now) == []
    never.log.assert_awaited_once()


async def test_delete_archived_ticket_removes_leftover_channel_and_audits():
    repo = FakeTicketRepo()
    _ticket(repo, 557, status="archived")
    channel = make_channel(557)
    guild = make_guild({557: channel})
    audit = make_audit()
    svc = make_service(repo, audit=audit)
    assert await svc.delete_ticket(1, guild, actor_id=STAFF_ID) == []
    channel.delete.assert_awaited_once()
    assert repo.deleted == [1]
    assert audit.log.await_args[0][2] == "ticket_deleted"


async def test_close_button_is_staff_only():
    from cogs.tickets import TicketControlView

    view = TicketControlView(1)
    cog = MagicMock()
    cog.permission_service.has_permission = AsyncMock(return_value=False)
    cog.ticket_service.close_ticket = AsyncMock(return_value=[])
    interaction = MagicMock()
    interaction.client.get_cog = MagicMock(return_value=cog)
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()

    await view._close_callback(interaction)
    cog.ticket_service.close_ticket.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()

    cog.permission_service.has_permission = AsyncMock(return_value=True)
    interaction.followup.send = AsyncMock()
    await view._close_callback(interaction)
    cog.ticket_service.close_ticket.assert_awaited_once()


# ---------------------------------------------------------------------------
# Purge guard
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (30, 30), ("30", 30), (1, 1), (0, None), (-5, None), ("0", None), ("abc", None),
    (None, None), (True, None), ("", None), (2.0, None),
])
def test_parse_purge_days(value, expected):
    assert parse_purge_days(value) == expected


async def test_service_refuses_purge_with_bad_days():
    repo = FakeTicketRepo()
    svc = make_service(repo)
    assert await svc.purge_old_archived_tickets(0) == []
    assert repo.purged == [] and repo.archived_before == []
    await svc.purge_old_archived_tickets(30)
    assert len(repo.purged) == 1


async def test_purge_loop_skips_when_setting_is_invalid():
    from cogs.tickets import TicketCog

    cog = object.__new__(TicketCog)
    cog.bot = MagicMock()
    cog.bot.guilds = [MagicMock(id=GUILD_ID)]
    cog._ticket_archive_purge_days = AsyncMock(return_value=0)
    cog.ticket_service = MagicMock()
    cog.ticket_service.purge_old_archived_tickets = AsyncMock(return_value=[])

    await TicketCog.purge_archived_tickets_loop.coro(cog)
    cog.ticket_service.purge_old_archived_tickets.assert_not_awaited()

    cog._ticket_archive_purge_days = AsyncMock(return_value=30)
    await TicketCog.purge_archived_tickets_loop.coro(cog)
    cog.ticket_service.purge_old_archived_tickets.assert_awaited_once_with(30)


# ---------------------------------------------------------------------------
# Verify memo on open
# ---------------------------------------------------------------------------

def _verify_repo():
    repo = FakeTicketRepo()
    repo.panel["purpose"] = "verify"
    return repo


async def test_verify_ticket_posts_the_memo_after_the_welcome():
    import discord
    templates = make_templates()
    rows = {"welcome-verify": {"id": 15}}
    templates.get_by_name = AsyncMock(side_effect=lambda gid, name: rows.get(name))
    templates.get_full = AsyncMock(return_value={"name": "welcome-verify"})
    renderer = MagicMock()
    memo = discord.Embed(title="How to get verified")
    renderer.render_message = MagicMock(return_value={"content": None, "embeds": [memo]})
    svc = TicketService(_verify_repo(), templates, renderer, MagicMock())
    guild = make_guild()
    member = make_member(OPENER_ID)

    channel, errors = await svc.open_ticket(PANEL_ID, guild, member)
    assert errors == []
    first, second = channel.send.await_args_list
    assert "view" in first.kwargs  # welcome + Verify & Close buttons first
    assert second.kwargs["embeds"] == [memo]
    am = second.kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is False and am.users == [member]
    ctx = renderer.render_message.call_args.args[1]
    assert ctx.member is member and ctx.ticket_number == 1


async def test_verify_memo_falls_back_and_never_blocks_the_ticket():
    import discord
    svc = TicketService(_verify_repo(), make_templates(), MagicMock(), MagicMock())
    guild = make_guild()
    channel, errors = await svc.open_ticket(PANEL_ID, guild, make_member(OPENER_ID))
    memo = channel.send.await_args_list[1]
    assert memo.kwargs["content"].startswith(f"Hi <@{OPENER_ID}>!") and memo.kwargs["embeds"] == []

    repo = _verify_repo()
    svc = TicketService(repo, make_templates(), MagicMock(), MagicMock())
    guild = make_guild()
    orig = guild.create_text_channel.side_effect

    async def failing_memo_channel(*a, **kw):
        ch = await orig(*a, **kw)
        ch.send = AsyncMock(side_effect=[MagicMock(id=1), discord.HTTPException(MagicMock(status=500), "boom")])
        return ch

    guild.create_text_channel.side_effect = failing_memo_channel
    channel, errors = await svc.open_ticket(PANEL_ID, guild, make_member(OPENER_ID))
    assert channel is not None and errors == [] and len(repo.tickets) == 1


async def test_support_ticket_gets_no_verify_memo():
    svc = make_service(FakeTicketRepo())
    channel, _ = await svc.open_ticket(PANEL_ID, make_guild(), make_member(OPENER_ID))
    assert channel.send.await_count == 1
