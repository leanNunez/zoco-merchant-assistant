"""The stored conversation: immutable, with the control log as the source of truth."""

from dataclasses import replace
from datetime import UTC, datetime, time

import pytest

from merchant_assistant.agent.domain.errors import InvalidConversationState
from merchant_assistant.agent.domain.messages import Message, Role
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.control import (
    Actor,
    AttemptOutcome,
    BusinessHours,
    Event,
    State,
    TransitionRecord,
    UnauthorizedActor,
)

AT = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)
HOURS = BusinessHours(tz=UTC, opens=time(9), closes=time(18), weekdays=frozenset(range(7)))


def test_a_new_conversation_starts_in_auto_with_nothing_in_it() -> None:
    state = ConversationState("conv-1")

    assert (state.version, state.turn, state.messages, state.abuse_strikes) == (0, 0, (), 0)
    assert state.control_state is State.AUTO
    assert state.control_version == 0
    assert state.last_registered_id is None


def test_the_control_version_counts_every_attempt_applied_or_rejected() -> None:
    state = ConversationState("conv-1")
    control = state.control()
    control.apply(Event.OPERATOR_TAKEOVER, Actor.OPERATOR, at=AT, hours=HOURS)
    with pytest.raises(UnauthorizedActor):
        control.apply(Event.OPERATOR_BLOCK, Actor.CUSTOMER, at=AT, hours=HOURS)

    moved = state.with_control(control)

    assert moved.control_state is State.HUMAN
    assert moved.control_version == 2
    assert state.control_version == 0  # the original is untouched


def test_the_aggregate_is_rebuilt_from_a_copy_so_it_cannot_mutate_the_state() -> None:
    state = ConversationState("conv-1")

    state.control().apply(Event.OPERATOR_TAKEOVER, Actor.OPERATOR, at=AT, hours=HOURS)

    assert state.control_state is State.AUTO
    assert state.control_log == ()


def test_messages_are_appended_into_a_new_state() -> None:
    state = ConversationState("conv-1")

    later = state.with_messages(Message(Role.USER, "hola"), Message(Role.ASSISTANT, "¡Hola!"))

    assert [m.content for m in later.messages] == ["hola", "¡Hola!"]
    assert state.messages == ()


def test_a_broken_control_log_cannot_be_stored() -> None:
    orphan = TransitionRecord(
        previous_state=State.HUMAN,
        new_state=State.AUTO,
        event=Event.OPERATOR_RELEASE,
        actor=Actor.OPERATOR,
        at=AT,
        outcome=AttemptOutcome.APPLIED,
    )

    with pytest.raises(InvalidConversationState):
        ConversationState("conv-1", control_log=(orphan,))


@pytest.mark.parametrize(
    "change",
    [
        {"conversation_id": "  "},
        {"version": -1},
        {"turn": -1},
        {"abuse_strikes": -1},
        {"turn": True},
    ],
)
def test_counters_and_the_identifier_are_validated(change: dict[str, object]) -> None:
    with pytest.raises(InvalidConversationState):
        replace(ConversationState("conv-1"), **change)  # type: ignore[arg-type]
