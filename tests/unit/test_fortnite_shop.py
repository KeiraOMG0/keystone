"""Fortnite shop: parser, grouping, embed splitting, wishlist, timing, repo and
the daily-post dedupe (services/fortnite_shop_service.py,
database/repositories/fortnite_repo.py). No network: the fixture is a trimmed
real fortnite-api.com /v2/shop response."""
from __future__ import annotations

import copy
import json
import pathlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
import pytest_asyncio

from database.connection import Database
from database.repositories.fortnite_repo import FortniteRepository, MAX_WISHLIST_PER_USER, normalise_query
from services import fortnite_shop_service as fs

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "fortnite_shop_sample.json"
SQL = pathlib.Path(__file__).resolve().parents[2] / "database" / "migrations" / "sql" / "027_fortnite_shop.sql"
GUILD_ID = 100000000000000001
CHANNEL_ID = 1000000000000000001


def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def shop() -> fs.Shop:
    return fs.parse_shop(payload())


# ---------------------------------------------------------------- parsing


def test_parse_basic_fields(shop):
    assert shop.shop_date == "2026-10-13"
    assert shop.hash
    assert shop.vbuck_icon.startswith("https://")
    assert len(shop.entries) == len(payload()["data"]["entries"])
    for e in shop.entries:
        assert e.name and e.final_price > 0 and e.image_url


def test_parse_bundle_with_banner_and_discount(shop):
    bundle = next(e for e in shop.entries if e.kind == "bundle" and e.banner)
    assert bundle.discounted and bundle.final_price < bundle.regular_price
    assert "~~" in fs.format_price(bundle)
    assert len(bundle.item_names) > 1  # bundle name + contained items


def test_parse_kinds_and_sections(shop):
    kinds = {e.kind for e in shop.entries}
    assert {"bundle", "br", "track", "car"} <= kinds
    tracks = [e for e in shop.entries if e.kind == "track"]
    assert all(e.type_label == "Jam Track" and " - " in e.name for e in tracks)
    # entries without a layout fall into "Other"
    assert any(e.section == fs.OTHER_SECTION for e in shop.entries)
    assert all(e.section == e.section.strip() for e in shop.entries)


def test_series_colour_overrides_rarity(shop):
    raw = payload()["data"]["entries"]
    series_raw = next(r for r in raw if any("series" in i for i in r.get("brItems", [])) and "bundle" not in r)
    entry = fs.parse_entry(series_raw)
    item = next(i for i in series_raw["brItems"] if "series" in i)
    assert entry.rarity_label == item["series"]["value"]
    assert entry.colour == int(item["series"]["colors"][1][:6], 16)


def test_parse_entry_without_items_is_skipped():
    assert fs.parse_entry({"offerId": "x", "finalPrice": 1}) is None
    assert fs.parse_entry("junk") is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [None, {}, {"status": 400, "error": "x"}, {"data": {"entries": None}}, {"data": {"entries": [], "date": "nope"}}],
)
def test_parse_shop_rejects_malformed(bad):
    with pytest.raises(fs.FortniteShopError):
        fs.parse_shop(bad)  # type: ignore[arg-type]


def test_shop_to_dict_is_json_serialisable(shop):
    d = shop.to_dict()
    json.dumps(d)
    assert d["entry_count"] == len(shop.entries)
    assert sum(s["count"] for s in d["sections"]) == len(shop.entries)


# ---------------------------------------------------------------- grouping


def test_group_sections_order(shop):
    sections = fs.group_sections(shop.entries)
    indexes = [s.index for s in sections]
    assert indexes == sorted(indexes)
    assert sections[-1].name == fs.OTHER_SECTION
    jam = next(s for s in sections if s.name == "Jam Tracks")
    assert jam.is_jam_tracks
    for s in sections:
        prios = [e.sort_priority for e in s.entries]
        assert prios == sorted(prios, reverse=True)


def test_ordered_entries_and_section_starts_agree(shop):
    ordered = fs.ordered_entries(shop)
    starts = fs.section_starts(shop)
    assert len(ordered) == len(shop.entries)
    for name, first, count in starts:
        assert all(e.section == name for e in ordered[first:first + count])


# ---------------------------------------------------------------- embeds


def _section_titles(embeds):
    return [e.title for e in embeds[1:]]


