from datetime import datetime, timedelta, timezone

import pytest

from services.honeypot_service import compute_stats, message_flags

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def ev(minutes_ago=10, **kw):
    base = dict(
        created_at=(NOW - timedelta(minutes=minutes_ago)).isoformat(), action="ban", action_result="ok",
        content="hi", dm_sent=1, account_created_at=None, seconds_since_join=None, had_default_avatar=0,
        flag_link=0, flag_invite=0, flag_everyone=0, flag_attachment=0,
    )
    base.update(kw)
    return base


ZERO_APPEALS = {"invited": 0, "pending": 0, "approved": 0, "denied": 0}


def test_empty_stats():
    s = compute_stats([], ZERO_APPEALS, NOW)
    assert s["totals"] == {"all": 0, "last_24h": 0, "last_7d": 0, "last_30d": 0}
    assert s["by_action"] == {"ban": 0, "softban": 0, "kick": 0}
    assert len(s["timeline_30d"]) == 30 and all(d["count"] == 0 for d in s["timeline_30d"])
    assert s["timeline_30d"][-1]["date"] == "2026-09-29" and s["timeline_30d"][0]["date"] == "2026-08-31"
    assert s["by_hour_utc"] == [0] * 24
    assert s["account_age"] == {"avg_days": None, "median_days": None, "under_7d_pct": None, "under_30d_pct": None}
    assert s["time_to_catch"] == {"median_seconds": None, "under_60s_pct": None, "under_1h_pct": None}
    assert all(v is None for v in s["signals"].values())
    assert s["top_messages"] == []
    assert s["appeals"]["total"] == 0 and s["appeals"]["false_positive_pct"] is None
    assert s["first_catch_at"] is None and s["last_catch_at"] is None


def test_totals_windows_and_timeline():
    events = [ev(60), ev(60 * 24 * 3), ev(60 * 24 * 20), ev(60 * 24 * 60)]
    s = compute_stats(events, ZERO_APPEALS, NOW)
    assert s["totals"] == {"all": 4, "last_24h": 1, "last_7d": 2, "last_30d": 3}
    assert sum(d["count"] for d in s["timeline_30d"]) == 3
    assert s["timeline_30d"][-1]["count"] == 1
    assert s["by_hour_utc"][11] >= 1 and sum(s["by_hour_utc"]) == 4
    assert s["first_catch_at"] < s["last_catch_at"]


def test_by_action_and_result():
    events = [ev(action="ban"), ev(action="kick", action_result="failed"), ev(action="softban", action_result="partial")]
    s = compute_stats(events, ZERO_APPEALS, NOW)
    assert s["by_action"] == {"ban": 1, "softban": 1, "kick": 1}
    assert s["by_result"] == {"ok": 1, "partial": 1, "failed": 1}


def test_percentages_and_medians():
    e = [
        ev(account_created_at=(NOW - timedelta(days=2)).isoformat(), seconds_since_join=30,
           had_default_avatar=1, flag_link=1, dm_sent=1),
        ev(account_created_at=(NOW - timedelta(days=100)).isoformat(), seconds_since_join=7200,
           flag_link=1, flag_invite=1, dm_sent=0),
        ev(),
        ev(),
    ]
    s = compute_stats(e, ZERO_APPEALS, NOW)
    assert s["signals"]["link_pct"] == 50.0
    assert s["signals"]["invite_pct"] == 25.0
    assert s["signals"]["default_avatar_pct"] == 25.0
    assert s["signals"]["dm_delivered_pct"] == 75.0
    assert s["account_age"]["under_7d_pct"] == 50.0 and s["account_age"]["under_30d_pct"] == 50.0
    assert s["account_age"]["median_days"] == pytest.approx(51.0, abs=0.1)
    assert s["time_to_catch"]["under_60s_pct"] == 50.0 and s["time_to_catch"]["under_1h_pct"] == 50.0
    assert s["time_to_catch"]["median_seconds"] == 3615.0


def test_top_messages_normalised_and_min_count():
    e = [ev(content="Free  NITRO"), ev(content="free nitro"), ev(content=" FREE nitro "), ev(content="unique"),
         ev(content="spam"), ev(content="Spam"), ev(content="")]
    s = compute_stats(e, ZERO_APPEALS, NOW)
    assert s["top_messages"] == [{"preview": "free nitro", "count": 3}, {"preview": "spam", "count": 2}]


def test_appeal_numbers_and_false_positive_rate():
    events = [ev(action="ban")] * 4 + [ev(action="kick")]
    s = compute_stats(events, {"invited": 2, "pending": 1, "approved": 1, "denied": 0}, NOW)
    assert s["appeals"] == {"total": 4, "invited": 2, "pending": 1, "approved": 1, "denied": 0,
                            "false_positive_pct": 25.0}


def test_message_flags():
    f = message_flags("join discord.gg/abc or https://evil.example @everyone", False, False)
    assert f == {"link": True, "invite": True, "everyone": True, "attachment": False}
    f = message_flags("hello", True, True)
    assert f == {"link": False, "invite": False, "everyone": True, "attachment": True}
    assert message_flags("www.foo.com", False, False)["link"] is True
