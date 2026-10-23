"""The committed dataset must satisfy what the brief demands of it."""

import importlib.util
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from merchant_assistant.sales.adapters.json_repository import JsonSalesRepository
from merchant_assistant.sales.models import DailySales
from merchant_assistant.shared.clock import SystemClock, business_today
from merchant_assistant.shared.config import Settings

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
DATASET = ROOT / "data" / "ventas.json"
GENERATOR = ROOT / "scripts" / "generate_ventas.py"


def days() -> list[DailySales]:
    """Every record, read through the strict repository (so the file is also validated)."""
    repository = JsonSalesRepository(DATASET)
    listed = json.loads(DATASET.read_text(encoding="utf-8"))["days"]
    records = [repository.get(date.fromisoformat(entry["fecha"])) for entry in listed]
    return [record for record in records if record is not None]


def test_the_dataset_loads_through_the_strict_repository_with_decimal_money() -> None:
    records = days()

    assert len(records) >= 30
    assert all(isinstance(record.gross, Decimal) for record in records)


def test_the_history_spans_at_least_thirty_days_with_exactly_one_gap() -> None:
    # The latest record is the presale; everything before it is the history.
    *history, _presale = sorted(record.day for record in days())

    span = (history[-1] - history[0]).days + 1
    gaps = [
        day
        for offset in range(span)
        if (day := history[0] + timedelta(days=offset)) not in set(history)
    ]

    assert span >= 30
    assert gaps == [date(2026, 9, 27)]


def test_a_registered_day_with_zero_sales_exists_besides_the_gap() -> None:
    zero = [record for record in days() if record.tickets == 0]

    assert [record.day for record in zero] == [date(2026, 9, 28)]
    assert zero[0].net == Decimal("0.00")


def test_at_least_one_day_has_a_negative_net() -> None:
    negative = [record for record in days() if record.net < 0]

    assert [(r.day, r.tickets, r.gross, r.refunds, r.net) for r in negative] == [
        (date(2026, 10, 9), 3, Decimal("30000.00"), Decimal("45000.00"), Decimal("-15000.00"))
    ]


def test_at_least_one_record_is_after_the_current_date() -> None:
    # This guard intentionally depends on the real date: the dataset must keep a future
    # record for as long as the project is evaluated, which is why the presale is so far out.
    today = business_today(SystemClock(), Settings(_env_file=None).timezone)

    assert any(record.day > today for record in days())


def test_the_generator_reproduces_the_committed_file_byte_for_byte() -> None:
    spec = importlib.util.spec_from_file_location("generate_ventas", GENERATOR)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.render().encode("utf-8") == DATASET.read_bytes()