def test_post_embeds_collapse_jam_tracks_by_default(shop):
    embeds = fs.build_post_embeds(shop)
    assert "Jam Tracks" not in _section_titles(embeds)
    assert embeds[-1].title == "Also in the shop" and "Jam Tracks" in embeds[-1].description
    with_tracks = fs.build_post_embeds(shop, include_jam_tracks=True)
    assert "Jam Tracks" in _section_titles(with_tracks)
    assert fs.build_post_embeds(shop, test=True)[0].title.startswith("[TEST]")


def test_long_section_is_split_under_description_limit(shop):
    big = copy.deepcopy(shop)
    template = next(e for e in big.entries if e.kind == "br")
    for i in range(400):
        clone = copy.deepcopy(template)
        clone.name = f"Very Long Cosmetic Name Number {i:03d} With Padding"
        big.entries.append(clone)
    embeds = fs.build_post_embeds(big)
    conts = [e for e in embeds if e.title and e.title.endswith("(cont.)")]
    assert conts, "expected the oversized section to be split"
    assert all(len(e.description or "") <= fs.MAX_DESCRIPTION for e in embeds)


def test_batch_embeds_respects_discord_limits():
    embeds = [discord.Embed(title=f"t{i}", description="x" * 1500) for i in range(25)]
    batches = fs.batch_embeds(embeds)
    assert sum(len(b) for b in batches) == 25
    for b in batches:
        assert len(b) <= fs.MAX_EMBEDS_PER_MESSAGE
        assert sum(len(e) for e in b) <= fs.MAX_CHARS_PER_MESSAGE
    small = [discord.Embed(title=str(i)) for i in range(23)]
    assert [len(b) for b in fs.batch_embeds(small)] == [10, 10, 3]


def test_item_page(shop):
    e = fs.ordered_entries(shop)[0]
    page = fs.build_item_page(shop, e, 1, 14)
    assert page.title == e.name
    assert page.image.url == e.image_url
    assert "1/14" in page.footer.text
    assert len(page.fields) <= 25


# ---------------------------------------------------------------- wishlist + timing


def test_match_wishlist(shop):
    some = next(e for e in shop.entries if e.kind == "br")
    wishes = [
        {"id": 1, "user_id": 10, "query": some.name.lower()[:5], "last_notified_date": None},
        {"id": 2, "user_id": 10, "query": "definitely-not-an-item", "last_notified_date": None},
        {"id": 3, "user_id": 20, "query": some.name.lower(), "last_notified_date": shop.shop_date},  # already told
    ]
    matches = fs.match_wishlist(shop, wishes)
    assert list(matches) == [10]
    assert matches[10][0]["wish_id"] == 1 and some.name in matches[10][0]["items"]
    text = fs.build_wishlist_message(matches)
    assert "<@10>" in text and some.name in text
    assert fs.build_wishlist_message({}) is None


def test_next_post_time():
    before = datetime(2026, 10, 13, 0, 2, tzinfo=timezone.utc)
    after = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    assert fs.next_post_time(before, 5) == datetime(2026, 10, 13, 0, 5, tzinfo=timezone.utc)
    assert fs.next_post_time(after, 5) == datetime(2026, 10, 14, 0, 5, tzinfo=timezone.utc)


def test_is_post_due():
    now = datetime(2026, 10, 13, 0, 10, tzinfo=timezone.utc)
    cfg = {"enabled": True, "channel_id": 1, "last_posted_date": "2026-10-12"}
    assert fs.is_post_due(cfg, now, 5)
    assert not fs.is_post_due({**cfg, "last_posted_date": "2026-10-13"}, now, 5)
    assert not fs.is_post_due({**cfg, "enabled": False}, now, 5)
    assert not fs.is_post_due({**cfg, "channel_id": None}, now, 5)
    assert not fs.is_post_due(cfg, datetime(2026, 10, 13, 0, 1, tzinfo=timezone.utc), 5)
    assert not fs.is_post_due(None, now, 5)


# ---------------------------------------------------------------- repo + service (in-memory DB)


@pytest_asyncio.fixture
async def repo():
    db = Database(":memory:")
    await db.connect()
    await db.executescript(SQL.read_text(encoding="utf-8"))
    yield FortniteRepository(db)
    await db.close()


