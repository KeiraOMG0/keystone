"""services/debug_actions_service.py: SafeChannel never pings roles/everyone,
always labels messages, strips live views; actions run without touching
feature state."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from services import debug_actions_service as dbg

pytestmark = pytest.mark.asyncio

OWNER_ID = 111222333444555666


def _channel():
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = 42
    ch.name = "debug"
    sent = []

    async def send(content=None, **kwargs):
        msg = MagicMock()
        msg.id = 9000 + len(sent)
        msg.jump_url = f"https://discord.com/channels/1/42/{msg.id}"
        sent.append((content, kwargs))
        return msg

    ch.send = AsyncMock(side_effect=send)
    return ch, sent


def _owner():
    m = MagicMock(spec=discord.Member)
    m.id = OWNER_ID
    m.display_avatar.url = "https://cdn.discordapp.com/a.png"
    m.__str__ = lambda self: "owner"
    m.name = "owner"
    m.display_name = "owner"
    return m


def _bot(cogs: dict | None = None):
    bot = MagicMock()
    bot.latency = 0.05
    bot.user = MagicMock(id=5, name="bot")
    bot.get_cog = lambda name: (cogs or {}).get(name)
    return bot


async def test_safe_channel_forces_mentions_and_prefix():
    ch, sent = _channel()
    safe = dbg.SafeChannel(ch, OWNER_ID, "Label")
    await safe.send("@everyone <@&1> hi", allowed_mentions=discord.AllowedMentions.all())
    content, kwargs = sent[0]
    assert content.startswith("[TEST] Label\n")
    am = kwargs["allowed_mentions"]
    assert am.everyone is False and am.roles is False
    assert [u.id for u in am.users] == [OWNER_ID]
    await safe.send(embed=discord.Embed(title="x"))
    assert sent[1][0] == "[TEST] Label"
    assert safe.name == "debug"  # proxied attribute
    await safe.send("x" * 2100)
    assert len(sent[2][0]) == 2000


async def test_safe_channel_drops_live_views_keeps_disabled():
    ch, sent = _channel()
    safe = dbg.SafeChannel(ch, OWNER_ID, "L")
    live = discord.ui.View()
    live.add_item(discord.ui.Button(label="Open", custom_id="ticket_open:1"))
    await safe.send("a", view=live)
    assert "view" not in sent[0][1]
    inert = dbg.disabled_button_view([("Open", None), ("Role", "\N{GRINNING FACE}")])
    await safe.send("b", view=inert)
    assert sent[1][1]["view"] is inert
    assert all(item.disabled for item in inert.children)


async def test_run_plain_message_and_embed():
    ch, sent = _channel()
    result = await dbg.run_action(_bot(), MagicMock(id=1), ch, _owner(), "plain_message", {"text": "hello"})
    assert result["ok"] and result["message_ids"] == [9000] and result["jump_url"].endswith("/9000")
    assert sent[0][0] == "[TEST] Plain test message\nhello"
    result = await dbg.run_action(_bot(), MagicMock(id=1), ch, _owner(), "plain_embed", {"title": "T"})
    assert result["ok"] and sent[1][1]["embed"].title == "T"


async def test_run_log_embed_validates_choice():
    ch, sent = _channel()
    ok = await dbg.run_action(_bot(), MagicMock(id=1), ch, _owner(), "log_embed", {"log_action": "member_banned"})
    assert ok["ok"] and sent[0][1]["embed"].title == "Member Banned"
    bad = await dbg.run_action(_bot(), MagicMock(id=1), ch, _owner(), "log_embed", {"log_action": "nope"})
    assert not bad["ok"] and bad["error"] == "unknown log event"


async def test_unavailable_when_cog_missing():
    ch, sent = _channel()
    r = await dbg.run_action(_bot(), MagicMock(id=1), ch, _owner(), "welcome_message", {})
    assert not r["ok"] and "not loaded" in r["error"] and sent == []


async def test_qotd_does_not_write_history():
    ch, sent = _channel()
    qotd = MagicMock()
    qotd._repo.get_config = AsyncMock(return_value={"use_emojis": 0})
    qotd._repo.add_history = AsyncMock()
    qotd._service._build_embed = MagicMock(return_value=discord.Embed(title="Question of the Day"))
    qotd._service.post_qotd = AsyncMock()
    r = await dbg.run_action(_bot({"QOTD": qotd}), MagicMock(id=1), ch, _owner(), "qotd_post", {"question": "Q?"})
    assert r["ok"]
    qotd._service._build_embed.assert_called_once_with("Q?", use_emojis=False)
    qotd._repo.add_history.assert_not_called()
    qotd._service.post_qotd.assert_not_called()


async def test_fortnite_post_is_test_mode():
    ch, sent = _channel()
    svc = MagicMock()
    svc.get_shop = AsyncMock(return_value=MagicMock(shop_date="2026-10-13", entries=[1, 2]))
    async def post_shop(channel, shop, **kw):
        return [await channel.send(embeds=[])]

    svc.post_shop = AsyncMock(side_effect=post_shop)
    cog = MagicMock(shop_service=svc)
    r = await dbg.run_action(_bot({"FortniteShop": cog}), MagicMock(id=1), ch, _owner(), "fortnite_shop_post", {})
    assert r["ok"]
    assert svc.post_shop.await_args.kwargs["test"] is True
    svc.repo.update_config.assert_not_called()


async def test_discord_forbidden_reported():
    ch, _sent = _channel()
    ch.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403, reason="x"), "no"))
    r = await dbg.run_action(_bot(), MagicMock(id=1), ch, _owner(), "plain_message", {})
    assert not r["ok"] and "missing permissions" in r["error"]


async def test_catalogue_marks_unavailable_and_lists_skipped():
    cat = await dbg.catalogue(_bot(), MagicMock(id=1))
    by_id = {a["id"]: a for a in cat["actions"]}
    assert by_id["plain_message"]["available"] is True
    assert by_id["welcome_message"]["available"] is False
    assert by_id["log_embed"]["params"][0]["options"]
    assert cat["skipped"]
