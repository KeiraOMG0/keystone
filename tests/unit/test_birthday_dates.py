"""Birthday date math (services/birthday_service.py): year boundaries across
timezones, Feb 29 in non-leap years, role removal the day after. Pure."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from services.birthday_service import (
    birthday_month_days,
    current_occurrence_year,
    effective_birthday,
    is_birthday_on,
    next_celebration,
    should_celebrate,
    should_remove_birthday_role,
    upcoming_month_days,
)

UTC = ZoneInfo("UTC")
KIRITIMATI = ZoneInfo("Pacific/Kiritimati")  # UTC+14
HONOLULU = ZoneInfo("Pacific/Honolulu")      # UTC-10


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


# --- Jan 1 birthday in UTC+14: starts on Dec 31 UTC -------------------------
def test_jan1_birthday_in_utc_plus_14_is_the_new_year_occurrence():
    # 2026-12-31 10:00 UTC is 2027-01-01 00:00 in Kiritimati.
    now = utc(2026, 12, 31, 10, 0)
    assert current_occurrence_year(1, 1, KIRITIMATI, now) == 2027
    fire_at, year = next_celebration(1, 1, KIRITIMATI, utc(2026, 12, 30, 12))
    assert fire_at == utc(2026, 12, 31, 10, 0) and year == 2027


def test_jan1_occurrence_is_not_repeated_after_utc_new_year():
    # Celebrated at Dec 31 10:00 UTC and stored as the LOCAL year 2027.
    last = 2027
    # Later the same local day, now Jan 1 UTC: still the same occurrence.
    assert current_occurrence_year(1, 1, KIRITIMATI, utc(2027, 1, 1, 5)) == 2027
    assert should_celebrate(last, 2027) is False
    # Next year's occurrence is due again.
    assert should_celebrate(last, 2028) is True


# --- Dec 31 birthday in UTC-10: ends on Jan 1 UTC ---------------------------
def test_dec31_birthday_in_utc_minus_10_does_not_reannounce_on_jan1_utc():
    # 2027-01-01 05:00 UTC is still 2026-12-31 19:00 in Honolulu.
    now = utc(2027, 1, 1, 5)
    assert current_occurrence_year(12, 31, HONOLULU, now) == 2026
    # Already celebrated at Dec 31 10:00 UTC and stored as 2026.
    assert should_celebrate(2026, 2026) is False
    # And the role stays until the local day ends ...
    assert should_remove_birthday_role(12, 31, HONOLULU, now) is False
    # ... and comes off once it is Jan 1 locally (10:00 UTC Jan 1).
    assert should_remove_birthday_role(12, 31, HONOLULU, utc(2027, 1, 1, 10, 1)) is True


def test_role_removed_day_after_even_when_year_rolled_over():
    # Jan 1 birthday in UTC+14 celebrated on Dec 31 UTC; on Jan 2 local the
    # role must come off (the old code compared last_celebrated with the UTC
    # year and kept it for a year).
    assert should_remove_birthday_role(1, 1, KIRITIMATI, utc(2027, 1, 1, 10, 0)) is True
    assert should_remove_birthday_role(1, 1, KIRITIMATI, utc(2027, 1, 1, 9, 59)) is False


def test_next_celebration_during_birthday_fires_now():
    now = utc(2026, 10, 14, 12)
    fire_at, year = next_celebration(10, 14, UTC, now)
    assert fire_at == now and year == 2026


def test_next_celebration_outside_window_is_none():
    assert next_celebration(3, 1, UTC, utc(2026, 1, 1), window_days=14) is None


# --- Feb 29 ------------------------------------------------------------------
def test_feb29_celebrated_on_feb28_in_non_leap_years():
    assert effective_birthday(2, 29, 2027) == date(2027, 2, 28)
    assert effective_birthday(2, 29, 2028) == date(2028, 2, 29)
    assert is_birthday_on(2, 29, date(2027, 2, 28)) is True
    assert is_birthday_on(2, 29, date(2028, 2, 28)) is False
    assert is_birthday_on(2, 29, date(2028, 2, 29)) is True
    assert effective_birthday(2, 29, 2100) == date(2100, 2, 28)  # century, not leap
    assert effective_birthday(2, 29, 2000) == date(2000, 2, 29)


def test_feb29_scheduled_for_feb28_midnight_local():
    fire_at, year = next_celebration(2, 29, UTC, utc(2027, 2, 20))
    assert fire_at == utc(2027, 2, 28) and year == 2027
    # Not "still my birthday" on Mar 1 of a non-leap year.
    assert current_occurrence_year(2, 29, UTC, utc(2027, 3, 1, 1)) is None


def test_feb29_rows_fetched_in_non_leap_window():
    assert (2, 29) in birthday_month_days(date(2027, 2, 28))
    assert (2, 29) not in birthday_month_days(date(2028, 2, 28))
    assert (2, 29) in upcoming_month_days(utc(2027, 2, 20), 14)


def test_upcoming_window_includes_yesterday_for_utc_minus_zones():
    pairs = upcoming_month_days(utc(2027, 1, 1, 5), 14)
    assert (12, 31) in pairs and (1, 1) in pairs and (1, 15) in pairs


def test_should_celebrate_never_without_occurrence():
    assert should_celebrate(0, None) is False
    assert should_celebrate(None, 2026) is True
