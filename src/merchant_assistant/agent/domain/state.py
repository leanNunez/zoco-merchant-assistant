"""One conversation as it is stored between turns.

Immutable: every turn builds a new state from the previous one. The control log is the source
of truth for who controls the conversation: `ConversationControl` is rebuilt from it when
needed, so the two can never disagree, and the CONTROL VERSION the confirmation gate checks is
simply the number of records in it (applied or rejected attempts alike: any attempt to change
hands invalidates a half-confirmed action, which is the conservative reading).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from merchant_assistant.agent.domain.drafts import Draft, pending_drafts
from merchant_assistant.agent.domain.errors import InvalidConversationState
from merchant_assistant.agent.domain.messages import Message
from merchant_assistant.agent.domain.pending import PendingState
from merchant_assistant.control import (
    AttemptOutcome,
    ControlError,
    ConversationControl,
    State,
    TransitionLog,
    TransitionRecord,
    transition,
)


@dataclass(frozen=True, slots=True)
class OperatorNotice:
    """An entry of the operator inbox: something a person of the team must look at.

    `code` is stable (for example `abuse_detected`, from the control effect NOTIFY_OPERATOR);
    `detail` is short and code-written, never text the customer or the model typed.
    """

    at: datetime
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.at.utcoffset() is None:
            raise InvalidConversationState("an inbox entry needs a timezone-aware time")
        if not self.code.strip():
            raise InvalidConversationState("an inbox entry needs a code")


@dataclass(frozen=True, slots=True)
class ConversationState:
    conversation_id: str
    version: int = 0  # bumped by every successful save (optimistic concurrency)
    turn: int = 0  # how many user messages have been handled
    messages: tuple[Message, ...] = ()
    pending: PendingState = field(default_factory=PendingState)
    control_log: tuple[TransitionRecord, ...] = ()
    abuse_strikes: int = 0
    # Every draft ever made in SUGGEST, decided or not (the record of what was withheld).
    drafts: tuple[Draft, ...] = ()
    inbox: tuple[OperatorNotice, ...] = ()

    def __post_init__(self) -> None:
        if not self.conversation_id.strip():
            raise InvalidConversationState("a conversation needs an identifier")
        for name in ("version", "turn", "abuse_strikes"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise InvalidConversationState(f"{name} must be an integer >= 0")
        _rebuild(self.control_log)  # a broken chain must never be stored or loaded
        ids = [draft.id for draft in self.drafts]
        if len(set(ids)) != len(ids):
            raise InvalidConversationState("two drafts share an identifier")

    @property
    def control_version(self) -> int:
        return len(self.control_log)

    @property
    def control_state(self) -> State:
        return _rebuild(self.control_log).state

    @property
    def last_registered_id(self) -> str | None:
        last = self.pending.last_registered
        return None if last is None else last.complaint_id

    @property
    def pending_drafts(self) -> tuple[Draft, ...]:
        return pending_drafts(self.drafts)

    def control(self) -> ConversationControl:
        """A fresh aggregate over a copy of the log; apply events to it, then `with_control`."""
        return ConversationControl(_rebuild(self.control_log))

    def with_control(self, control: ConversationControl) -> ConversationState:
        return replace(self, control_log=control.log.entries)

    def with_messages(self, *messages: Message) -> ConversationState:
        return replace(self, messages=(*self.messages, *messages))


def _rebuild(records: tuple[TransitionRecord, ...]) -> TransitionLog:
    """Replay the log: every record must continue the chain, and every APPLIED record must be a
    transition the rules allow to that actor. A stored state cannot claim, for example, that an
    operator took a conversation out of BLOCKED. (The open-hours guard cannot be replayed: the
    window is not part of the state, so a guarded record is judged as if the hours were open.)
    """
    log = TransitionLog()
    try:
        for record in records:
            if record.outcome is AttemptOutcome.APPLIED:
                allowed = transition(
                    record.previous_state, record.event, record.actor, hours_open=True
                )
                if allowed.new is not record.new_state:
                    raise ValueError(f"{record.event} does not lead to {record.new_state}")
            log.append(record)
    except ControlError as forbidden:
        raise InvalidConversationState(f"control log: {forbidden.code}") from None
    except ValueError as broken:
        raise InvalidConversationState(f"control log: {broken}") from None
    return log
