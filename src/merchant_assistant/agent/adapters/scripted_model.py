"""A chat model that plays a script: the way to test the agent with zero network and zero cost.

Each step is a `ModelReply`, or a callable that receives what the model would receive (the
messages and the offered tools) and returns one; a callable may also raise a `DomainError`,
which is how a provider failure is simulated. The script is STRICT: a call after the last step
raises `ScriptExhausted`, which is not a `DomainError`, so the turn cannot swallow it as a
provider failure. A loop that calls the model more often than the test planned fails the test.

Every call is recorded (the messages and the names of the tools offered) for assertions.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from merchant_assistant.agent.domain.messages import Message, ModelReply, ToolCall
from merchant_assistant.agent.domain.tools import ToolSpec
from merchant_assistant.agent.ports import Completion

SCRIPTED_MODEL_NAME = "scripted-model"
PROMPT_TOKENS = 100
COMPLETION_TOKENS = 20

ScriptStep = ModelReply | Callable[[Sequence[Message], Sequence[ToolSpec]], ModelReply]


class ScriptExhausted(AssertionError):  # noqa: N818 - named after the situation
    """The agent called the model more times than the script has steps."""


@dataclass(frozen=True, slots=True)
class ScriptedCall:
    messages: tuple[Message, ...]
    tool_names: tuple[str, ...]


class ScriptedChatModel:
    def __init__(self, steps: Iterable[ScriptStep], *, model: str = SCRIPTED_MODEL_NAME) -> None:
        self._steps: Iterator[ScriptStep] = iter(steps)
        self._model = model
        self.calls: list[ScriptedCall] = []

    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        self.calls.append(ScriptedCall(tuple(messages), tuple(spec.name for spec in tools)))
        try:
            step = next(self._steps)
        except StopIteration:
            raise ScriptExhausted(f"the script has no step for call {len(self.calls)}") from None
        reply = step if isinstance(step, ModelReply) else step(messages, tools)
        return Completion(reply, self._model, PROMPT_TOKENS, COMPLETION_TOKENS)

    @property
    def call_count(self) -> int:
        return len(self.calls)


_ids = itertools.count(1)


def tool_call(name: str, arguments: Mapping[str, object] | str, *, call_id: str = "") -> ToolCall:
    """A tool call; a mapping is dumped as JSON, a string is sent as is (for malformed ones)."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return ToolCall(call_id or f"call-{next(_ids)}", name, raw)


def calls_reply(*calls: ToolCall, text: str | None = None) -> ModelReply:
    return ModelReply(text=text, tool_calls=calls)


def text_reply(text: str) -> ModelReply:
    return ModelReply(text=text, tool_calls=())