@pytest.mark.asyncio
async def test_repo_config_roundtrip(repo):
    assert await repo.get_config(GUILD_ID) is None
    cfg = await repo.update_config(GUILD_ID, channel_id=CHANNEL_ID, enabled=True)
    assert cfg["channel_id"] == CHANNEL_ID and cfg["enabled"] is True and cfg["include_jam_tracks"] is False
    cfg = await repo.update_config(GUILD_ID, ping_role_id=5)
    assert cfg["ping_role_id"] == 5
    cfg = await repo.update_config(GUILD_ID, ping_role_id=None)
    assert cfg["ping_role_id"] is None
    with pytest.raises(ValueError):
        await repo.update_config(GUILD_ID, bogus=1)


@pytest.mark.asyncio
async def test_repo_wishlist(repo):
    assert normalise_query("  Renegade   RAIDER ") == "renegade raider"
    assert await repo.add_wish(GUILD_ID, 1, "Renegade Raider") is True
    assert await repo.add_wish(GUILD_ID, 1, "renegade  raider") is False  # case/space-insensitive dupe
    assert [w["query"] for w in await repo.list_wishes(GUILD_ID, 1)] == ["renegade raider"]
    assert await repo.count_wishes(GUILD_ID) == {"entries": 1, "users": 1}
    with pytest.raises(ValueError):
        await repo.add_wish(GUILD_ID, 1, "   ")
    for i in range(MAX_WISHLIST_PER_USER - 1):
        await repo.add_wish(GUILD_ID, 1, f"item {i}")
    with pytest.raises(ValueError):
        await repo.add_wish(GUILD_ID, 1, "one too many")
    assert await repo.remove_wish(GUILD_ID, 1, "RENEGADE RAIDER") is True
    assert await repo.remove_wish(GUILD_ID, 1, "renegade raider") is False


def _guild_with_channel():
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHANNEL_ID
    channel.name = "shop"
    sent = []

    async def send(*args, **kwargs):
        msg = MagicMock()
        msg.id = 7000 + len(sent)
        sent.append((args, kwargs))
        return msg

    channel.send = AsyncMock(side_effect=send)
    guild = MagicMock()
    guild.id = GUILD_ID
    guild.get_channel = lambda cid: channel if cid == CHANNEL_ID else None
    return guild, channel, sent


@pytest.mark.asyncio
async def test_run_daily_posts_once_and_dedupes(repo):
    data = payload()
    fetcher = AsyncMock(return_value=data)
    service = fs.FortniteShopService(repo, fetcher=fetcher, post_delay_minutes=5)
    guild, channel, sent = _guild_with_channel()
    await repo.update_config(GUILD_ID, channel_id=CHANNEL_ID, enabled=True)
    target = next(e for e in fs.parse_shop(data).entries if e.kind == "br")
    await repo.add_wish(GUILD_ID, 42, target.name)

    now = datetime(2026, 10, 13, 0, 6, tzinfo=timezone.utc)
    assert await service.run_daily(guild, now=now) == "posted"
    cfg = await repo.get_config(GUILD_ID)
    assert cfg["last_posted_date"] == "2026-10-13" and cfg["last_message_id"] == 7000
    assert any("<@42>" in (a[0] if a else "") for a, _ in sent)  # wishlist ping message
    wishes = await repo.list_wishes(GUILD_ID, 42)
    assert wishes[0]["last_notified_date"] == "2026-10-13"
    for _args, kwargs in sent:
        if "embeds" in kwargs:
            assert len(kwargs["embeds"]) <= 10

    # A "restart": new service instance, same DB -> no second post.
    service2 = fs.FortniteShopService(repo, fetcher=fetcher, post_delay_minutes=5)
    n = len(sent)
    assert await service2.run_daily(guild, now=now) == "not_due"
    assert len(sent) == n


@pytest.mark.asyncio
async def test_run_daily_waits_for_rotation_and_survives_outage(repo):
    guild, _channel, sent = _guild_with_channel()
    await repo.update_config(GUILD_ID, channel_id=CHANNEL_ID, enabled=True)
    tomorrow = datetime(2026, 10, 14, 0, 6, tzinfo=timezone.utc)

    stale = fs.FortniteShopService(repo, fetcher=AsyncMock(return_value=payload()))
    assert await stale.run_daily(guild, now=tomorrow) == "stale_shop"  # API still has the 13th
    assert sent == []

    down = fs.FortniteShopService(repo, fetcher=AsyncMock(side_effect=fs.FortniteShopError("HTTP 503")))
    assert await down.run_daily(guild, now=tomorrow) == "fetch_failed"
    cfg = await repo.get_config(GUILD_ID)
    assert "HTTP 503" in cfg["last_error"] and cfg["last_posted_date"] is None


