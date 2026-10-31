"""The simulated model: a deterministic script, no network and no keys (brief section 7).

It plays the kit's `model_script` one turn per call: `{"tool": name, "args": {...}}` or
`{"final": text}`. Each instance numbers its own call ids and records every prompt it received,
so tests can check what the model saw. Nothing is shared between instances.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from cortex.runner.model import (
    FinalAnswer,
    InvalidModelOutput,
    ModelTurn,
    Prompt,
    ScriptExhausted,
)
from merchant_assistant.agent.domain.messages import ToolCall


class ScriptedModel:
    def __init__(self, script: Sequence[Mapping[str, object]]) -> None:
        self._script = tuple(script)
        self._next = 0
        self.prompts: list[Prompt] = []

    def next_turn(self, prompt: Prompt, *, timeout_ms: int) -> ModelTurn:
        del timeout_ms  # a script answers instantly
        self.prompts.append(prompt)
        if self._next >= len(self._script):
            raise ScriptExhausted(f"the script has {len(self._script)} turns")
        self._next += 1
        # Parsed when played, like a provider's reply: a malformed step is a malformed answer.
        return _parse(self._next, self._script[self._next - 1])


def _parse(index: int, step: Mapping[str, object]) -> ModelTurn:
    keys = set(step)
    if keys == {"final"} and isinstance(step["final"], str):
        return FinalAnswer(step["final"])
    if keys == {"tool", "args"} and isinstance(step["tool"], str):
        # The arguments travel as raw JSON text, exactly like a real provider's tool call.
        return ToolCall(id=f"call-{index}", name=step["tool"], arguments=json.dumps(step["args"]))
    raise InvalidModelOutput(f"script step {index} has keys {sorted(keys)}")
