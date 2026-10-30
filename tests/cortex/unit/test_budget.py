from dataclasses import replace

import pytest

from cortex.config import RunLimits
from cortex.runner.budget import Budget
from cortex.timekeeping import ManualTicker

LIMITS = RunLimits(
    max_steps=2,
    total_timeout_ms=1000,
    tool_timeout_ms=400,
    max_read_retries=2,
    context_window_messages=12,
)


def test_steps_run_out_after_the_limit() -> None:
    budget = Budget(LIMITS, ManualTicker())

    budget.take_step()
    budget.take_step()

    assert budget.out_of_steps()
    with pytest.raises(RuntimeError):
        budget.take_step()


def test_time_is_measured_from_the_start_of_the_run() -> None:
    ticker = ManualTicker(now=50.0)
    budget = Budget(LIMITS, ticker)

    ticker.advance(0.7)

    assert budget.elapsed_ms() == 700
    assert budget.remaining_ms() == 300
    assert not budget.out_of_time()


def test_a_tool_attempt_never_gets_more_time_than_the_run_has_left() -> None:
    ticker = ManualTicker()
    budget = Budget(LIMITS, ticker)

    assert budget.attempt_timeout_ms() == 400
    ticker.advance(0.9)
    assert budget.attempt_timeout_ms() == 100
    ticker.advance(0.2)
    assert budget.out_of_time()
    assert budget.attempt_timeout_ms() == 0


def test_a_tool_limit_above_the_total_is_clamped_to_what_the_run_has_left() -> None:
    ticker = ManualTicker()
    budget = Budget(replace(LIMITS, total_timeout_ms=1000, tool_timeout_ms=3000), ticker)

    assert budget.attempt_timeout_ms() == 1000
    ticker.advance(0.25)
    assert budget.attempt_timeout_ms() == 750
