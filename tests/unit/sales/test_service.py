from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from merchant_assistant.sales.errors import FutureSalesDate, InvalidSalesDate
from merchant_assistant.sales.models import DailySales
from merchant_assistant.sales.service import SalesService
from merchant_assistant.shared.clock import FixedClock

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
AUCKLAND = ZoneInfo("Pacific/Auckland")


class InMemorySales:
    """A repository that really stores days and counts how often it is read."""

    def __init__(self, *records: DailySales) -> None:
        self._days = {record.day: record for record in records}
        self.reads = 0

    def get(self, day: date) -> DailySales | None:
        self.reads += 1
        return self._days.get(day)


def record(day: date, tickets: int = 10, gross: str = "100.00") -> DailySales:
    amount = Decimal(gross)
    return DailySales(day, tickets, amount, Decimal("0.00"), amount)


def service(repository: InMemorySales, now: datetime, tz: tzinfo = BUENOS_AIRES) -> SalesService:
    return SalesService(repository, FixedClock(now), tz)


NOON_FRIDAY = datetime(2026, 10, 23, 12, 0, tzinfo=BUENOS_AIRES)


def test_a_registered_day_comes_back_with_its_record() -> None:
    sold = record(date(2026, 10, 3), tickets=7, gross="85000.00")

    found = service(InMemorySales(sold), NOON_FRIDAY).lookup("2026-10-03")

    assert found.day == date(2026, 10, 3)
    assert found.record == sold


def test_a_day_without_a_record_comes_back_as_no_sales_registered() -> None:
    found = service(InMemorySales(record(date(2026, 10, 3))), NOON_FRIDAY).lookup("2026-10-04")

    assert (found.day, found.record) == (date(2026, 10, 4), None)


def test_today_in_the_business_timezone_can_be_looked_up() -> None:
    today = record(date(2026, 10, 23))

    assert service(InMemorySales(today), NOON_FRIDAY).lookup("2026-10-23").record == today


def test_tomorrow_is_refused_without_reading_the_repository() -> None:
    repository = InMemorySales(record(date(2026, 10, 24)))

    with pytest.raises(FutureSalesDate):
        service(repository, NOON_FRIDAY).lookup("2026-10-24")

    assert repository.reads == 0


def test_an_invalid_date_is_refused_without_reading_the_repository() -> None:
    repository = InMemorySales()

    with pytest.raises(InvalidSalesDate):
        service(repository, NOON_FRIDAY).lookup("ayer")

    assert repository.reads == 0


def test_at_21_30_in_buenos_aires_the_server_is_in_tomorrow_but_the_merchant_is_not() -> None:
    # 21:30 in Buenos Aires is 00:30 of the next day in UTC. A service that took "today"
    # from the UTC date would let the merchant look up a day that has not started for them.
    evening = datetime(2026, 10, 23, 21, 30, tzinfo=BUENOS_AIRES)
    assert evening.astimezone(UTC).date() == date(2026, 10, 24)
    repository = InMemorySales(record(date(2026, 10, 23)), record(date(2026, 10, 24)))

    lookup = service(repository, evening)

    assert lookup.lookup("2026-10-23").record is not None
    with pytest.raises(FutureSalesDate):
        lookup.lookup("2026-10-24")


def test_a_day_that_is_tomorrow_in_utc_but_today_for_the_merchant_is_allowed() -> None:
    # The other side of the boundary: for a merchant ahead of UTC the date is already
    # 2026-10-24 while UTC still says 2026-10-23.
    instant = datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
    assert instant.astimezone(AUCKLAND).date() == date(2026, 10, 24)
    arrived = record(date(2026, 10, 24))

    assert service(InMemorySales(arrived), instant, AUCKLAND).lookup("2026-10-24").record == arrived


def test_the_boundary_is_the_local_midnight() -> None:
    last_second = datetime(2026, 10, 23, 23, 59, 59, tzinfo=BUENOS_AIRES)
    midnight = last_second + timedelta(seconds=1)

    with pytest.raises(FutureSalesDate):
        service(InMemorySales(), last_second).lookup("2026-10-24")
    assert service(InMemorySales(), midnight).lookup("2026-10-24").record is None
