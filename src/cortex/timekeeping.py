"""The run's sense of time: a monotonic reading and a way to wait (brief section 2).

Injected everywhere time matters, so a test or an evaluation can make time pass without sleeping,
and a change of the wall clock cannot cut a run short.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


class Ticker(Protocol):
    """The run's sense of time: a monotonic reading, and a way to wait."""

    def seconds(self) -> float:
        """A monotonic reading, in seconds. Only differences between readings mean anything."""
        ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class SystemTicker:
    def seconds(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


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

    def sleep(self, seconds: float) -> None:
        """Waiting moves the clock and nothing else: no real time passes."""
        self.advance(seconds)
