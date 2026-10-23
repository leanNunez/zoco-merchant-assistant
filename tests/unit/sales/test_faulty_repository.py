from datetime import date
from decimal import Decimal

import pytest

from merchant_assistant.sales.adapters.faulty_repository import FaultySalesRepository
from merchant_assistant.sales.models import DailySales
from merchant_assistant.shared.faults import (
    FaultConfig,
    FaultInjector,
    FaultMode,
    SimulatedFailure,
    SimulatedTimeout,
)

DAY = date(2026, 10, 3)
SOLD = DailySales(DAY, 5, Decimal("500.00"), Decimal("0.00"), Decimal("500.00"))


class InMemorySales:
    def __init__(self) -> None:
        self.reads: list[date] = []

    def get(self, day: date) -> DailySales | None:
        self.reads.append(day)
        return SOLD if day == DAY else None


def injector(rate: float, *modes: FaultMode, seed: int = 0) -> FaultInjector:
    return FaultInjector(FaultConfig(rate=rate, seed=seed, modes=frozenset(modes)))


def test_without_faults_the_read_goes_through_untouched() -> None:
    inner = InMemorySales()
    repository = FaultySalesRepository(inner, FaultInjector.disabled())

    assert repository.get(DAY) == SOLD
    assert repository.get(date(2026, 10, 4)) is None
    assert inner.reads == [DAY, date(2026, 10, 4)]


def test_a_timeout_is_raised_before_the_inner_repository_is_read() -> None:
    inner = InMemorySales()
    repository = FaultySalesRepository(inner, injector(1.0, FaultMode.TIMEOUT))

    with pytest.raises(SimulatedTimeout):
        repository.get(DAY)

    assert inner.reads == []


def test_an_error_is_raised_before_the_inner_repository_is_read() -> None:
    inner = InMemorySales()
    repository = FaultySalesRepository(inner, injector(1.0, FaultMode.ERROR))

    with pytest.raises(SimulatedFailure):
        repository.get(DAY)

    assert inner.reads == []


def test_a_lost_acknowledgement_makes_no_sense_for_a_read_so_it_never_fires() -> None:
    inner = InMemorySales()
    repository = FaultySalesRepository(inner, injector(1.0, FaultMode.LOST_ACK))

    assert repository.get(DAY) == SOLD
    assert inner.reads == [DAY]


def test_the_failures_follow_the_seeded_sequence() -> None:
    # Random(0) at rate 0.5: the third call draws a fault and picks timeout, the seventh error.
    repository = FaultySalesRepository(
        InMemorySales(), injector(0.5, FaultMode.TIMEOUT, FaultMode.ERROR, FaultMode.LOST_ACK)
    )
    outcomes: list[str] = []
    for _ in range(8):
        try:
            repository.get(DAY)
            outcomes.append("ok")
        except (SimulatedTimeout, SimulatedFailure) as failure:
            outcomes.append(failure.code)

    assert outcomes == [
        "ok", "ok", "tool_timeout", "ok", "tool_timeout", "ok", "tool_unavailable", "ok",
    ]  # fmt: skip
