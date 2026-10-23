import pytest

from merchant_assistant.shared.errors import DomainError
from merchant_assistant.shared.faults import (
    FaultConfig,
    FaultInjector,
    FaultMode,
    SimulatedFailure,
    SimulatedTimeout,
    ToolFailure,
)

EVERY_MODE = frozenset(FaultMode)
NONE = frozenset[FaultMode]()
TIMEOUT, ERROR, LOST_ACK = FaultMode.TIMEOUT, FaultMode.ERROR, FaultMode.LOST_ACK


def run(injector: FaultInjector, supports: frozenset[FaultMode], calls: int) -> list[str | None]:
    return [fault.value if (fault := injector.next_fault(supports)) else None for _ in range(calls)]


# The literals below come from random.Random(seed) in a scratch session: the first draws of
# Random(0) are 0.844, 0.758, 0.421, 0.259, 0.511, 0.405, ... so at rate 0.5 only the third,
# fifth and seventh calls fault, and the generator then picks the mode among the sorted names.


def test_a_fixed_seed_reproduces_the_same_sequence_of_faults() -> None:
    config = FaultConfig(rate=0.5, seed=0, modes=EVERY_MODE)

    first = run(FaultInjector(config), EVERY_MODE, 8)
    second = run(FaultInjector(config), EVERY_MODE, 8)

    assert first == [None, None, "lost_ack", None, "lost_ack", None, "error", None]
    assert second == first


def test_another_seed_gives_another_sequence() -> None:
    injector = FaultInjector(FaultConfig(rate=0.3, seed=1, modes=EVERY_MODE))

    assert run(injector, EVERY_MODE, 10) == [
        "error", "lost_ack", None, None, None, "lost_ack", "lost_ack", None, None, "lost_ack",
    ]  # fmt: skip


def test_at_rate_one_every_call_faults_with_a_reproducible_mode() -> None:
    injector = FaultInjector(FaultConfig(rate=1.0, seed=7, modes=EVERY_MODE))

    assert run(injector, EVERY_MODE, 6) == [
        "error",
        "error",
        "timeout",
        "timeout",
        "timeout",
        "error",
    ]


def test_at_rate_zero_nothing_ever_faults() -> None:
    injector = FaultInjector(FaultConfig(rate=0.0, seed=7, modes=EVERY_MODE))

    assert run(injector, EVERY_MODE, 200) == [None] * 200


def test_a_mode_the_tool_does_not_support_is_never_chosen() -> None:
    injector = FaultInjector(FaultConfig(rate=0.5, seed=0, modes=EVERY_MODE))

    assert run(injector, frozenset({TIMEOUT, ERROR}), 8) == [
        None, None, "timeout", None, "timeout", None, "error", None,
    ]  # fmt: skip


def test_without_a_mode_in_common_nothing_faults_even_at_rate_one() -> None:
    injector = FaultInjector(FaultConfig(rate=1.0, seed=0, modes=frozenset({TIMEOUT, ERROR})))

    assert run(injector, frozenset({LOST_ACK}), 4) == [None] * 4
    assert run(injector, NONE, 4) == [None] * 4


def test_a_call_with_nothing_to_fault_still_consumes_its_draw() -> None:
    # Two calls that cannot fault (they would have been None anyway) still advance the
    # generator, so the third call sees the same draw as in the plain sequence above.
    injector = FaultInjector(FaultConfig(rate=0.5, seed=0, modes=EVERY_MODE))

    assert run(injector, NONE, 2) == [None, None]
    assert run(injector, EVERY_MODE, 1) == ["lost_ack"]


def test_a_disabled_injector_never_faults() -> None:
    assert run(FaultInjector.disabled(), EVERY_MODE, 100) == [None] * 100


@pytest.mark.parametrize("rate", [-0.1, 1.5, float("nan"), float("inf")])
def test_a_rate_outside_zero_to_one_is_refused(rate: float) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        FaultConfig(rate=rate, seed=0, modes=EVERY_MODE)


@pytest.mark.parametrize("rate", [0.0, 1.0])
def test_the_bounds_of_the_rate_are_valid(rate: float) -> None:
    assert FaultConfig(rate=rate, seed=0, modes=EVERY_MODE).rate == rate


def test_no_modes_is_valid_only_when_nothing_can_fault() -> None:
    assert FaultConfig(rate=0.0, seed=0, modes=NONE).modes == NONE

    with pytest.raises(ValueError, match="at least one failure mode"):
        FaultConfig(rate=0.1, seed=0, modes=NONE)


def test_the_modes_have_stable_names() -> None:
    assert sorted(mode.value for mode in FaultMode) == ["error", "lost_ack", "timeout"]


def test_the_simulated_failures_expose_a_stable_code_and_a_safe_message() -> None:
    assert SimulatedTimeout("calls=3").public() == {
        "code": "tool_timeout",
        "message": "The service did not answer in time.",
    }
    assert SimulatedFailure("calls=3").public() == {
        "code": "tool_unavailable",
        "message": "The service is not available right now.",
    }


def test_both_failures_are_retryable_domain_errors() -> None:
    for error in (SimulatedTimeout(), SimulatedFailure()):
        assert isinstance(error, ToolFailure)
        assert isinstance(error, DomainError)
        assert error.retryable is True
