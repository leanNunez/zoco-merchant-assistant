"""Simulated tool failures, driven by the environment.

The brief asks the tools to fail on demand so the agent's behaviour under failure can be
tested. The decision to fail is made here, in one place, from a seeded generator: the same
seed and the same sequence of calls always produce the same failures, so a failing
conversation can be replayed exactly.

Pure stdlib. The adapters that wrap a real repository live next to each capability.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Self

from merchant_assistant.shared.errors import DomainError


class FaultMode(StrEnum):
    TIMEOUT = "timeout"  # the call fails before it does anything
    ERROR = "error"  # the service answers with an error before it does anything
    LOST_ACK = "lost_ack"  # the effect happens, but the acknowledgement never arrives


class ToolFailure(DomainError):  # noqa: N818 - named by the failure it models, not as an error
    """A tool failed for a reason outside the caller's control. Trying again may work."""

    retryable: ClassVar[bool] = True


class SimulatedTimeout(ToolFailure):
    code: ClassVar[str] = "tool_timeout"
    user_message: ClassVar[str] = "The service did not answer in time."


class SimulatedFailure(ToolFailure):
    code: ClassVar[str] = "tool_unavailable"
    user_message: ClassVar[str] = "The service is not available right now."


@dataclass(frozen=True)
class FaultConfig:
    rate: float
    seed: int
    modes: frozenset[FaultMode]

    def __post_init__(self) -> None:
        # Written as a positive range check so NaN, which compares false to everything, fails.
        if not 0.0 <= self.rate <= 1.0:
            raise ValueError(f"the failure rate must be between 0 and 1, got {self.rate}")
        if self.rate > 0 and not self.modes:
            raise ValueError("a failure rate above 0 needs at least one failure mode")

    @classmethod
    def off(cls) -> Self:
        return cls(rate=0.0, seed=0, modes=frozenset())


class FaultInjector:
    """Decides, call by call, whether a tool should fail and how.

    Not thread-safe: one injector per tool, used from one conversation thread.
    """

    def __init__(self, config: FaultConfig) -> None:
        self._rate = config.rate
        self._modes = config.modes
        # Reproducibility, not security: this generator is never used to protect anything.
        self._rng = random.Random(config.seed)  # noqa: S311

    @classmethod
    def disabled(cls) -> Self:
        return cls(FaultConfig.off())

    def next_fault(self, supports: frozenset[FaultMode]) -> FaultMode | None:
        """The fault to apply to this call, or None.

        Exactly one number is drawn on every call, even when the tool cannot fail in any
        way it supports. Drawing the same amount each time keeps the sequence the same no
        matter which tools are called in between.
        """
        draw = self._rng.random()
        if draw >= self._rate:
            return None
        usable = self._modes & supports
        if not usable:
            return None
        return self._rng.choice(sorted(usable))
