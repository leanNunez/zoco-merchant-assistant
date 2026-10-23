"""Sales read from a JSON file, strictly.

The file is the merchant's record of the money, so anything doubtful stops the load: a
duplicate date, an amount that is not a decimal string, a net that does not add up, a key
nobody asked for. Failing at start-up beats answering a customer with a number that is
wrong.

Format: {"currency": "ARS", "days": [{"fecha": "YYYY-MM-DD", "entradas": 12,
"bruto": "1000.00", "devoluciones": "0.00", "neto": "1000.00"}, ...]}. Amounts are strings
because JSON numbers are floats.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from merchant_assistant.sales.contract import parse_iso_day
from merchant_assistant.sales.errors import InvalidSalesData, InvalidSalesDate
from merchant_assistant.sales.models import DailySales

_CURRENCY = "ARS"
_TOP_KEYS = frozenset({"currency", "days"})
_DAY_KEYS = frozenset({"fecha", "entradas", "bruto", "devoluciones", "neto"})
# Plain decimal notation with at most cents: no exponent, NaN or Infinity.
_AMOUNT = re.compile(r"-?[0-9]+(?:\.[0-9]{1,2})?")


class JsonSalesRepository:
    def __init__(self, path: Path) -> None:
        self._days = _load(path)

    def get(self, day: date) -> DailySales | None:
        return self._days.get(day)


def _load(path: Path) -> dict[date, DailySales]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise InvalidSalesData(f"cannot read {path}: {type(error).__name__}") from error
    except json.JSONDecodeError as error:
        raise InvalidSalesData(f"{path} is not valid JSON: {error}") from error

    if not isinstance(document, dict) or set(document) != _TOP_KEYS:
        raise InvalidSalesData(f"{path} must have exactly the keys {sorted(_TOP_KEYS)}")
    if document["currency"] != _CURRENCY:
        raise InvalidSalesData(
            f"{path}: currency must be {_CURRENCY}, got {document['currency']!r}"
        )
    entries = document["days"]
    if not isinstance(entries, list):
        raise InvalidSalesData(f"{path}: 'days' must be a list")

    days: dict[date, DailySales] = {}
    for index, entry in enumerate(entries):
        record = _record(entry, where=f"{path} days[{index}]")
        if record.day in days:
            raise InvalidSalesData(f"{path}: duplicate date {record.day} at days[{index}]")
        days[record.day] = record
    return days


def _record(entry: Any, *, where: str) -> DailySales:
    if not isinstance(entry, dict) or set(entry) != _DAY_KEYS:
        raise InvalidSalesData(f"{where} must have exactly the keys {sorted(_DAY_KEYS)}")
    tickets = entry["entradas"]
    if not isinstance(tickets, int) or isinstance(tickets, bool):
        raise InvalidSalesData(f"{where}: 'entradas' must be an integer")
    fecha = entry["fecha"]
    if not isinstance(fecha, str):
        raise InvalidSalesData(f"{where}: 'fecha' must be a string")
    try:
        day = parse_iso_day(fecha)
        return DailySales(
            day=day,
            tickets=tickets,
            gross=_amount(entry["bruto"], f"{where}: 'bruto'"),
            refunds=_amount(entry["devoluciones"], f"{where}: 'devoluciones'"),
            net=_amount(entry["neto"], f"{where}: 'neto'"),
        )
    except InvalidSalesDate as error:
        raise InvalidSalesData(f"{where}: {error.detail}") from error
    except ValueError as error:
        raise InvalidSalesData(f"{where}: {error}") from error


def _amount(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or _AMOUNT.fullmatch(value) is None:
        raise ValueError(f"{label} must be a decimal string with at most two decimals")
    return Decimal(value)
