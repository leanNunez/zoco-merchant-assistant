"""How much a run may still do: steps and total time (brief section 2).

A step is one model call. Time is measured with an injected monotonic ticker, so a test or an
evaluation can make time pass without sleeping, and a wall-clock change cannot cut a run short.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from cortex.config import RunLimits


class Ticker(Protocol):
    def seconds(self) -> float:
        """A monotonic reading, in seconds. Only differences between readings mean anything."""
        ...


@dataclass(frozen=True)
class SystemTicker:
    def seconds(self) -> float:
        return time.monotonic()


@dataclass
class ManualTicker:
    """Time that moves only when told to. Used by tests and by the evaluation's simulated faults."""

    now: float = 0.0

    def seconds(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("time does not go backwards")
        self.now += seconds


@dataclass
class Budget:
    """The remaining allowance of one run. Created per run, never shared."""

    limits: RunLimits
    ticker: Ticker
    steps_taken: int = 0
    _started: float = field(init=False)

    def __post_init__(self) -> None:
        self._started = self.ticker.seconds()

    def elapsed_ms(self) -> int:
        return int((self.ticker.seconds() - self._started) * 1000)

    def remaining_ms(self) -> int:
        return max(0, self.limits.total_timeout_ms - self.elapsed_ms())

    def out_of_time(self) -> bool:
        return self.remaining_ms() == 0

    def out_of_steps(self) -> bool:
        return self.steps_taken >= self.limits.max_steps

    def take_step(self) -> int:
        if self.out_of_steps():
            raise RuntimeError("no step left: check out_of_steps() first")
        self.steps_taken += 1
        return self.steps_taken

    def attempt_timeout_ms(self) -> int:
        """Time one tool attempt may take: its own limit, never more than what the run has left."""
        return min(self.limits.tool_timeout_ms, self.remaining_ms())
