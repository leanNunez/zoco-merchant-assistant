"""The audit trail: an append-only record of every attempt to change who is in control.

The brief requires the previous state, the new state, the event, the actor and the date for
each transition. Rejected attempts are recorded too: a refused takeover or a refused
unblock is exactly what an audit asks about.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from merchant_assistant.control.states import Actor, Event, State

INITIAL_STATE = State.AUTO


class AttemptOutcome(StrEnum):
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class TransitionRecord:
    previous_state: State
    new_state: State
    event: Event
    actor: Actor
    at: datetime
    outcome: AttemptOutcome
    # The error code of a rejected attempt; None when the transition was applied.
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.at.utcoffset() is None:
            raise ValueError("a record needs a timezone-aware datetime")
        if self.outcome is AttemptOutcome.APPLIED and self.reason is not None:
            raise ValueError("an applied record has no rejection reason")
        if self.outcome is AttemptOutcome.REJECTED and not self.reason:
            raise ValueError("a rejected record must carry the code of its rejection")


class TransitionLog:
    """Append-only. There is no way to remove or replace an entry."""

    def __init__(self) -> None:
        self._entries: list[TransitionRecord] = []
        self._state = INITIAL_STATE

    @property
    def entries(self) -> tuple[TransitionRecord, ...]:
        return tuple(self._entries)

    @property
    def state(self) -> State:
        """The state after the last applied record (AUTO for an empty log)."""
        return self._state

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[TransitionRecord]:
        return iter(self.entries)

    def append(self, record: TransitionRecord) -> None:
        """Add a record if it continues the chain; raise ValueError if it does not."""
        if record.previous_state != self._state:
            raise ValueError(
                f"record starts from {record.previous_state} but the conversation is {self._state}"
            )
        if record.outcome is AttemptOutcome.REJECTED and record.new_state != record.previous_state:
            raise ValueError("a rejected attempt cannot change the state")
        self._entries.append(record)
        if record.outcome is AttemptOutcome.APPLIED:
            self._state = record.new_state
