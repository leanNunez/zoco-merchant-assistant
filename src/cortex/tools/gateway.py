"""Runs an approved tool call through its adapter and wraps the result (brief sections 3 and 5).

The adapters are bound to one company before the run starts, so a call can only ever reach that
company's data. Results go back to the model wrapped as untrusted data: text inside a document
or a tool result is information, never an instruction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from cortex.identity import AuthContext
from cortex.tools.failures import FailureKind, ToolFailure
from cortex.tools.policy import Approved
from cortex.tools.retry import Retrier
from merchant_assistant.shared.errors import DomainError


class ToolAdapter(Protocol):
    """The code that reaches one tool's data. It is bound to one company and says which."""

    @property
    def tenant_id(self) -> str: ...

    def __call__(self, arguments: BaseModel, *, conversation_id: str) -> Mapping[str, object]: ...


@runtime_checkable
class WriteAdapter(ToolAdapter, Protocol):
    """A tool with an effect. It must be able to tell whether a command was applied."""

    def lookup(self, operation_id: str) -> Mapping[str, object] | None: ...


# Builds the adapters of one company. Called once per run with the authenticated context.
ToolBinder = Callable[[AuthContext], Mapping[str, ToolAdapter]]


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str
    ok: bool
    # Whose data this is. Kept out of the envelope: it is evidence for the trace, not for the model.
    source_tenant: str
    data: Mapping[str, object] | None = None
    error: Mapping[str, str] | None = None

    def envelope(self) -> dict[str, object]:
        return {
            "untrusted_data": True,
            "tool": self.tool,
            "status": "ok" if self.ok else "error",
            "data": dict(self.data) if self.data is not None else None,
            "error": dict(self.error) if self.error is not None else None,
        }


def execute(
    approved: Approved,
    adapters: Mapping[str, ToolAdapter],
    *,
    conversation_id: str,
    retrier: Retrier | None = None,
) -> ToolResult:
    """Run a READ tool, retrying transient failures. Writes go through `cortex.tools.writes`."""
    name = approved.spec.name
    adapter = adapters[name]

    def attempt() -> Mapping[str, object]:
        return adapter(approved.arguments, conversation_id=conversation_id)

    try:
        data = retrier.call(attempt) if retrier is not None else attempt()
    except DomainError as failure:
        return ToolResult(name, ok=False, source_tenant=adapter.tenant_id, error=_view(failure))
    return ToolResult(name, ok=True, source_tenant=adapter.tenant_id, data=data)


def _view(failure: DomainError) -> dict[str, str]:
    """Only the public view crosses: a stable code and a safe message, never internals."""
    view = failure.public()
    if isinstance(failure, ToolFailure):
        view["kind"] = failure.kind.value
        view["retryable"] = str(failure.kind is FailureKind.TRANSIENT).lower()
        view["attempts"] = str(failure.attempts)
    return view