@pytest.mark.asyncio
async def test_post_shop_test_mode_never_pings(repo):
    service = fs.FortniteShopService(repo, fetcher=AsyncMock(return_value=payload()))
    shop = await service.get_shop()
    _guild, channel, sent = _guild_with_channel()
    await service.post_shop(channel, shop, ping_role_id=99, wishlist_text="<@1> hi", test=True)
    assert all(kwargs.get("content") is None for _a, kwargs in sent if "embeds" in kwargs)
    wl = sent[-1][1]["allowed_mentions"]
    assert wl.users is False and wl.roles is False and wl.everyone is False
    assert (await repo.get_config(GUILD_ID)) is None  # nothing written


@pytest.mark.asyncio
async def test_get_shop_cache_and_stale_fallback(repo, monkeypatch):
    data = payload()
    today = data["data"]["date"]
    monkeypatch.setattr(fs, "datetime", _FrozenDatetime.at(today))
    fetcher = AsyncMock(return_value=data)
    service = fs.FortniteShopService(repo, fetcher=fetcher)
    s1 = await service.get_shop()
    s2 = await service.get_shop()
    assert s1 is s2 and fetcher.await_count == 1
    fetcher.side_effect = fs.FortniteShopError("boom")
    s3 = await service.get_shop(force=False)  # cache is fresh -> no fetch at all
    assert s3 is s1
    with pytest.raises(fs.FortniteShopError):
        await service.get_shop(force=True)
    assert service.last_fetch_error == "boom"


class _FrozenDatetime(datetime):
    _now: datetime

    @classmethod
    def at(cls, iso: str):
        cls._now = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return cls

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls._now if tz is None else cls._now.astimezone(tz)


# ---------------------------------------------------------------- review fixes


def test_wishlist_messages_split_and_report_rendered_ids():
    matches = {
        1000 + i: [{"wish_id": i, "query": "x", "items": [f"Item number {i} " + "y" * 40]}]
        for i in range(80)
    }
    chunks = fs.build_wishlist_messages(matches, limit=500)
    assert len(chunks) > 1
    seen: list[int] = []
    for text, ids in chunks:
        assert len(text) <= 500
        for wid in ids:
            assert f"<@{1000 + wid}>" in text  # every id returned was really mentioned
        seen += ids
    assert sorted(seen) == list(range(80))  # nobody cut off
    assert fs.build_wishlist_messages({}) == []


def test_wishlist_long_single_line_keeps_mention():
    matches = {42: [{"wish_id": 1, "query": "a", "items": [f"Item {i:03d}" for i in range(500)]}]}
    [(text, ids)] = fs.build_wishlist_messages(matches, limit=300)
    assert len(text) <= 300 and "<@42>" in text and text.endswith("...") and ids == [1]


def test_compute_next_post_reports_pending_today():
    cfg = {"enabled": True, "channel_id": 1, "last_posted_date": "2026-10-12"}
    afternoon = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)
    assert fs.compute_next_post(cfg, afternoon, 5) == (afternoon, True)  # due now, not tomorrow
    gate = datetime(2026, 10, 13, 14, 5, tzinfo=timezone.utc)
    assert fs.compute_next_post(cfg, afternoon, 5, gate) == (gate, True)  # retry gate after a failed tick
    done = {**cfg, "last_posted_date": "2026-10-13"}
    assert fs.compute_next_post(done, afternoon, 5) == (datetime(2026, 10, 14, 0, 5, tzinfo=timezone.utc), False)
    early = datetime(2026, 10, 13, 0, 2, tzinfo=timezone.utc)
    assert fs.compute_next_post(cfg, early, 5) == (datetime(2026, 10, 13, 0, 5, tzinfo=timezone.utc), False)
    assert fs.compute_next_post({**cfg, "enabled": False}, afternoon, 5) == (None, False)


