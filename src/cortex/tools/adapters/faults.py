"""Simulated failures of the outside world, played from the kit's `faults` (brief section 7).

A plan lists, per tool, what each call does: `SUCCESS`, `TIMEOUT`, `INVALID_REQUEST`,
`COMMITTED_BUT_TIMEOUT` (the effect happened, the answer was lost) or `REPEAT_SAME_OPERATION_ID`
(the request reaches the system twice). Once a tool's list is used up,
its calls succeed. The plan belongs to one scenario's world and is never shared. A simulated
timeout costs the tool's full time limit on the run's clock, so the budget behaves as it would
with a real one without anybody waiting.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel

from cortex.identity import AuthContext
from cortex.timekeeping import Ticker
from cortex.tools.failures import PermanentFailure
from cortex.tools.gateway import ToolAdapter, ToolBinder, WriteAdapter


class FaultKind(StrEnum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    INVALID_REQUEST = "INVALID_REQUEST"
    COMMITTED_BUT_TIMEOUT = "COMMITTED_BUT_TIMEOUT"
    REPEAT_SAME_OPERATION_ID = "REPEAT_SAME_OPERATION_ID"


class FaultPlan:
    def __init__(
        self,
        sequences: Mapping[str, Sequence[FaultKind]],
        *,
        ticker: Ticker,
        timeout_cost_ms: int,
    ) -> None:
        self._remaining = {tool: list(kinds) for tool, kinds in sequences.items()}
        self._ticker = ticker
        self._timeout_cost_ms = timeout_cost_ms
        self._lock = threading.Lock()

    def next_for(self, tool: str) -> FaultKind:
        with self._lock:
            queue = self._remaining.get(tool)
            return queue.pop(0) if queue else FaultKind.SUCCESS

    def spend_timeout(self) -> None:
        self._ticker.sleep(self._timeout_cost_ms / 1000)

    def wrap(self, binder: ToolBinder) -> ToolBinder:
        def bind(auth: AuthContext) -> Mapping[str, ToolAdapter]:
            return {name: self._faulty(name, adapter) for name, adapter in binder(auth).items()}

        return bind

    def _faulty(self, name: str, adapter: ToolAdapter) -> ToolAdapter:
        if isinstance(adapter, WriteAdapter):
            return _FaultyWrite(self, name, adapter)
        return _Faulty(self, name, adapter)


class _Faulty:
    def __init__(self, plan: FaultPlan, name: str, adapter: ToolAdapter) -> None:
        self._plan, self._name, self._adapter = plan, name, adapter
        self.tenant_id = adapter.tenant_id

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        kind = self._plan.next_for(self._name)
        if kind is FaultKind.SUCCESS:
            return self._adapter(arguments, conversation_id=conversation_id)
        if kind is FaultKind.REPEAT_SAME_OPERATION_ID:
            # The external system receives the same request, and so the same operation_id, a
            # second time (a duplicated delivery). It performs the same idempotent write with the
            # same key: the claims store's UNIQUE key absorbs it and no second effect happens.
            self._adapter(arguments, conversation_id=conversation_id)
            return self._adapter(arguments, conversation_id=conversation_id)
        if kind is FaultKind.INVALID_REQUEST:
            raise PermanentFailure("simulated: the request was refused")
        if kind is FaultKind.COMMITTED_BUT_TIMEOUT:
            # The effect happens, then the answer is lost.
            self._adapter(arguments, conversation_id=conversation_id)
        self._plan.spend_timeout()
        raise TimeoutError("simulated timeout")


class _FaultyWrite(_Faulty):
    """The lookup of a write is never faulted: it is how an uncertain outcome gets resolved."""

    def __init__(self, plan: FaultPlan, name: str, adapter: WriteAdapter) -> None:
        super().__init__(plan, name, adapter)
        self._writer = adapter

    def lookup(self, operation_id: str) -> Mapping[str, object] | None:
        return self._writer.lookup(operation_id)
