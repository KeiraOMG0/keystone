"""Live `roles` topic: coalescing publisher (services/role_live_service.py) and hub wiring."""

from __future__ import annotations

import asyncio

import pytest

from services import live_events
from services.role_live_service import RoleEventCoalescer, build_messages, merge_action

BIG = 987654321098765432
BIG2 = 987654321098765999


@pytest.mark.parametrize(
    "prev,new,expected",
    [
        (None, "updated", "updated"),
        (None, "created", "created"),
        ("created", "updated", "created"),
        ("updated", "updated", "updated"),
        ("updated", "deleted", "deleted"),
        ("created", "deleted", "deleted"),
        ("deleted", "updated", "deleted"),
    ],
)
def test_merge_action(prev, new, expected):
    assert merge_action(prev, new) == expected


def test_build_messages_groups_by_action_in_fixed_order():
    msgs = build_messages({3: "updated", BIG: "deleted", 1: "updated", 2: "created"})
    assert [m["action"] for m in msgs] == ["created", "updated", "deleted"]
    upd = msgs[1]
    assert upd["role_id"] == 3 and upd["role_ids"] == ["3", "1"] and upd["count"] == 2
    assert msgs[2]["role_ids"] == [str(BIG)]
    assert build_messages({}) == []


class Recorder:
    def __init__(self, subscribed=True):
        self.sent: list[tuple[str, dict]] = []
        self.subscribed = subscribed

    def publish(self, topic, data):
        self.sent.append((topic, data))

    def has_subscribers(self, topic):
        return self.subscribed


@pytest.mark.asyncio
async def test_burst_of_position_updates_coalesces_into_one_message():
    rec = Recorder()
    c = RoleEventCoalescer(rec.publish, has_subscribers=rec.has_subscribers, window=0.05, max_delay=1.0)
    for rid in range(30):
        c.note("updated", 1000 + rid)
    assert rec.sent == []
    await asyncio.sleep(0.12)
    assert len(rec.sent) == 1
    topic, data = rec.sent[0]
    assert topic == "roles" and data["action"] == "updated" and data["count"] == 30


@pytest.mark.asyncio
async def test_max_delay_caps_a_never_ending_trickle():
    rec = Recorder()
    c = RoleEventCoalescer(rec.publish, has_subscribers=rec.has_subscribers, window=0.05, max_delay=0.12)
    for i in range(8):  # an event every 30ms keeps resetting the 50ms debounce
        c.note("updated", i)
        await asyncio.sleep(0.03)
    assert len(rec.sent) >= 1  # flushed by the cap, not starved
    await asyncio.sleep(0.15)
    assert sum(d["count"] for _, d in rec.sent) == 8


@pytest.mark.asyncio
async def test_no_subscribers_means_no_buffering_and_no_publish():
    rec = Recorder(subscribed=False)
    c = RoleEventCoalescer(rec.publish, has_subscribers=rec.has_subscribers, window=0.01)
    c.note("created", 1)
    assert c.pending == {}
    await asyncio.sleep(0.03)
    assert rec.sent == []


@pytest.mark.asyncio
async def test_create_then_delete_publishes_only_deleted_and_cancel_drops():
    rec = Recorder()
    c = RoleEventCoalescer(rec.publish, has_subscribers=rec.has_subscribers, window=0.02)
    c.note("created", 5)
    c.note("updated", 5)
    c.note("deleted", 5)
    c.note("bogus", 6)  # unknown actions ignored
    await asyncio.sleep(0.06)
    assert [d["action"] for _, d in rec.sent] == ["deleted"]
    c.note("updated", 7)
    c.cancel()
    await asyncio.sleep(0.04)
    assert len(rec.sent) == 1


@pytest.mark.asyncio
async def test_publisher_failure_never_raises():
    def boom(topic, data):
        raise RuntimeError("socket exploded")

    c = RoleEventCoalescer(boom, window=0.01)
    c.note("updated", 1)
    await asyncio.sleep(0.03)  # flush raised internally and was swallowed
    c.flush()


@pytest.mark.asyncio
async def test_roles_topic_reaches_a_subscribed_client_with_string_ids():
    assert "roles" in live_events.EVENT_TOPICS
    client = live_events.hub.add_client()
    try:
        accepted, rejected = live_events.hub.subscribe(client, ["roles"])
        assert accepted == ["roles"] and rejected == []
        c = RoleEventCoalescer(live_events.publish, has_subscribers=live_events.hub.has_subscribers, window=0.01)
        c.note("updated", BIG)
        c.note("deleted", BIG2)
        await asyncio.sleep(0.04)
        msgs = []
        while not client.queue.empty():
            msgs.append(client.queue.get_nowait())
        assert [m["topic"] for m in msgs] == ["roles", "roles"]
        upd, dele = msgs[0]["data"], msgs[1]["data"]
        assert upd == {"action": "updated", "role_id": str(BIG), "role_ids": [str(BIG)], "count": 1}
        assert dele["action"] == "deleted" and dele["role_id"] == str(BIG2)
    finally:
        live_events.hub.remove_client(client)


def test_dashboard_hub_authorizes_roles_on_manage_reaction_roles():
    from server.live.hub import TOPIC_PERMISSIONS

    assert TOPIC_PERMISSIONS["roles"] == "manage_reaction_roles"
