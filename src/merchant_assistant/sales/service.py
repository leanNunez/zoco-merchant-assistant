"""The `consultar_ventas` use case: validate the date, then read."""

from __future__ import annotations

from datetime import tzinfo

from merchant_assistant.sales.contract import parse_sales_date
from merchant_assistant.sales.models import SalesLookup
from merchant_assistant.sales.repository import SalesRepository
from merchant_assistant.shared.clock import Clock, business_today


class SalesService:
    def __init__(self, repository: SalesRepository, clock: Clock, tz: tzinfo) -> None:
        self._repository = repository
        self._clock = clock
        self._tz = tz

    def lookup(self, raw_date: str) -> SalesLookup:
        # The business day, not the server's: a UTC server is already "tomorrow" at 21:00 in
        # Buenos Aires, and the merchant's today must still be allowed then.
        day = parse_sales_date(raw_date, today=business_today(self._clock, self._tz))
        return SalesLookup(day=day, record=self._repository.get(day))
