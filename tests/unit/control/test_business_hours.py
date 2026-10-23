from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from merchant_assistant.control.hours import BusinessHours

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")  # UTC-3, no daylight saving time
MONDAY_TO_FRIDAY = frozenset({0, 1, 2, 3, 4})

# For these tests only: the domain itself defines no window.
OFFICE = BusinessHours(BUENOS_AIRES, time(9, 0), time(18, 0), MONDAY_TO_FRIDAY)

# 2026-10-23 is a Friday; 2026-10-24 a Saturday; 2026-10-26 a Monday.


def in_buenos_aires(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=BUENOS_AIRES)


def test_the_window_opens_exactly_at_the_opening_time() -> None:
    assert not OFFICE.is_open(in_buenos_aires(23, 8, 59, 59))
    assert OFFICE.is_open(in_buenos_aires(23, 9, 0))


def test_the_window_closes_exactly_at_the_closing_time() -> None:
    assert OFFICE.is_open(in_buenos_aires(23, 17, 59, 59))
    assert not OFFICE.is_open(in_buenos_aires(23, 18, 0))


def test_a_weekend_is_closed_even_at_midday() -> None:
    assert not OFFICE.is_open(in_buenos_aires(24, 12))
    assert not OFFICE.is_open(in_buenos_aires(25, 12))


def test_a_weekday_at_midday_is_open() -> None:
    assert OFFICE.is_open(in_buenos_aires(26, 12))


def test_the_clock_of_the_business_decides_not_the_clock_of_the_server() -> None:
    # 10:00 UTC reads like office time, but it is 07:00 in Buenos Aires.
    assert not OFFICE.is_open(datetime(2026, 10, 23, 10, 0, tzinfo=UTC))
    # 20:00 UTC reads like closing time, but it is 17:00 in Buenos Aires.
    assert OFFICE.is_open(datetime(2026, 10, 23, 20, 0, tzinfo=UTC))


def test_an_instant_that_is_tuesday_in_utc_can_still_be_monday_evening() -> None:
    mondays_only = BusinessHours(BUENOS_AIRES, time(9, 0), time(23, 0), frozenset({0}))
    tuesday_in_utc = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)  # Monday 22:00 in Buenos Aires

    assert tuesday_in_utc.weekday() == 1
    assert mondays_only.is_open(tuesday_in_utc)


def test_the_same_instant_gets_the_same_answer_in_any_timezone() -> None:
    instant_in_utc = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
    same_instant_in_tokyo = instant_in_utc.astimezone(ZoneInfo("Asia/Tokyo"))

    assert OFFICE.is_open(instant_in_utc)
    assert OFFICE.is_open(same_instant_in_tokyo)


def test_a_naive_datetime_is_rejected_because_its_zone_is_unknown() -> None:
    naive = datetime(2026, 10, 23, 12, 0)  # noqa: DTZ001 - naive on purpose

    with pytest.raises(ValueError, match="timezone-aware"):
        OFFICE.is_open(naive)


@pytest.mark.parametrize(
    ("opens", "closes"),
    [
        pytest.param(time(18, 0), time(9, 0), id="reversed"),
        pytest.param(time(9, 0), time(9, 0), id="empty window"),
        pytest.param(time(22, 0), time(6, 0), id="overnight windows are unsupported"),
    ],
)
def test_a_window_must_open_before_it_closes(opens: time, closes: time) -> None:
    with pytest.raises(ValueError, match="opens must be earlier than closes"):
        BusinessHours(BUENOS_AIRES, opens, closes, MONDAY_TO_FRIDAY)


def test_a_window_needs_at_least_one_weekday() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        BusinessHours(BUENOS_AIRES, time(9, 0), time(18, 0), frozenset())


@pytest.mark.parametrize("day", [-1, 7])
def test_a_weekday_outside_zero_to_six_is_rejected(day: int) -> None:
    with pytest.raises(ValueError, match="within 0"):
        BusinessHours(BUENOS_AIRES, time(9, 0), time(18, 0), frozenset({day}))


def test_the_opening_times_must_be_wall_clock_times_without_a_zone() -> None:
    with pytest.raises(ValueError, match="naive time"):
        BusinessHours(BUENOS_AIRES, time(9, 0, tzinfo=UTC), time(18, 0), MONDAY_TO_FRIDAY)
    with pytest.raises(ValueError, match="naive time"):
        BusinessHours(BUENOS_AIRES, time(9, 0), time(18, 0, tzinfo=UTC), MONDAY_TO_FRIDAY)
