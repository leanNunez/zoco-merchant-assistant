import json
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from merchant_assistant.sales.adapters.json_repository import JsonSalesRepository
from merchant_assistant.sales.errors import InvalidSalesData
from merchant_assistant.sales.models import DailySales

pytestmark = pytest.mark.integration

VALID: dict[str, Any] = {
    "currency": "ARS",
    "days": [
        {
            "fecha": "2026-10-01",
            "entradas": 12,
            "bruto": "150000.00",
            "devoluciones": "12500.50",
            "neto": "137499.50",
        },
        {
            "fecha": "2026-10-02",
            "entradas": 3,
            "bruto": "30000.00",
            "devoluciones": "45000.00",
            "neto": "-15000.00",
        },
    ],
}


def write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "ventas.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def with_day(**changes: object) -> dict[str, Any]:
    document = deepcopy(VALID)
    document["days"][0].update(changes)
    return document


def test_days_are_read_with_exact_decimal_money(tmp_path: Path) -> None:
    repository = JsonSalesRepository(write(tmp_path, VALID))

    assert repository.get(date(2026, 10, 1)) == DailySales(
        date(2026, 10, 1), 12, Decimal("150000.00"), Decimal("12500.50"), Decimal("137499.50")
    )


def test_a_negative_net_is_loaded(tmp_path: Path) -> None:
    found = JsonSalesRepository(write(tmp_path, VALID)).get(date(2026, 10, 2))

    assert found is not None
    assert found.net == Decimal("-15000.00")


def test_a_day_that_is_not_in_the_file_is_none(tmp_path: Path) -> None:
    assert JsonSalesRepository(write(tmp_path, VALID)).get(date(2026, 10, 3)) is None


def test_an_empty_list_of_days_is_a_valid_file(tmp_path: Path) -> None:
    repository = JsonSalesRepository(write(tmp_path, {"currency": "ARS", "days": []}))

    assert repository.get(date(2026, 10, 1)) is None


def duplicated() -> dict[str, Any]:
    document = deepcopy(VALID)
    document["days"].append(deepcopy(document["days"][0]))
    return document


def extra_top_key() -> dict[str, Any]:
    return {**deepcopy(VALID), "timezone": "UTC"}


def without_top_key() -> dict[str, Any]:
    return {"days": deepcopy(VALID["days"])}


def missing_day_key() -> dict[str, Any]:
    document = deepcopy(VALID)
    del document["days"][0]["neto"]
    return document


@pytest.mark.parametrize(
    ("document", "problem"),
    [
        (duplicated(), "duplicate date 2026-10-01"),
        (extra_top_key(), "must have exactly the keys ['currency', 'days']"),
        (without_top_key(), "must have exactly the keys ['currency', 'days']"),
        (missing_day_key(), "days[0] must have exactly the keys"),
        (with_day(extra="x"), "days[0] must have exactly the keys"),
        ({**VALID, "currency": "USD"}, "currency must be ARS"),
        ({**VALID, "days": {}}, "'days' must be a list"),
        ({**VALID, "days": ["2026-10-01"]}, "days[0] must have exactly the keys"),
        (["not", "an", "object"], "must have exactly the keys"),
        (with_day(fecha="2026-02-30"), "not a calendar date"),
        (with_day(fecha="1/10/2026"), "not in YYYY-MM-DD format"),
        (with_day(fecha=20261001), "'fecha' must be a string"),
        (with_day(entradas="12"), "'entradas' must be an integer"),
        (with_day(entradas=12.5), "'entradas' must be an integer"),
        (with_day(entradas=True), "'entradas' must be an integer"),
        (with_day(entradas=-1), "tickets cannot be negative"),
        (with_day(bruto=150000), "'bruto' must be a decimal string"),
        (with_day(bruto=150000.0), "'bruto' must be a decimal string"),
        (with_day(bruto="150.000,00"), "'bruto' must be a decimal string"),
        (with_day(bruto="1e5"), "'bruto' must be a decimal string"),
        (with_day(bruto="NaN"), "'bruto' must be a decimal string"),
        (with_day(bruto="10.001"), "'bruto' must be a decimal string"),
        (with_day(bruto=""), "'bruto' must be a decimal string"),
        (with_day(devoluciones=None), "'devoluciones' must be a decimal string"),
        (with_day(bruto="-1.00", neto="-13501.50"), "gross cannot be negative"),
        (with_day(devoluciones="-1.00", neto="150001.00"), "refunds cannot be negative"),
        (with_day(neto="137499.51"), "is not gross 150000.00 minus refunds 12500.50"),
    ],
)
def test_a_file_that_breaks_the_format_stops_the_load(
    tmp_path: Path, document: object, problem: str
) -> None:
    with pytest.raises(InvalidSalesData) as refused:
        JsonSalesRepository(write(tmp_path, document))

    assert problem in refused.value.detail
    assert refused.value.public() == {
        "code": "invalid_sales_data",
        "message": "The sales data could not be read.",
    }


def test_a_missing_file_stops_the_load(tmp_path: Path) -> None:
    with pytest.raises(InvalidSalesData, match="cannot read"):
        JsonSalesRepository(tmp_path / "absent.json")


def test_a_directory_instead_of_a_file_stops_the_load(tmp_path: Path) -> None:
    with pytest.raises(InvalidSalesData, match="cannot read"):
        JsonSalesRepository(tmp_path)


def test_a_file_that_is_not_json_stops_the_load(tmp_path: Path) -> None:
    path = tmp_path / "ventas.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(InvalidSalesData, match="is not valid JSON"):
        JsonSalesRepository(path)


def test_a_file_that_is_not_utf8_stops_the_load(tmp_path: Path) -> None:
    path = tmp_path / "ventas.json"
    path.write_bytes(b'{"currency": "\xff"}')

    with pytest.raises(InvalidSalesData, match="cannot read"):
        JsonSalesRepository(path)
