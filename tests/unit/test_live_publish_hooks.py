"""Publish hooks at the real mutation points, and their failure-proofing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from database.repositories.mod_action_repo import ModActionRepository
from services import live_events
from services.audit_service import AuditService
from services.honeypot_service import HoneypotService
from services.moderation_service import ModerationService
from services.ticket_service import TicketService
from services.warning_service import WarningService
from tests.unit.test_honeypot_service import build
from tests.unit.test_honeypot_support import (
    GUILD_ID, configured, db, make_guild, make_member, make_message, make_service, repo,
)

pytestmark = pytest.mark.asyncio

BIG = 987654321098765432


@pytest.fixture
def live():
    """A client subscribed to every event topic on the process-wide hub."""
    client = live_events.hub.add_client()
    live_events.hub.subscribe(client, list(live_events.EVENT_TOPICS))

    def drain(topic=None):
        out = []
        while not client.queue.empty():
            m = client.queue.get_nowait()
            if topic is None or m["topic"] == topic:
                out.append(m)
        return out

    yield SimpleNamespace(drain=drain, client=client)
    live_events.hub.remove_client(client)


async def test_honeypot_catch_publishes_catch_and_stats(repo, db, live):
    env = await build(repo, db)
    await env.svc.handle_message(make_message(make_member(BIG), env.trap, "free nitro http://x.y", guild=env.guild))
    msgs = live.drain("honeypot")
    kinds = [m["data"]["kind"] for m in msgs]
    assert "catch_created" in kinds and "stats" in kinds
    catch = next(m for m in msgs if m["data"]["kind"] == "catch_created")["data"]["catch"]
    assert catch["user_id"] == str(BIG) and catch["action"] == "ban" and catch["id"] >= 1
    assert catch["flags"]["link"] is True and catch["appeal_status"] == "invited"
    stats = next(m for m in msgs if m["data"]["kind"] == "stats")["data"]["stats"]
    assert isinstance(stats, dict) and stats


async def test_honeypot_catch_survives_a_broken_hub(repo, db, live, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("websocket exploded")

    monkeypatch.setattr(live_events.hub, "publish", boom)
    env = await build(repo, db)
    member = make_member(42)
    await env.svc.handle_message(make_message(member, env.trap, "free nitro http://x.y", guild=env.guild))
    env.guild.ban.assert_awaited_once()  # the punishment still happened
    (event,) = await repo.list_events(GUILD_ID)
    assert event["action_result"] == "ok"


async def test_honeypot_appeal_lifecycle_events(repo, db, live):
    guild = make_guild()
    svc, bot = make_service(guild, repo, db)
    bot.get_user = MagicMock(return_value=None)
    bot.fetch_user = AsyncMock(return_value=MagicMock(send=AsyncMock()))
    await repo.create_appeal(GUILD_ID, 5, "tok", username="bob")
    await svc.submit_appeal(GUILD_ID, "Bob", "I was hacked", token="tok")
    (sub,) = [m for m in live.drain("honeypot") if m["data"]["kind"] == "appeal_submitted"]
    appeal = sub["data"]["appeal"]
    assert appeal["status"] == "pending" and appeal["user_id"] == "5" and "token" not in appeal

    await svc.approve_appeal(guild, appeal["id"], 9)
    kinds = [m["data"]["kind"] for m in live.drain("honeypot")]
    assert "appeal_approved" in kinds and "stats" in kinds

    await repo.create_appeal(GUILD_ID, 6, "tok2")
    await svc.submit_appeal(GUILD_ID, "Al", "x", token="tok2")
    live.drain()
    a2 = await repo.get_appeal_by_token(GUILD_ID, "tok2")
    await svc.deny_appeal(GUILD_ID, a2["id"], 9, "no")
    denied = [m for m in live.drain("honeypot") if m["data"]["kind"] == "appeal_denied"]
    assert denied and denied[0]["data"]["appeal_id"] == a2["id"]


async def test_honeypot_config_changed_event(repo, db, live):
    env = await build(repo, db)
    await env.svc.update_config(env.guild, {"action": "kick"})
    (m,) = [m for m in live.drain("honeypot") if m["data"]["kind"] == "config_changed"]
    assert m["data"]["config"]["action"] == "kick" and m["data"]["config"]["channel_id"] == "1000000000000000001"


async def test_moderation_actions_publish_and_audit_rows(db, live):
    audit_repo = SimpleNamespace(log=AsyncMock())
    svc = ModerationService(ModActionRepository(db), AuditService(audit_repo))
    guild = SimpleNamespace(id=GUILD_ID, ban=AsyncMock(), unban=AsyncMock())
    mod = SimpleNamespace(id=BIG)
    member = make_member(77)
    await svc.kick(member, guild, mod, "spam")
    await svc.ban(guild, 78, mod, "raid")
    await svc.unban(guild, 78, mod, "oops")
    member.timeout = AsyncMock()
    await svc.timeout(member, guild, mod, 10, "loud")
    await svc.untimeout(member, guild, mod, None)
    everything = live.drain()
    mods = [m for m in everything if m["topic"] == "moderation"]
    assert [m["data"]["kind"] for m in mods] == ["kick", "ban", "unban", "timeout", "untimeout"]
    assert mods[0]["data"]["moderator_id"] == str(BIG) and mods[0]["data"]["user_id"] == "77"
    assert mods[3]["data"]["duration_minutes"] == 10
    audits = [m["data"]["row"]["action"] for m in everything if m["topic"] == "audit"]
    assert audits == ["member_kicked", "member_banned", "member_unbanned", "member_timeout", "member_untimeout"]


async def test_audit_topic_carries_rows(live):
    svc = AuditService(SimpleNamespace(log=AsyncMock()))
    await svc.log(GUILD_ID, BIG, "member_kicked", "member", 5, {"reason": "x"})
    (m,) = live.drain("audit")
    row = m["data"]["row"]
    assert row["action"] == "member_kicked" and row["actor_id"] == str(BIG) and row["target_id"] == "5"
    assert row["detail"] == {"reason": "x"} and row["created_at"]


async def test_moderation_action_never_broken_by_hub(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("hub down")

    monkeypatch.setattr(live_events.hub, "publish", boom)
    svc = ModerationService(ModActionRepository(db), AuditService(SimpleNamespace(log=AsyncMock())))
    member = make_member(77)
    assert await svc.kick(member, SimpleNamespace(id=GUILD_ID), SimpleNamespace(id=1), "r") >= 1


async def test_warning_events(live):
    repo = SimpleNamespace(add_warning=AsyncMock(return_value=12), remove_warning=AsyncMock(return_value=True))
    svc = WarningService(repo)
    assert await svc.add_warning(GUILD_ID, BIG, 2, "rude") == 12
    await svc.remove_warning(GUILD_ID, 12)
    a, b = live.drain("moderation")
    assert a["data"]["kind"] == "warn" and a["data"]["user_id"] == str(BIG) and a["data"]["warning_id"] == 12
    assert b["data"]["kind"] == "warning_removed" and b["data"]["warning_id"] == 12


async def test_ticket_lifecycle_events(live):
    ticket = {"id": 3, "guild_id": GUILD_ID, "panel_id": 1, "channel_id": 9, "status": "open", "claimed_by": None,
              "opened_by": BIG, "ticket_number": 4}
    repo = MagicMock()
    repo.get_ticket = AsyncMock(side_effect=lambda tid: dict(ticket))
    repo.claim_ticket = AsyncMock(return_value=True)
    repo.set_status = AsyncMock()
    repo.transition_status = AsyncMock(return_value=True)
    repo.get_panel = AsyncMock(return_value=None)
    svc = TicketService(repo, MagicMock(), MagicMock(), MagicMock())
    guild = SimpleNamespace(id=GUILD_ID, get_channel=lambda cid: None, default_role=None, me=None)
    actor = SimpleNamespace(id=BIG, mention="<@x>")
    assert await svc.claim_ticket(3, guild, actor) == []
    assert await svc.close_ticket(3, guild, actor) == []
    for task in list(svc._delete_tasks):  # the 30 s delayed delete; archived by hand below
        task.cancel()
    ticket["status"] = "closed"
    assert await svc.archive_ticket(3, guild, actor) == []
    ticket["status"] = "archived"
    repo.delete_ticket = AsyncMock()
    assert await svc.delete_ticket(3, guild) == []
    kinds = [(m["data"]["kind"], m["data"]["ticket_id"]) for m in live.drain("tickets")]
    assert kinds == [("claimed", 3), ("closed", 3), ("archived", 3), ("deleted", 3)]
