"""A sales repository that fails on demand, for the simulated-failure scenarios."""

from __future__ import annotations

from datetime import date

from merchant_assistant.sales.models import DailySales
from merchant_assistant.sales.repository import SalesRepository
from merchant_assistant.shared.faults import (
    FaultInjector,
    FaultMode,
    SimulatedFailure,
    SimulatedTimeout,
)

# A read has no effect to acknowledge, so a lost acknowledgement cannot happen here.
SUPPORTED_FAULTS = frozenset({FaultMode.TIMEOUT, FaultMode.ERROR})


class FaultySalesRepository:
    def __init__(self, inner: SalesRepository, injector: FaultInjector) -> None:
        self._inner = inner
        self._injector = injector

    def get(self, day: date) -> DailySales | None:
        # The fault is raised before the inner repository is touched: the call "never arrived".
        fault = self._injector.next_fault(SUPPORTED_FAULTS)
        if fault is FaultMode.TIMEOUT:
            raise SimulatedTimeout(f"simulated timeout reading sales for {day}")
        if fault is FaultMode.ERROR:
            raise SimulatedFailure(f"simulated failure reading sales for {day}")
        return self._inner.get(day)
