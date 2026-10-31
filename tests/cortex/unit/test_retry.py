import pytest

from cortex.tools.failures import (
    FailureKind,
    PermanentFailure,
    ToolFailure,
    TransientFailure,
    UncertainOutcome,
)
from cortex.tools.retry import MAX_BACKOFF_MS, Retrier, jitter_for


class _Attempts:
    """A call that fails with the given failures, in order, and then succeeds."""

    def __init__(self, *failures: ToolFailure) -> None:
        self.failures = list(failures)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


def _retrier(*, max_retries: int = 2, remaining_ms: int = 10_000) -> tuple[Retrier, list[float]]:
    slept: list[float] = []
    retrier = Retrier(
        max_retries=max_retries,
        remaining_ms=lambda: remaining_ms,
        sleep=slept.append,
        jitter=lambda: 1.0,
        on_retry=lambda retry_number, failure: None,
    )
    return retrier, slept


def test_a_transient_failure_is_retried_until_it_works() -> None:
    retrier, slept = _retrier()
    attempts = _Attempts(TransientFailure("t1"), TransientFailure("t2"))

    assert retrier.call(attempts) == "ok"
    assert attempts.calls == 3
    assert slept == [0.1, 0.2]  # exponential, jitter at its maximum


def test_the_retries_are_capped() -> None:
    retrier, _ = _retrier(max_retries=2)
    attempts = _Attempts(*(TransientFailure(str(n)) for n in range(5)))

    with pytest.raises(TransientFailure) as exhausted:
        retrier.call(attempts)

    assert attempts.calls == 3
    assert exhausted.value.attempts == 3


@pytest.mark.parametrize("failure", [PermanentFailure("no"), UncertainOutcome("maybe")])
def test_only_transient_failures_are_ever_retried(failure: ToolFailure) -> None:
    retrier, slept = _retrier()
    attempts = _Attempts(failure)

    with pytest.raises(ToolFailure) as raised:
        retrier.call(attempts)

    assert attempts.calls == 1
    assert slept == []
    assert raised.value.attempts == 1
    assert raised.value.kind is not FailureKind.TRANSIENT


def test_a_retry_that_does_not_fit_in_the_remaining_time_is_not_made() -> None:
    retrier, slept = _retrier(remaining_ms=100)  # the first wait alone is 100 ms
    attempts = _Attempts(TransientFailure("slow"))

    with pytest.raises(TransientFailure):
        retrier.call(attempts)

    assert attempts.calls == 1
    assert slept == []


def test_zero_retries_means_one_attempt() -> None:
    retrier, _ = _retrier(max_retries=0)
    attempts = _Attempts(TransientFailure("t"))

    with pytest.raises(TransientFailure):
        retrier.call(attempts)

    assert attempts.calls == 1


def test_the_waits_are_bounded_and_the_jitter_is_reproducible_per_run() -> None:
    retrier = Retrier(
        max_retries=9,
        remaining_ms=lambda: 10**9,
        sleep=lambda seconds: None,
        jitter=jitter_for("run-1"),
        on_retry=lambda n, f: None,
    )
    waits = [retrier.backoff_ms(n) for n in range(1, 10)]

    assert all(0 < wait <= MAX_BACKOFF_MS for wait in waits)
    assert waits[-1] >= MAX_BACKOFF_MS // 2  # it grows until it reaches the cap
    first, second, other = jitter_for("run-1"), jitter_for("run-1"), jitter_for("run-2")
    assert [first() for _ in range(3)] == [second() for _ in range(3)]
    assert jitter_for("run-1")() != other()
