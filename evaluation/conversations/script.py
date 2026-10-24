"""The toolkit scripted models are written with: what the model receives, and its replies.

A scripted model is a POLICY: a function from what the model would receive (the messages and the
offered tools) to one reply. It reacts to the transcript instead of replaying a fixed list of
steps, so the same script works whatever number of calls the agent makes, in the real agent and in
the unsafe reference agent alike. Tool call ids are numbered per conversation, never from module
state, so two runs in one process produce identical transcripts.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeAlias

from merchant_assistant.agent.domain.messages import Message, ModelReply, Role, ToolCall

CallSpec: TypeAlias = tuple[str, Mapping[str, object] | str]


@dataclass(frozen=True)
class View:
    """One model call as the script sees it."""

    messages: tuple[Message, ...]
    tools: tuple[str, ...]
    next_id: Callable[[], str]

    @property
    def system_prompt(self) -> str:
        for message in self.messages:
            if message.role is Role.SYSTEM:
                return message.content or ""
        return ""

    @property
    def user_texts(self) -> tuple[str, ...]:
        return tuple(m.content or "" for m in self.messages if m.role is Role.USER)

    @property
    def last_user(self) -> str:
        texts = self.user_texts
        return texts[-1] if texts else ""

    def results(self) -> tuple[dict[str, object], ...]:
        """The tool payloads the agent returned after the latest user message, in order."""
        start = 0
        for index, message in enumerate(self.messages):
            if message.role is Role.USER:
                start = index
        payloads: list[dict[str, object]] = []
        for message in self.messages[start:]:
            if message.role is Role.TOOL and message.content:
                try:
                    payload = json.loads(message.content)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    payloads.append(payload)
        return tuple(payloads)

    def result_of(self, tool: str) -> dict[str, object] | None:
        """The latest payload of `tool` in this turn, or None."""
        found = [payload for payload in self.results() if payload.get("tool") == tool]
        return found[-1] if found else None

    def offered(self, name: str) -> bool:
        return name in self.tools

    def call(self, *calls: CallSpec, text: str | None = None) -> ModelReply:
        built = tuple(
            ToolCall(
                self.next_id(),
                name,
                arguments
                if isinstance(arguments, str)
                else json.dumps(arguments, ensure_ascii=False),
            )
            for name, arguments in calls
        )
        return ModelReply(text=text, tool_calls=built)

    def say(self, text: str) -> ModelReply:
        return ModelReply(text=text, tool_calls=())


Policy: TypeAlias = Callable[[View], ModelReply]
ScriptFactory: TypeAlias = Callable[[], Policy]


def fold(text: str) -> str:
    """NFKC, accents removed, casefolded: how scripts and checks compare words."""
    decomposed = unicodedata.normalize("NFD", unicodedata.normalize("NFKC", text))
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").casefold()


def by_user_text(*rules: tuple[str, Policy], default: Policy) -> Policy:
    """The policy of the first rule whose pattern matches the latest user message.

    Patterns are searched in the folded text (no accents, lower case), so they are written
    folded too: `"cambia el motivo"` matches "cambiá el motivo".
    """
    compiled = tuple((re.compile(pattern), policy) for pattern, policy in rules)

    def policy(view: View) -> ModelReply:
        text = fold(view.last_user)
        for pattern, chosen in compiled:
            if pattern.search(text):
                return chosen(view)
        return default(view)

    return policy


def passages(payload: Mapping[str, object] | None) -> list[dict[str, object]]:
    """The passages of a `buscar_documentacion` payload (empty for anything else)."""
    if payload is None:
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    found = data.get("passages")
    if not isinstance(found, list):
        return []
    return [passage for passage in found if isinstance(passage, dict)]


def data_of(payload: Mapping[str, object] | None) -> dict[str, object]:
    if payload is None:
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}
