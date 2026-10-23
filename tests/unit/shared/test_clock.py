from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from merchant_assistant.shared.clock import FixedClock, SystemClock, business_today

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")


def test_the_system_clock_returns_an_aware_utc_instant() -> None:
    now = SystemClock().now()

    assert now.utcoffset() == timedelta(0)


def test_a_fixed_clock_always_returns_its_instant() -> None:
    instant = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

    assert FixedClock(instant).now() == instant


def test_a_fixed_clock_rejects_a_naive_datetime() -> None:
    naive = datetime(2026, 10, 23, 12, 0)  # noqa: DTZ001 - naive on purpose

    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(naive)


def test_after_21_in_buenos_aires_the_server_date_is_already_tomorrow() -> None:
    # 21:30 in Buenos Aires is 00:30 UTC of the next day: the two calendars disagree.
    clock = FixedClock(datetime(2026, 10, 24, 0, 30, tzinfo=UTC))

    assert clock.now().date() == date(2026, 10, 24)
    assert business_today(clock, BUENOS_AIRES) == date(2026, 10, 23)


def test_during_the_business_day_both_calendars_agree() -> None:
    clock = FixedClock(datetime(2026, 10, 23, 15, 0, tzinfo=UTC))

    assert business_today(clock, BUENOS_AIRES) == clock.now().date()
