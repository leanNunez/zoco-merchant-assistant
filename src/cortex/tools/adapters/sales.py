"""`consultar_ventas` over the kit's two-company fixture, reusing the first delivery's sales model.

The book is split by company when it is loaded; an adapter is bound to one company and cannot
see another one's days.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from pydantic import BaseModel

from cortex.identity import IDENTIFIER, AuthContext
from merchant_assistant.agent.domain.tools import ConsultarVentasArgs
from merchant_assistant.sales.contract import parse_iso_day
from merchant_assistant.sales.models import DailySales


class SalesBook:
    def __init__(self, days_by_tenant: Mapping[str, Mapping[date, DailySales]]) -> None:
        self._days = MappingProxyType(
            {tenant: MappingProxyType(dict(days)) for tenant, days in days_by_tenant.items()}
        )

    @classmethod
    def from_fixture(cls, rows: list[Mapping[str, object]]) -> SalesBook:
        days: dict[str, dict[date, DailySales]] = {}
        for row in rows:
            tenant = str(row["tenant_id"])
            if IDENTIFIER.fullmatch(tenant) is None:
                raise ValueError(f"not a valid tenant id: {tenant!r}")
            record = DailySales(
                day=parse_iso_day(str(row["fecha"])),
                tickets=_count(row["cantidad"]),
                gross=_amount(row["bruto"]),
                refunds=_amount(row["devoluciones"]),
                net=_amount(row["neto"]),
            )
            if record.day in days.setdefault(tenant, {}):
                raise ValueError(f"{tenant} has two rows for {record.day}")
            days[tenant][record.day] = record
        return cls(days)

    def for_tenant(self, auth: AuthContext) -> SalesAdapter:
        return SalesAdapter(auth.tenant_id, self._days.get(auth.tenant_id, MappingProxyType({})))


class SalesAdapter:
    def __init__(self, tenant_id: str, days: Mapping[date, DailySales]) -> None:
        self.tenant_id = tenant_id
        self._days = days

    def __call__(self, arguments: BaseModel) -> Mapping[str, object]:
        if not isinstance(arguments, ConsultarVentasArgs):
            raise TypeError("consultar_ventas received arguments of another tool")
        day = parse_iso_day(arguments.fecha)
        record = self._days.get(day)
        if record is None:
            # No sales registered is not the same as a day with zero sales.
            return {"fecha": day.isoformat(), "registrado": False}
        return {
            "fecha": day.isoformat(),
            "registrado": True,
            "cantidad": record.tickets,
            "bruto": _number(record.gross),
            "devoluciones": _number(record.refunds),
            "neto": _number(record.net),
        }


def _count(value: object) -> int:
    if type(value) is not int:
        raise ValueError(f"a count must be an integer, got {value!r}")
    return value


def _amount(value: object) -> Decimal:
    if type(value) not in (int, str):
        raise ValueError(f"an amount must be an integer or a decimal string, got {value!r}")
    return Decimal(str(value))


def _number(amount: Decimal) -> int | str:
    return int(amount) if amount == amount.to_integral_value() else str(amount)
