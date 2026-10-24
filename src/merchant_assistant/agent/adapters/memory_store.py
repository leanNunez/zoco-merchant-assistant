"""Conversations in memory, with the same optimistic concurrency a real store must have."""

from __future__ import annotations

import threading

from merchant_assistant.agent.domain.errors import ConcurrentUpdate
from merchant_assistant.agent.domain.state import ConversationState


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._states: dict[str, ConversationState] = {}
        self._lock = threading.Lock()

    def load(self, conversation_id: str) -> ConversationState | None:
        with self._lock:
            return self._states.get(conversation_id)

    def save(self, state: ConversationState, *, expected_version: int) -> None:
        if state.version != expected_version + 1:
            raise ValueError("a saved state must carry the next version")
        with self._lock:
            stored = self._states.get(state.conversation_id)
            current = 0 if stored is None else stored.version
            if current != expected_version:
                raise ConcurrentUpdate(
                    f"expected version {expected_version}, the store has {current}"
                )
            self._states[state.conversation_id] = state
