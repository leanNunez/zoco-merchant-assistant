"""The in-memory store refuses a save that would overwrite someone else's write."""

from dataclasses import replace

import pytest

from merchant_assistant.agent.adapters.memory_store import InMemoryConversationStore
from merchant_assistant.agent.domain.errors import ConcurrentUpdate
from merchant_assistant.agent.domain.state import ConversationState


def test_a_missing_conversation_loads_as_none_and_is_saved_from_version_zero() -> None:
    store = InMemoryConversationStore()

    assert store.load("conv-1") is None
    store.save(ConversationState("conv-1", version=1), expected_version=0)

    loaded = store.load("conv-1")
    assert loaded is not None
    assert loaded.version == 1


def test_a_save_against_a_stale_version_is_refused_and_changes_nothing() -> None:
    store = InMemoryConversationStore()
    first = ConversationState("conv-1", version=1)
    store.save(first, expected_version=0)
    store.save(replace(first, version=2, turn=5), expected_version=1)

    with pytest.raises(ConcurrentUpdate):
        store.save(replace(first, version=2, turn=9), expected_version=1)

    loaded = store.load("conv-1")
    assert loaded is not None
    assert (loaded.version, loaded.turn) == (2, 5)


def test_a_save_must_carry_the_next_version() -> None:
    store = InMemoryConversationStore()

    with pytest.raises(ValueError, match="next version"):
        store.save(ConversationState("conv-1", version=3), expected_version=0)


def test_conversations_are_independent() -> None:
    store = InMemoryConversationStore()
    store.save(ConversationState("conv-1", version=1), expected_version=0)

    store.save(ConversationState("conv-2", version=1), expected_version=0)

    assert store.load("conv-1") != store.load("conv-2")
