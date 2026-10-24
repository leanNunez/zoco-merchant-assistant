"""What the agent needs from the outside world: the model, the capabilities and the store.

Protocols and frozen value types only. The application depends on these; `bootstrap.py` maps
the real capabilities onto them, and the tests implement them in a few lines. Nothing here
knows a provider, a database or another package of the system.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Protocol

from merchant_assistant.agent.domain.messages import Message, ModelReply
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.agent.domain.tools import ToolSpec

LookupStatus = Literal["ok", "no_data"]


@dataclass(frozen=True, slots=True)
class Completion:
    """One model call. The served model and the usage are kept to report cost and to check
    that the model that answered is the model that was asked for."""

    reply: ModelReply
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


class ChatModel(Protocol):
    def complete(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Completion:
        """Raise a `DomainError` when the provider fails; never return a partial reply."""
        ...


@dataclass(frozen=True, slots=True)
class Passage:
    doc_id: str
    text: str
    valid_from: date
    valid_until: date


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    status: LookupStatus
    passages: tuple[Passage, ...]

    def __post_init__(self) -> None:
        if (self.status == "ok") != bool(self.passages):
            raise ValueError("an ok search has passages and a no_data search has none")


class KnowledgeSearch(Protocol):
    def search(self, query: str) -> SearchOutcome: ...


@dataclass(frozen=True, slots=True)
class DayFigures:
    tickets: int
    gross: Decimal
    refunds: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class SalesOutcome:
    """`no_data` means nothing was registered that day, which is not a day of zero sales."""

    status: LookupStatus
    day: date
    figures: DayFigures | None

    def __post_init__(self) -> None:
        if (self.status == "ok") != (self.figures is not None):
            raise ValueError("an ok lookup has figures and a no_data lookup has none")


class SalesLookup(Protocol):
    def lookup(self, raw_date: str) -> SalesOutcome:
        """Raise the capability's `DomainError` for an invalid or future date."""
        ...


@dataclass(frozen=True, slots=True)
class Registration:
    complaint_id: str
    created: bool  # False: the same complaint was already stored, nothing new was written


class ComplaintRegistry(Protocol):
    def register(self, reason: str, description: str, conversation_id: str) -> Registration:
        """Idempotent per conversation and normalized text: a retry finds the stored one."""
        ...


class ConversationStore(Protocol):
    def load(self, conversation_id: str) -> ConversationState | None: ...

    def save(self, state: ConversationState, *, expected_version: int) -> None:
        """Store `state` only if the stored version is still `expected_version`.

        `state.version` must be `expected_version + 1`. Raises `ConcurrentUpdate` when
        someone else saved in between (a missing conversation has version 0).
        """
        ...
