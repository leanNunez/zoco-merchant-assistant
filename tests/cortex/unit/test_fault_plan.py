from collections.abc import Mapping

import pytest
from pydantic import BaseModel

from cortex.identity import PrincipalDirectory, PrincipalRecord
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.faults import FaultKind, FaultPlan
from cortex.tools.failures import PermanentFailure

AUTH = PrincipalDirectory([PrincipalRecord("comercio_a", "operador_a", frozenset())]).authenticate(
    "comercio_a", "operador_a"
)


class _Empty(BaseModel):
    pass


class _Real:
    tenant_id = "comercio_a"

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        self.calls += 1
        return {"ok": True}

    def lookup(self, operation_id: str) -> Mapping[str, object] | None:
        return {"found": operation_id}


def _adapter(*kinds: FaultKind) -> tuple[_Real, FaultPlan, ManualTicker]:
    real, ticker = _Real(), ManualTicker()
    plan = FaultPlan({"tool": kinds}, ticker=ticker, timeout_cost_ms=3000)
    return real, plan, ticker


def _call(plan: FaultPlan, real: _Real) -> Mapping[str, object]:
    bound = plan.wrap(lambda auth: {"tool": real})(AUTH)["tool"]
    return bound(_Empty(), conversation_id="conv_1")


def test_calls_follow_the_sequence_and_then_succeed() -> None:
    real, plan, ticker = _adapter(FaultKind.TIMEOUT, FaultKind.SUCCESS)

    with pytest.raises(TimeoutError):
        _call(plan, real)
    assert real.calls == 0  # a plain timeout never reached the system
    assert ticker.seconds() == 3.0  # and it cost the tool's whole time limit

    assert _call(plan, real) == {"ok": True}
    assert _call(plan, real) == {"ok": True}  # the sequence is used up: back to normal
    assert real.calls == 2


def test_committed_but_timeout_applies_the_effect_and_loses_the_answer() -> None:
    real, plan, _ = _adapter(FaultKind.COMMITTED_BUT_TIMEOUT)

    with pytest.raises(TimeoutError):
        _call(plan, real)

    assert real.calls == 1


def test_an_invalid_request_is_a_certain_refusal_and_costs_no_time() -> None:
    real, plan, ticker = _adapter(FaultKind.INVALID_REQUEST)

    with pytest.raises(PermanentFailure):
        _call(plan, real)

    assert (real.calls, ticker.seconds()) == (0, 0.0)


def test_the_lookup_of_a_write_is_never_faulted() -> None:
    real, plan, _ = _adapter(FaultKind.TIMEOUT)
    bound = plan.wrap(lambda auth: {"tool": real})(AUTH)["tool"]

    assert bound.lookup("op_1") == {"found": "op_1"}  # type: ignore[attr-defined]


def test_two_plans_never_share_their_sequences() -> None:
    first = FaultPlan({"tool": [FaultKind.TIMEOUT]}, ticker=ManualTicker(), timeout_cost_ms=1)
    second = FaultPlan({"tool": [FaultKind.TIMEOUT]}, ticker=ManualTicker(), timeout_cost_ms=1)

    assert first.next_for("tool") is FaultKind.TIMEOUT
    assert second.next_for("tool") is FaultKind.TIMEOUT
    assert first.next_for("tool") is FaultKind.SUCCESS
