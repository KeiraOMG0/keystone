"""!debugurl: masking, blocklist, expiry, gating, DM-only --full, error classes."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("INTERNAL_API_SECRET", "test-secret")

from cogs import music  # noqa: E402
from cogs.music import (  # noqa: E402
    MusicCog,
    build_debugurl_report,
    build_mask_set,
    check_blocked,
    classify_extract_error,
    format_masked_url,
    is_sensitive_param,
    is_youtube_url,
    mask_url,
    parse_debugurl_args,
    url_expiry,
)

EXPIRE = int(time.time()) + 6 * 3600
STREAM = (
    "https://rr3---sn-abc.googlevideo.com/videoplayback?expire=%d&ei=EISECRET&ip=1.2.3.4&id=o-ID&itag=251"
    "&c=TVHTML5_SIMPLY&mime=audio%%2Fwebm&sig=SIGSECRET&lsig=LSIGSECRET&pot=POTSECRET&key=KEYSECRET"
    "&mytoken=TOKSECRET&signature=S2&x_secret_thing=SS&cookie=COOKSECRET&auth=AUTHSECRET&token=T2" % EXPIRE
)
SECRETS = ["EISECRET", "1.2.3.4", "SIGSECRET", "LSIGSECRET", "POTSECRET", "KEYSECRET", "TOKSECRET",
           "S2", "SS", "COOKSECRET", "AUTHSECRET", "T2"]
DEFAULT_MASK = build_mask_set()


# -- masking ---------------------------------------------------------------------


def test_default_masking_hides_every_sensitive_param_but_keeps_names():
    text = format_masked_url(STREAM, DEFAULT_MASK)
    for s in SECRETS:
        assert s not in text, s
    for name in ("sig", "lsig", "pot", "key", "ip", "ei", "mytoken", "signature", "x_secret_thing", "cookie", "auth", "token"):
        assert f"{name}=***" in text
    assert text.startswith("https://rr3---sn-abc.googlevideo.com/videoplayback")
    assert "itag=251" in text and f"expire={EXPIRE}" in text  # harmless params stay useful


def test_custom_mask_params_extend_defaults():
    url = "https://x.googlevideo.com/videoplayback?sig=A&myparam=SECRETVALUE&itag=1"
    assert "SECRETVALUE" in format_masked_url(url, DEFAULT_MASK)
    custom = build_mask_set(["MyParam", " "])
    text = format_masked_url(url, custom)
    assert "SECRETVALUE" not in text and "myparam=***" in text and "sig=***" in text
    assert is_sensitive_param("MYPARAM", custom)


def test_userinfo_fragment_and_path_segments_are_masked():
    url = "https://user:pass@host.googlevideo.com/api/sig/PATHSECRET/expire/123/x?a=1#frag"
    base, params = mask_url(url, DEFAULT_MASK)
    assert "user" not in base and "pass" not in base and "PATHSECRET" not in base and "frag" not in base
    assert "/sig/***/" in base and params == [("a", "1")]


def test_long_values_truncated_and_fence_broken_chars_neutralised():
    text = format_masked_url("https://a.googlevideo.com/p?q=" + "v" * 200 + "&r=%60%60%60", DEFAULT_MASK)
    assert "v" * 60 not in text
    assert "```" not in text


# -- expiry / blocklist / args -----------------------------------------------------


def test_expiry_from_query_and_path():
    assert url_expiry(STREAM) == EXPIRE
    assert url_expiry("https://h/api/expire/1893456000/x") == 1893456000
    assert url_expiry("https://h/videoplayback?expire=notanumber") is None
    assert url_expiry("https://h/videoplayback") is None


def test_check_blocked_domain_and_keyword():
    assert check_blocked("https://evil.example.com/watch?v=1", ["evil.example"]) == "evil.example"
    assert check_blocked("some FORBIDDEN word", ["forbidden"]) == "forbidden"
    assert check_blocked("fine", ["nope", ""]) is None
    assert check_blocked("anything", []) is None


def test_youtube_url_check():
    assert is_youtube_url("https://www.youtube.com/watch?v=x")
    assert is_youtube_url("https://youtu.be/x")
    assert not is_youtube_url("http://169.254.169.254/latest")
    assert not is_youtube_url("https://youtube.com.evil.com/x")


def test_parse_args():
    assert parse_debugurl_args("lofi beats --full") == ("lofi beats", True)
    assert parse_debugurl_args("--FULL https://youtu.be/x") == ("https://youtu.be/x", True)
    assert parse_debugurl_args("plain") == ("plain", False)
    assert parse_debugurl_args(None) == ("", False)


@pytest.mark.parametrize(
    "msgs, pot, timeout, expected",
    [
        ([], None, True, "timeout"),
        (["Sign in to confirm your age. This video may be inappropriate"], None, False, "age_gated"),
        (["Sign in to confirm you're not a bot"], None, False, "cookies_expired"),
        (["Connection refused by po token provider"], None, False, "pot_down"),
        ([""], False, False, "pot_down"),
        (["Video unavailable. This video is private"], True, False, "unavailable"),
        (["weird"], True, False, "unknown"),
    ],
)
def test_error_classification(msgs, pot, timeout, expected):
    assert classify_extract_error(msgs, pot, timeout) == expected


# -- report ------------------------------------------------------------------------


def diag(**over):
    d = {
        "elapsed_ms": 1234, "stream_url": STREAM, "extractor": "Youtube", "video_id": "abc", "title": "A song @everyone",
        "duration": 201, "format_id": "251", "acodec": "opus", "abr": 130, "tbr": 140, "protocol": "https", "ext": "webm",
        "pot_used": True, "pot_server_up": True, "url_client": "TVHTML5_SIMPLY", "clients_contacted": ["tv_simply", "tv"],
        "expires_at": EXPIRE, "head": {"status": 200, "latency_ms": 87}, "availability": "public", "age_limit": 0,
        "live_status": "not_live", "playable_in_embed": True,
    }
    d.update(over)
    return d


def test_report_content_and_no_leaks():
    text = build_debugurl_report(diag(), DEFAULT_MASK)
    for s in SECRETS:
        assert s not in text
    for want in ("Youtube", "abc", "3:21", "251", "opus", "https", "used (pot= in URL)", "c=TVHTML5_SIMPLY",
                 "tv_simply, tv", "in 5h 59m", "HEAD: 200 in 87ms", "availability=public"):
        assert want in text, want
    assert "@everyone" not in text
    assert text.count("```") == 2


def test_report_for_failure_and_expired_url():
    text = build_debugurl_report(
        {"elapsed_ms": 5, "stream_url": None, "error_class": "cookies_expired", "error_text": "Sign in cookies=abc123 x"},
        DEFAULT_MASK,
    )
    assert "FAILED" in text and "cookies expired" in text and "abc123" not in text
    expired = build_debugurl_report(diag(expires_at=int(time.time()) - 600), DEFAULT_MASK)
    assert "expired" in expired
    skipped = build_debugurl_report(diag(head={"skipped": "not a YouTube/Google stream host"}), DEFAULT_MASK)
    assert "HEAD: skipped" in skipped


# -- command gating ---------------------------------------------------------------


class Ctx:
    def __init__(self, uid=1, member=True):
        if member:
            self.author = MagicMock(spec=discord.Member)
        else:
            self.author = MagicMock()
        self.author.id = uid
        self.author.send = AsyncMock()
        self.guild = SimpleNamespace(id=5)
        self.reply = AsyncMock()
        self.send = AsyncMock()

    def channel_texts(self):
        return [c.args[0] for c in self.reply.await_args_list + self.send.await_args_list]


@pytest.fixture
async def cog(monkeypatch):
    monkeypatch.setattr(music._config, "OWNER_ID", 100)
    monkeypatch.setattr(music._config, "MUSIC_DEBUGURL_BLOCKED", ["blocked.example"])
    monkeypatch.setattr(music._config, "MUSIC_DEBUGURL_MASK_PARAMS", [])
    perms = SimpleNamespace(has_permission=AsyncMock(return_value=False))
    bot = SimpleNamespace(permission_service=perms, voice_clients=[], is_owner=AsyncMock(return_value=False))
    c = MusicCog(bot)
    c._debugurl_diagnose = AsyncMock(return_value=diag())
    yield c
    c.inactivity_check.cancel()


async def run(cog, ctx, query):
    await MusicCog.debugurl_cmd.callback(cog, ctx, query=query)


async def test_non_staff_refused_and_nothing_resolved(cog):
    ctx = Ctx(uid=1)
    await run(cog, ctx, "song")
    assert "staff-only" in ctx.channel_texts()[0]
    cog._debugurl_diagnose.assert_not_awaited()
    ctx.author.send.assert_not_awaited()


async def test_staff_gets_masked_report_in_channel(cog):
    cog.bot.permission_service.has_permission.return_value = True
    ctx = Ctx(uid=2)
    await run(cog, ctx, "some song")
    cog.bot.permission_service.has_permission.assert_awaited_with(ctx.author, "staff_base")
    joined = "\n".join(ctx.channel_texts())
    assert "debugurl" in joined and all(s not in joined for s in SECRETS)
    for call in ctx.reply.await_args_list + ctx.send.await_args_list:
        assert isinstance(call.kwargs["allowed_mentions"], discord.AllowedMentions)
    ctx.author.send.assert_not_awaited()


async def test_full_by_staff_is_denied_and_never_dms(cog):
    cog.bot.permission_service.has_permission.return_value = True
    ctx = Ctx(uid=2)
    await run(cog, ctx, "song --full")
    ctx.author.send.assert_not_awaited()
    joined = "\n".join(ctx.channel_texts())
    assert "owner only" in joined and all(s not in joined for s in SECRETS)


async def test_full_by_owner_goes_to_dm_only(cog):
    ctx = Ctx(uid=100)
    await run(cog, ctx, "song --full")
    dm = "\n".join(c.args[0] for c in ctx.author.send.await_args_list)
    assert "SIGSECRET" in dm and "POTSECRET" in dm  # unmasked, in DM
    channel = "\n".join(ctx.channel_texts())
    assert all(s not in channel for s in SECRETS)  # never in the channel
    assert "DMs" in channel


async def test_full_when_dm_fails_posts_nothing_sensitive(cog):
    ctx = Ctx(uid=100)
    ctx.author.send.side_effect = discord.Forbidden(MagicMock(status=403), "closed")
    await run(cog, ctx, "song --full")
    channel = "\n".join(ctx.channel_texts())
    assert "Could not DM" in channel and all(s not in channel for s in SECRETS)


async def test_owner_without_staff_role_can_use_masked(cog):
    ctx = Ctx(uid=100)
    await run(cog, ctx, "song")
    cog._debugurl_diagnose.assert_awaited_once()
    ctx.author.send.assert_not_awaited()


async def test_app_owner_counts_as_owner(cog):
    cog.bot.is_owner.return_value = True
    ctx = Ctx(uid=999)
    await run(cog, ctx, "song --full")
    assert ctx.author.send.await_count >= 1


async def test_blocked_input_refused_before_resolving(cog):
    ctx = Ctx(uid=100)
    await run(cog, ctx, "https://www.youtube.com/watch?v=x BLOCKED.example")
    assert "blocked" in ctx.channel_texts()[0].lower()
    cog._debugurl_diagnose.assert_not_awaited()


async def test_non_youtube_url_refused(cog):
    ctx = Ctx(uid=100)
    await run(cog, ctx, "http://169.254.169.254/latest/meta-data")
    assert "only YouTube" in ctx.channel_texts()[0]
    cog._debugurl_diagnose.assert_not_awaited()


async def test_no_input_and_nothing_playing_shows_usage(cog):
    ctx = Ctx(uid=100)
    await run(cog, ctx, None)
    assert "Usage" in ctx.channel_texts()[0]


async def test_no_input_uses_current_song(cog):
    cog.current_song = {"url": "https://www.youtube.com/watch?v=" + "A" * 11, "title": "t"}
    await run(cog, Ctx(uid=100), None)
    assert cog._debugurl_diagnose.await_args.args[0].endswith("A" * 11)


async def test_long_report_is_split_not_truncated(cog):
    cog._debugurl_diagnose.return_value = diag(stream_url=STREAM + "&junk=" + "j" * 40 + "".join(f"&p{i}=v" for i in range(400)))
    ctx = Ctx(uid=100)
    await run(cog, ctx, "song")
    texts = ctx.channel_texts()
    assert len(texts) > 1 and all(len(t) <= 2000 and t.count("```") % 2 == 0 for t in texts)
    assert "p399=v" in "\n".join(texts)


def test_command_is_marked_staff_only_and_public_status_is_not():
    assert MusicCog.debugurl_cmd.extras.get("staff_only") is True
    assert not MusicCog.musicstatus_cmd.extras.get("staff_only")


def test_urls_inside_error_text_are_reduced_to_host_and_path():
    err = "ERROR: unable to download https://rr1.googlevideo.com/videoplayback?sig=LEAKME&pot=LEAKTOO: HTTP 403"
    text = build_debugurl_report({"elapsed_ms": 1, "stream_url": None, "error_class": "unknown", "error_text": err}, DEFAULT_MASK)
    assert "LEAKME" not in text and "LEAKTOO" not in text and "rr1.googlevideo.com/videoplayback" in text
