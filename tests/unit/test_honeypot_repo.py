import pytest

from tests.unit.test_honeypot_support import GUILD_ID, db, repo  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio


async def test_config_missing_then_upsert_defaults(repo):
    assert await repo.get_config(GUILD_ID) is None
    cfg = await repo.upsert_config(GUILD_ID, channel_id=123)
    assert cfg["channel_id"] == 123
    assert cfg["enabled"] is True and cfg["dm_before"] is True and cfg["appeal_enabled"] is True
    assert cfg["action"] == "ban" and cfg["delete_message_days"] == 1


async def test_config_update_and_bool_roundtrip(repo):
    await repo.upsert_config(GUILD_ID, channel_id=1)
    cfg = await repo.upsert_config(GUILD_ID, enabled=False, action="kick", delete_message_days=0, log_channel_id=77)
    assert cfg["enabled"] is False and cfg["action"] == "kick"
    assert cfg["delete_message_days"] == 0 and cfg["log_channel_id"] == 77
    cfg = await repo.upsert_config(GUILD_ID, log_channel_id=None)
    assert cfg["log_channel_id"] is None


async def test_config_rejects_unknown_field(repo):
    with pytest.raises(ValueError):
        await repo.upsert_config(GUILD_ID, bogus=1)


async def test_event_crud_and_pagination(repo):
    ids = [await repo.add_event(GUILD_ID, 10 + i, "ban", username=f"u{i}", errors=["x"] if i == 0 else [],
                                dm_sent=bool(i % 2), flag_link=True) for i in range(5)]
    got = await repo.list_events(GUILD_ID, limit=3)
    assert [e["id"] for e in got] == ids[::-1][:3]
    assert got[0]["flag_link"] is True and got[0]["errors"] == []
    older = await repo.list_events(GUILD_ID, limit=10, before_id=ids[2])
    assert [e["id"] for e in older] == [ids[1], ids[0]]
    assert older[-1]["errors"] == ["x"]
    assert await repo.count_events(GUILD_ID) == 5
    assert (await repo.get_last_event(GUILD_ID))["id"] == ids[-1]
    assert await repo.list_events(GUILD_ID + 1) == []


async def test_append_event_error(repo):
    eid = await repo.add_event(GUILD_ID, 1, "ban")
    await repo.append_event_error(eid, "boom")
    await repo.append_event_error(eid, "again")
    assert (await repo.get_event(eid))["errors"] == ["boom", "again"]


async def test_list_events_reports_latest_appeal_status(repo):
    eid = await repo.add_event(GUILD_ID, 5, "ban")
    assert (await repo.list_events(GUILD_ID))[0]["appeal_status"] is None
    await repo.create_appeal(GUILD_ID, 5, "tok", catch_id=eid)
    assert (await repo.list_events(GUILD_ID))[0]["appeal_status"] == "invited"


async def test_appeal_lifecycle(repo):
    aid = await repo.create_appeal(GUILD_ID, 5, "tok1", username="bob")
    assert (await repo.get_appeal_by_token(GUILD_ID, "tok1"))["id"] == aid
    assert await repo.get_appeal_by_token(GUILD_ID, "nope") is None
    assert await repo.review_appeal(aid, "approved", 1, None) is False  # not pending yet
    assert await repo.submit_appeal(aid, "Bob", "hi") is True
    assert await repo.submit_appeal(aid, "Bob", "again") is False       # no longer invited
    assert await repo.review_appeal(aid, "denied", 9, "no") is True
    assert await repo.review_appeal(aid, "approved", 9, None) is False  # already reviewed
    a = await repo.get_appeal(aid)
    assert a["status"] == "denied" and a["reviewed_by"] == 9 and a["review_note"] == "no"
    counts = await repo.count_appeals_by_status(GUILD_ID)
    assert counts == {"invited": 0, "pending": 0, "approved": 0, "denied": 1}


async def test_appeal_token_unique(repo):
    await repo.create_appeal(GUILD_ID, 1, "same")
    with pytest.raises(Exception):
        await repo.create_appeal(GUILD_ID, 2, "same")


async def test_list_appeals_filter(repo):
    a = await repo.create_appeal(GUILD_ID, 1, "a")
    b = await repo.create_appeal(GUILD_ID, 2, "b", status="pending")
    assert [x["id"] for x in await repo.list_appeals(GUILD_ID)] == [b, a]
    assert [x["id"] for x in await repo.list_appeals(GUILD_ID, "pending")] == [b]
    assert await repo.list_appeals(GUILD_ID, "approved") == []
