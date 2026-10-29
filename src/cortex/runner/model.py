"""The model port (brief section 2): the runner talks to any model through this interface.

A model turn is either a tool request or a final answer, never both. The request reuses the
first delivery's `ToolCall`, whose arguments are untrusted raw JSON text: the code verifies them,
the model only proposes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol

from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.shared.errors import DomainError

Role = Literal["user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class PromptMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class Prompt:
    """What the model sees: a bounded history plus the structured state, never summarized."""

    messages: tuple[PromptMessage, ...]
    state: Mapping[str, object]
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinalAnswer:
    text: str


ModelTurn = ToolCall | FinalAnswer


class ModelError(DomainError):
    """Base of the failures of the model itself (not of a tool)."""


class ModelUnavailable(ModelError):  # noqa: N818 - named after the situation
    code: ClassVar[str] = "model_unavailable"
    user_message: ClassVar[str] = "The model is not available right now."


class InvalidModelOutput(ModelError):  # noqa: N818 - named after the situation
    code: ClassVar[str] = "invalid_model_output"
    user_message: ClassVar[str] = "The model answered something the harness cannot use."


class ScriptExhausted(ModelError):  # noqa: N818 - named after the situation
    code: ClassVar[str] = "script_exhausted"
    user_message: ClassVar[str] = "The simulated model has no more scripted turns."


class ModelPort(Protocol):
    def next_turn(self, prompt: Prompt, *, timeout_ms: int) -> ModelTurn: ...
