"""Runs an approved tool call through its adapter and wraps the result (brief sections 3 and 5).

The adapters are bound to one company before the run starts, so a call can only ever reach that
company's data. Results go back to the model wrapped as untrusted data: text inside a document
or a tool result is information, never an instruction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from cortex.identity import AuthContext
from cortex.tools.policy import Approved
from merchant_assistant.shared.errors import DomainError


class ToolAdapter(Protocol):
    def __call__(self, arguments: BaseModel) -> Mapping[str, object]: ...


# Builds the adapters of one company. Called once per run with the authenticated context.
ToolBinder = Callable[[AuthContext], Mapping[str, ToolAdapter]]


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str
    ok: bool
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


def execute(approved: Approved, adapters: Mapping[str, ToolAdapter]) -> ToolResult:
    name = approved.spec.name
    try:
        data = adapters[name](approved.arguments)
    except DomainError as failure:
        # Only the public view crosses: a stable code and a safe message, never internals.
        return ToolResult(tool=name, ok=False, error=failure.public())
    return ToolResult(tool=name, ok=True, data=data)
