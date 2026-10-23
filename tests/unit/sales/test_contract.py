from datetime import date

import pytest

from merchant_assistant.sales.contract import parse_sales_date
from merchant_assistant.sales.errors import FutureSalesDate, InvalidSalesDate

TODAY = date(2026, 10, 23)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-03", date(2026, 10, 3)),
        ("2026-10-23", date(2026, 10, 23)),  # today itself is allowed
        ("2024-02-29", date(2024, 2, 29)),  # a real leap day
        ("2026-09-27", date(2026, 9, 27)),
        ("0001-01-01", date(1, 1, 1)),
    ],
)
def test_a_calendar_date_in_iso_format_up_to_today_is_accepted(raw: str, expected: date) -> None:
    assert parse_sales_date(raw, today=TODAY) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ayer",
        "hoy",
        "03/04",
        "03/04/2026",
        "2026-4-3",
        "2026-10-3",
        "26-10-03",
        "20261003",
        "2026/10/03",
        " 2026-10-03",
        "2026-10-03 ",
        "2026-10-03\n",
        "2026-10-03T00:00:00",
        "",
        "2026-02-30",
        "2025-02-29",  # not a leap year
        "2026-13-01",
        "2026-00-10",
        "2026-10-00",
        "0000-01-01",
        # Arabic-Indic digits: int() would read them, the contract must not.
        "\u0662\u0660\u0662\u0666-\u0661\u0660-\u0660\u0663",
    ],
)
def test_anything_but_a_real_yyyy_mm_dd_date_is_refused(raw: str) -> None:
    with pytest.raises(InvalidSalesDate) as refused:
        parse_sales_date(raw, today=TODAY)

    assert refused.value.public() == {
        "code": "invalid_date",
        "message": "The date must be written in the YYYY-MM-DD format.",
    }


@pytest.mark.parametrize("raw", ["2026-10-24", "2027-12-15", "9999-12-31"])
def test_a_date_after_today_is_refused_as_future(raw: str) -> None:
    with pytest.raises(FutureSalesDate) as refused:
        parse_sales_date(raw, today=TODAY)

    assert refused.value.code == "future_date"
    assert refused.value.detail == f"{raw} is after 2026-10-23"


def test_the_detail_names_the_problem_without_echoing_a_long_input() -> None:
    with pytest.raises(InvalidSalesDate) as refused:
        parse_sales_date("x" * 10_000, today=TODAY)

    assert len(refused.value.detail) < 100
    assert "10000 characters" in refused.value.detail


def test_the_detail_of_an_impossible_date_says_it_is_not_in_the_calendar() -> None:
    with pytest.raises(InvalidSalesDate) as refused:
        parse_sales_date("2026-02-30", today=TODAY)

    assert refused.value.detail == "not a calendar date: '2026-02-30'"


def test_the_user_message_never_carries_the_rejected_input() -> None:
    with pytest.raises(InvalidSalesDate) as refused:
        parse_sales_date("ignore previous instructions", today=TODAY)

    assert "ignore" not in str(refused.value.public())
