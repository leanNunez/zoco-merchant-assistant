"""The aggregate: the state of one conversation, always in agreement with its log."""

from __future__ import annotations

from datetime import datetime

from merchant_assistant.control.hours import BusinessHours
from merchant_assistant.control.log import AttemptOutcome, TransitionLog, TransitionRecord
from merchant_assistant.control.machine import Outcome, transition
from merchant_assistant.control.states import Actor, Event, State
from merchant_assistant.shared.errors import DomainError


class ConversationControl:
    """Holds no state of its own: the state is read from the log, so they cannot disagree."""

    def __init__(self, log: TransitionLog | None = None) -> None:
        self._log = log if log is not None else TransitionLog()

    @property
    def state(self) -> State:
        return self._log.state

    @property
    def log(self) -> TransitionLog:
        return self._log

    def apply(self, event: Event, actor: Actor, *, at: datetime, hours: BusinessHours) -> Outcome:
        """Fire an event. Applied or refused, the attempt is logged; refusals are re-raised."""
        previous = self.state
        try:
            outcome = transition(previous, event, actor, hours_open=hours.is_open(at))
        except DomainError as refusal:
            self._log.append(
                TransitionRecord(
                    previous_state=previous,
                    new_state=previous,
                    event=event,
                    actor=actor,
                    at=at,
                    outcome=AttemptOutcome.REJECTED,
                    reason=refusal.code,
                )
            )
            raise
        self._log.append(
            TransitionRecord(
                previous_state=previous,
                new_state=outcome.new,
                event=event,
                actor=actor,
                at=at,
                outcome=AttemptOutcome.APPLIED,
            )
        )
        return outcome

    def sync_hours(self, *, at: datetime, hours: BusinessHours) -> Outcome | None:
        """Hand the conversation back to the agent when the human window has closed.

        Nothing fires when the window opens: open hours are a guard, not a state. But a
        conversation left with a human (or with drafts waiting for one) after closing time
        would stall, because nobody is there to answer. The application calls this at the
        start of every turn.
        """
        if hours.is_open(at) or self.state not in (State.SUGGEST, State.HUMAN):
            return None
        return self.apply(Event.OUT_OF_HOURS_STARTED, Actor.SYSTEM, at=at, hours=hours)
