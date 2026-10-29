"""The contract's output, built from evidence: the database, the trace and the call counters.

Every observable the kit's scenarios assert on is emitted for every scenario, at the top level
and inside `effects` and `state` where they belong. `None` means the run did not measure it.
Nothing here reads a scenario's `expected` block; the harness never sees it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from cortex.identity import AuthContext
from cortex.kit_adapter.audit import Audit
from cortex.memory.tenant_store import TenantStore
from cortex.runner.agent_runner import RunResult
from cortex.tools.gateway import ToolAdapter, ToolBinder, WriteAdapter


class CallCounter:
    """Counts the calls that reach each tool adapter: the boundary with the outside world."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def wrap(self, binder: ToolBinder) -> ToolBinder:
        def bind(auth: AuthContext) -> Mapping[str, ToolAdapter]:
            adapters = binder(auth)
            return {name: self._counting(name, adapter) for name, adapter in adapters.items()}

        return bind

    def _counting(self, name: str, adapter: ToolAdapter) -> ToolAdapter:
        if isinstance(adapter, WriteAdapter):
            return _CountedWrite(self.calls, name, adapter)
        return _Counted(self.calls, name, adapter)


class _Counted:
    def __init__(self, calls: dict[str, int], name: str, adapter: ToolAdapter) -> None:
        self._calls, self._name, self._adapter = calls, name, adapter
        self.tenant_id = adapter.tenant_id

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]:
        self._calls[self._name] = self._calls.get(self._name, 0) + 1
        return self._adapter(arguments, conversation_id=conversation_id)


class _CountedWrite(_Counted):
    """A write adapter keeps its lookup; a lookup is an external call too."""

    def __init__(self, calls: dict[str, int], name: str, adapter: WriteAdapter) -> None:
        super().__init__(calls, name, adapter)
        self._writer = adapter

    def lookup(self, operation_id: str) -> Mapping[str, object] | None:
        self._calls[self._name] = self._calls.get(self._name, 0) + 1
        return self._writer.lookup(operation_id)


def build_output(
    result: RunResult,
    *,
    store: TenantStore,
    counter: CallCounter,
    audit: Audit,
    contexts_separated: bool | None,
) -> dict[str, object]:
    executed = _tools_of(result, "tool_executed")
    rejected = _tools_of(result, "tool_rejected")
    sent = store.sent_count(result.run_id)
    conversation = store.conversation(result.conversation_id)
    claims, distinct_claims = audit.claims(result.tenant_id)
    effects = {"claims_created": claims, "customer_messages_sent": sent}
    state = {
        "conversation_mode": conversation.mode.value if conversation else None,
        "control_version": conversation.control_version if conversation else None,
    }
    # Every tool result is tagged with the company its data came from.
    foreign_data = any(
        event.attributes.get("data_tenant") not in (None, result.tenant_id)
        for event in result.events
    )
    observables: dict[str, object] = {
        "executed_tools": executed,
        "rejected_tools": rejected,
        "external_calls_to_rejected_tool": sum(
            counter.calls.get(name, 0) for name in set(rejected)
        ),
        "retry_count": sum(1 for event in result.events if event.type == "tool_retry"),
        "max_steps": result.max_steps,
        "customer_messages_sent": sent,
        "claims_created": claims,
        "unique_claims_created": distinct_claims,
        # Measured by later blocks of work: restart, takeover and follow-ups.
        "cross_tenant_data_exposed": foreign_data,
        "cross_tenant_effects": (
            audit.rows_written_outside(result.tenant_id, result.run_id)
            + audit.claims_outside(result.tenant_id)
        ),
        "contexts_separated": contexts_separated,
        "state_after_restart": None,
        "pending_action_retained": None,
        "delivered_ai_messages_after_takeover": None,
        "tool_effects_after_takeover": None,
        "decision": None,
    }
    return {
        "run_id": result.run_id,
        "tenant_id": result.tenant_id,
        "conversation_id": result.conversation_id,
        "finish_reason": result.finish_reason.value,
        "finish_detail": result.finish_detail.value,
        "step_count": result.step_count,
        "events": [event.as_dict() for event in result.events],
        "effects": effects,
        "state": state,
        **observables,
    }


def _tools_of(result: RunResult, event_type: str) -> list[str]:
    return [
        str(event.attributes.get("tool")) for event in result.events if event.type == event_type
    ]