def test_entry_line_shows_rarity(shop):
    e = next(e for e in shop.entries if e.kind == "br" and e.rarity_label)
    assert e.rarity_label in fs._entry_line(e)


def _failing_channel(fail_on: int):
    """Channel whose send() raises discord.HTTPException on call number fail_on (0-based)."""
    guild, channel, sent = _guild_with_channel()
    calls = {"n": 0}
    ok_send = channel.send.side_effect

    async def send(*args, **kwargs):
        n = calls["n"]
        calls["n"] += 1
        if n == fail_on:
            resp = MagicMock(status=503, reason="Service Unavailable")
            raise discord.HTTPException(resp, "upstream down")
        return await ok_send(*args, **kwargs)

    channel.send = AsyncMock(side_effect=send)
    return guild, channel, sent


@pytest.mark.asyncio
async def test_run_daily_partial_post_is_not_reposted(repo):
    data = payload()
    service = fs.FortniteShopService(repo, fetcher=AsyncMock(return_value=data), post_delay_minutes=5)
    shop = fs.parse_shop(data)
    batches = fs.batch_embeds(fs.build_post_embeds(shop))
    if len(batches) < 2:  # make sure the shop needs several messages
        for i in range(60):
            e = copy.deepcopy(data["data"]["entries"][0])
            e["offerId"] = f"extra{i}"
            e.pop("bundle", None)
            for item in e.get("brItems") or []:
                item["name"] = f"Filler {i} " + "z" * 200
            data["data"]["entries"].append(e)
        assert len(fs.batch_embeds(fs.build_post_embeds(fs.parse_shop(data)))) >= 2
    guild, _channel, sent = _failing_channel(fail_on=1)  # first batch ok, second fails
    await repo.update_config(GUILD_ID, channel_id=CHANNEL_ID, enabled=True)
    now = datetime(2026, 10, 13, 0, 6, tzinfo=timezone.utc)

    assert await service.run_daily(guild, now=now) == "post_partial"
    cfg = await repo.get_config(GUILD_ID)
    assert cfg["last_posted_date"] == "2026-10-13"  # claimed: never re-posted
    assert "partial post" in cfg["last_error"] and cfg["last_message_id"] == 7000
    n = len(sent)
    assert n == 1
    assert await service.run_daily(guild, now=now) == "not_due"
    assert len(sent) == n


@pytest.mark.asyncio
async def test_run_daily_first_send_failure_releases_claim(repo):
    service = fs.FortniteShopService(repo, fetcher=AsyncMock(return_value=payload()), post_delay_minutes=5)
    guild, _channel, sent = _failing_channel(fail_on=0)
    await repo.update_config(GUILD_ID, channel_id=CHANNEL_ID, enabled=True)
    now = datetime(2026, 10, 13, 0, 6, tzinfo=timezone.utc)

    assert await service.run_daily(guild, now=now) == "post_failed"
    cfg = await repo.get_config(GUILD_ID)
    assert cfg["last_posted_date"] is None and "post failed" in cfg["last_error"]
    assert sent == []
    # The next tick retries and succeeds (nothing had gone out before).
    assert await service.run_daily(guild, now=now) == "posted"
    assert (await repo.get_config(GUILD_ID))["last_error"] is None


@pytest.mark.asyncio
async def test_status_next_post_is_now_when_today_pending(repo, monkeypatch):
    monkeypatch.setattr(fs, "datetime", _FrozenDatetime.at("2026-10-13T14:00:00Z"))
    service = fs.FortniteShopService(repo, fetcher=AsyncMock(return_value=payload()), post_delay_minutes=5)
    await repo.update_config(GUILD_ID, channel_id=CHANNEL_ID, enabled=True)
    st = await service.status(GUILD_ID)
    assert st["post_pending"] is True and st["next_post_at"].startswith("2026-10-13T14:00")


@pytest.mark.asyncio
async def test_manual_fetch_passes_attempts(repo):
    fetcher = AsyncMock(return_value=payload())
    service = fs.FortniteShopService(repo, fetcher=fetcher)
    await service.get_shop(force=True, attempts=fs.MANUAL_FETCH_ATTEMPTS)
    assert fetcher.await_args.kwargs == {"attempts": fs.MANUAL_FETCH_ATTEMPTS}
