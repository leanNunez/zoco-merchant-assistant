from datetime import date
from decimal import Decimal

import pytest

from merchant_assistant.sales.models import DailySales, SalesLookup

DAY = date(2026, 10, 9)


def sales(
    *, tickets: int = 3, gross: str = "30000.00", refunds: str = "0.00", net: str = "30000.00"
) -> DailySales:
    return DailySales(DAY, tickets, Decimal(gross), Decimal(refunds), Decimal(net))


def test_a_consistent_day_is_accepted() -> None:
    record = sales(refunds="5000.50", net="24999.50")

    assert (record.gross, record.refunds, record.net) == (
        Decimal("30000.00"),
        Decimal("5000.50"),
        Decimal("24999.50"),
    )


def test_a_negative_net_is_valid_when_refunds_exceed_the_gross() -> None:
    record = sales(gross="30000.00", refunds="45000.00", net="-15000.00")

    assert record.net == Decimal("-15000.00")


def test_a_day_with_zero_sales_is_valid() -> None:
    assert sales(tickets=0, gross="0.00", refunds="0.00", net="0.00").tickets == 0


def test_money_is_decimal_so_cents_add_up_exactly() -> None:
    # With floats 0.1 + 0.2 == 0.30000000000000004 and this record would be refused.
    assert sales(gross="0.30", refunds="0.20", net="0.10").net == Decimal("0.10")


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"tickets": -1}, "tickets cannot be negative"),
        ({"gross": "-1.00", "net": "-1.00"}, "gross cannot be negative"),
        ({"refunds": "-1.00", "net": "30001.00"}, "refunds cannot be negative"),
        ({"net": "29999.99"}, "is not gross 30000.00 minus refunds 0.00"),
        ({"refunds": "100.00"}, "is not gross 30000.00 minus refunds 100.00"),
        ({"gross": "NaN"}, "gross must be a finite amount"),
        ({"net": "Infinity"}, "net must be a finite amount"),
    ],
)
def test_an_inconsistent_day_is_refused(fields: dict[str, object], problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        sales(**fields)  # type: ignore[arg-type]


def test_a_lookup_without_a_record_is_not_a_day_with_zero_sales() -> None:
    nothing = SalesLookup(DAY, None)
    zero = SalesLookup(DAY, sales(tickets=0, gross="0.00", refunds="0.00", net="0.00"))

    assert nothing.record is None
    assert zero.record is not None
    assert nothing != zero


def test_a_lookup_refuses_a_record_of_another_day() -> None:
    with pytest.raises(ValueError, match="the lookup is for 2026-10-10"):
        SalesLookup(date(2026, 10, 10), sales())
