"""A time limit for every tool attempt (brief section 2).

An attempt gets the time its own limit allows, never more than the run has left. When it takes
longer it is abandoned: a read is then a TRANSIENT failure (nothing changed, trying again may
work) and a write is an UNCERTAIN outcome (the request may have landed before the answer was
lost). Python cannot kill a running thread, so an abandoned call may still finish in the
background; that is exactly why a write is never assumed to have failed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from pydantic import BaseModel

from cortex.tools.failures import ToolFailure, TransientFailure, UncertainOutcome
from cortex.tools.gateway import ToolAdapter, WriteAdapter

T = TypeVar("T")


class TimeLimited:
    """A read adapter that gives up after the time the run still allows."""

    def __init__(
        self, adapter: ToolAdapter, executor: ThreadPoolExecutor, timeout_ms: Callable[[], int]
    ) -> None:
        self.tenant_id = adapter.tenant_id
        self._adapter = adapter
        self._executor = executor
        self._timeout_ms = timeout_ms

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        return self._limited(lambda: self._adapter(arguments, conversation_id=conversation_id))

    def _limited(self, call: Callable[[], T]) -> T:
        allowed_ms = self._timeout_ms()
        if allowed_ms <= 0:
            raise self._on_timeout()
        future = self._executor.submit(call)
        try:
            return future.result(timeout=allowed_ms / 1000)
        except TimeoutError:  # the wait ran out, or the adapter itself timed out
            future.cancel()
            raise self._on_timeout() from None

    def _on_timeout(self) -> ToolFailure:
        return TransientFailure("the tool did not answer in time")


class TimeLimitedWrite(TimeLimited):
    """A write adapter: a timeout leaves the outcome unknown, and the lookup is limited too."""

    def __init__(
        self, adapter: WriteAdapter, executor: ThreadPoolExecutor, timeout_ms: Callable[[], int]
    ) -> None:
        super().__init__(adapter, executor, timeout_ms)
        self._writer = adapter

    def lookup(self, operation_id: str) -> Mapping[str, object] | None:
        return self._limited(lambda: self._writer.lookup(operation_id))

    def _on_timeout(self) -> ToolFailure:
        return UncertainOutcome("the answer did not arrive in time")


def time_limited(
    adapter: ToolAdapter, executor: ThreadPoolExecutor, timeout_ms: Callable[[], int]
) -> ToolAdapter:
    if isinstance(adapter, WriteAdapter):
        return TimeLimitedWrite(adapter, executor, timeout_ms)
    return TimeLimited(adapter, executor, timeout_ms)
