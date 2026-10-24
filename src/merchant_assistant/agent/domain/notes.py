"""Platform notes: what the code tells the model when it withdrew a complaint proposal.

The confirmation gate drops a pending complaint when the customer answers anything but a plain
yes, and a change of who controls the conversation drops it too. Both are correct and safe, but
the model only sees the history: without a word about it, it keeps believing the proposal is
alive and asks the customer to "reply yes", which finds nothing pending. A note is a `SYSTEM`
message written HERE, by the code, appended to the stored history right where the withdrawal
happened. It carries no text of the customer or of the model (the three texts below are the
only ones that exist), so it cannot be used to smuggle instructions.

A note is for the model only:

- it is never an outbound message, a draft or part of a console view;
- it is not a user message, so no grounding, gate or signal logic reads it;
- it is not a source of complaint numbers, documents or amounts for the output checks;
- the model's answers are checked against it like against the system prompt, so a model that
  repeats one to the customer is withheld.

`add_note` never writes the same note twice in a row, so handling one withdrawal twice leaves
one note.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from merchant_assistant.agent.domain.messages import Message, Role

if TYPE_CHECKING:  # the state imports nothing from here; only the annotation needs it
    from merchant_assistant.agent.domain.state import ConversationState


class Withdrawal(StrEnum):
    INVALIDATED = "invalidated"  # the customer answered something that was not a plain yes
    CANCELLED = "cancelled"  # the customer said no
    DROPPED = "dropped"  # the conversation changed hands


_TEXTS = {
    Withdrawal.INVALIDATED: (
        "Platform note: the complaint proposal that was waiting for confirmation was withdrawn "
        "because the customer's last message was not a plain yes. Nothing was registered. If "
        "the customer still wants a complaint registered, call crear_reclamo again with the "
        "current details; do not ask them to reply yes in your own words."
    ),
    Withdrawal.CANCELLED: (
        "Platform note: the customer declined the complaint proposal. Nothing was registered. "
        "Do not propose it again unless the customer asks."
    ),
    Withdrawal.DROPPED: (
        "Platform note: the complaint proposal that was waiting for confirmation was dropped "
        "because the conversation changed hands. Nothing was registered. If the customer still "
        "wants it, call crear_reclamo again."
    ),
}
NOTE_TEXTS: frozenset[str] = frozenset(_TEXTS.values())


def note_text(withdrawal: Withdrawal) -> str:
    return _TEXTS[withdrawal]


def is_platform_note(message: Message) -> bool:
    return message.role is Role.SYSTEM and message.content in NOTE_TEXTS


def add_note(state: ConversationState, withdrawal: Withdrawal) -> ConversationState:
    """The state with the note at the end of its history (unless it is already the last)."""
    note = Message(Role.SYSTEM, note_text(withdrawal))
    if state.messages and state.messages[-1] == note:
        return state
    return state.with_messages(note)
