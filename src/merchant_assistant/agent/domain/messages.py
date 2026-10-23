"""The conversation as the agent sees it: roles, messages and what the model replied.

These are provider-neutral. An adapter translates them to and from a provider's wire format;
nothing here knows any provider. A tool call keeps its arguments as the raw JSON text the
model produced, because validating them is the guards' job, not the parser's.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from merchant_assistant.agent.domain.errors import InvalidMessage


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call the model proposed. `arguments` is untrusted raw JSON text."""

    id: str
    name: str
    arguments: str

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise InvalidMessage("a tool call needs an id to pair it with its result")


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        match self.role:
            case Role.ASSISTANT:
                if self.tool_call_id is not None:
                    raise InvalidMessage("an assistant message does not answer a tool call")
                if self.content is None and not self.tool_calls:
                    raise InvalidMessage("an assistant message needs content or tool calls")
                _check_unique_ids(self.tool_calls)
            case Role.TOOL:
                if self.tool_calls:
                    raise InvalidMessage("only the assistant may carry tool calls")
                if self.tool_call_id is None or not self.tool_call_id.strip():
                    raise InvalidMessage("a tool message must name the call it answers")
                if self.content is None:
                    raise InvalidMessage("a tool message must carry the tool's result")
            case Role.SYSTEM | Role.USER:
                if self.tool_calls:
                    raise InvalidMessage("only the assistant may carry tool calls")
                if self.tool_call_id is not None:
                    raise InvalidMessage(f"a {self.role} message does not answer a tool call")
                if self.content is None:
                    raise InvalidMessage(f"a {self.role} message must carry content")


@dataclass(frozen=True, slots=True)
class ModelReply:
    """What one model call returned: text, tool calls, or both.

    Blank text counts as no text: a reply with neither is not a reply.
    """

    text: str | None
    tool_calls: tuple[ToolCall, ...]

    def __post_init__(self) -> None:
        if (self.text is None or not self.text.strip()) and not self.tool_calls:
            raise InvalidMessage("a model reply needs text or tool calls")
        _check_unique_ids(self.tool_calls)


def _check_unique_ids(calls: tuple[ToolCall, ...]) -> None:
    if len({call.id for call in calls}) != len(calls):
        raise InvalidMessage("two tool calls share an id, so their results could be swapped")
