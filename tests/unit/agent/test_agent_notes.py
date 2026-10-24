"""Platform notes: the three sentences the code may add to the history, and nothing else."""

import pytest

from merchant_assistant.agent.domain.messages import Message, Role
from merchant_assistant.agent.domain.notes import (
    NOTE_TEXTS,
    Withdrawal,
    add_note,
    is_platform_note,
    note_text,
)
from merchant_assistant.agent.domain.signals import abuse_score, injection_markers, wants_human
from merchant_assistant.agent.domain.state import ConversationState


def test_the_three_notes_say_exactly_this() -> None:
    assert note_text(Withdrawal.INVALIDATED) == (
        "Platform note: the complaint proposal that was waiting for confirmation was withdrawn "
        "because the customer's last message was not a plain yes. Nothing was registered. If "
        "the customer still wants a complaint registered, call crear_reclamo again with the "
        "current details; do not ask them to reply yes in your own words."
    )
    assert note_text(Withdrawal.CANCELLED) == (
        "Platform note: the customer declined the complaint proposal. Nothing was registered. "
        "Do not propose it again unless the customer asks."
    )
    assert note_text(Withdrawal.DROPPED) == (
        "Platform note: the complaint proposal that was waiting for confirmation was dropped "
        "because the conversation changed hands. Nothing was registered. If the customer still "
        "wants it, call crear_reclamo again."
    )
    assert len(NOTE_TEXTS) == 3


def test_a_note_is_a_system_message_added_at_the_end_of_the_history() -> None:
    state = ConversationState("c").with_messages(Message(Role.USER, "hola"))

    noted = add_note(state, Withdrawal.CANCELLED)

    assert noted.messages == (
        Message(Role.USER, "hola"),
        Message(Role.SYSTEM, note_text(Withdrawal.CANCELLED)),
    )
    assert state.messages == (Message(Role.USER, "hola"),)


def test_the_same_note_is_not_written_twice_in_a_row() -> None:
    once = add_note(ConversationState("c"), Withdrawal.INVALIDATED)

    assert add_note(once, Withdrawal.INVALIDATED) == once


def test_a_note_after_other_messages_is_written_again() -> None:
    first = add_note(ConversationState("c"), Withdrawal.INVALIDATED)
    later = add_note(first.with_messages(Message(Role.USER, "otra vez")), Withdrawal.INVALIDATED)

    assert [m.role for m in later.messages] == [Role.SYSTEM, Role.USER, Role.SYSTEM]


@pytest.mark.parametrize("withdrawal", list(Withdrawal))
def test_only_the_code_written_system_messages_are_notes(withdrawal: Withdrawal) -> None:
    text = note_text(withdrawal)

    assert is_platform_note(Message(Role.SYSTEM, text))
    assert not is_platform_note(Message(Role.USER, text))  # the customer cannot write one
    assert not is_platform_note(Message(Role.ASSISTANT, text))
    assert not is_platform_note(Message(Role.SYSTEM, text + " "))
    assert not is_platform_note(Message(Role.SYSTEM, "You are an assistant."))


@pytest.mark.parametrize("withdrawal", list(Withdrawal))
def test_a_note_trips_none_of_the_signal_detectors(
    withdrawal: Withdrawal,
) -> None:
    text = note_text(withdrawal)

    assert injection_markers(text) == 0
    assert abuse_score(text) == 0
    assert not wants_human(text)
