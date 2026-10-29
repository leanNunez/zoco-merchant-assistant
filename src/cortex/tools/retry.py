"""Bounded retries of reads (brief section 5). Pure: it waits through injected functions.

Only a TRANSIENT failure is retried, at most `max_retries` times, and only if the wait still
fits in the time the run has left. A PERMANENT failure never helps to repeat, and an UNCERTAIN
one (a write that may have landed) is never repeated blindly: it is reconciled by its id. Writes
do not go through here at all.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from cortex.tools.failures import FailureKind, ToolFailure

T = TypeVar("T")

BASE_BACKOFF_MS = 100
MAX_BACKOFF_MS = 1000


def jitter_for(run_id: str) -> Callable[[], float]:
    """A reproducible source of jitter in [0, 1), private to one run (never shared)."""
    seed = int.from_bytes(hashlib.sha256(run_id.encode("utf-8")).digest()[:8], "big")
    return random.Random(seed).random  # noqa: S311 - spreading retries, not a secret


@dataclass(frozen=True, slots=True)
class Retrier:
    max_retries: int
    remaining_ms: Callable[[], int]
    sleep: Callable[[float], None]
    jitter: Callable[[], float]
    on_retry: Callable[[int, ToolFailure], None]

    def backoff_ms(self, retry_number: int) -> int:
        """Exponential, capped, between half and all of the ceiling so retries do not align."""
        ceiling = min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** (retry_number - 1))
        return int(ceiling * (0.5 + self.jitter() / 2))

    def call(self, attempt: Callable[[], T]) -> T:
        retries = 0
        while True:
            try:
                return attempt()
            except ToolFailure as failure:
                failure.attempts = retries + 1
                if failure.kind is not FailureKind.TRANSIENT or retries >= self.max_retries:
                    raise
                wait = self.backoff_ms(retries + 1)
                if self.remaining_ms() <= wait:
                    raise  # a retry that does not fit in the run's time is not made
                retries += 1
                self.on_retry(retries, failure)
                self.sleep(wait / 1000)
