"""Strict argument validation, reused from the first delivery's guard.

`authorize` refuses oversized payloads, anything that is not one JSON object, repeated keys and
anything the strict argument model rejects. It is the first delivery's code, unchanged; this
module only adapts the harness's tool spec to the shape that guard expects.
"""

from __future__ import annotations

from pydantic import BaseModel

from cortex.tools.catalog import ToolKind, ToolSpec
from merchant_assistant.agent.domain.guards import authorize
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.agent.domain.tools import ToolKind as LegacyKind
from merchant_assistant.agent.domain.tools import ToolSpec as LegacySpec


def validate_arguments(spec: ToolSpec, call: ToolCall) -> BaseModel:
    """The validated arguments, or `InvalidToolArguments` naming the fields (never the values)."""
    legacy = LegacySpec(
        name=spec.name,
        description=spec.description,
        args_model=spec.args_model,
        kind=LegacyKind.EFFECT if spec.kind is ToolKind.WRITE else LegacyKind.READ,
    )
    return authorize(call, {spec.name: legacy}).arguments
