"""What the merchant's team does to a conversation: operators and supervisors.

`OperatorService` shares the `ConversationStore` with the customer's turn
(`ConversationService`). Every command is load, change, save with optimistic concurrency
(`expected_version`), so a command that lands while a customer turn is running makes that
turn drop its answer (F16), and a command that loses the race is replayed on the fresh state.

Control commands (`takeover`, `release`, `block`, `unblock`) fire their event through
`ConversationControl`: the control log is the audit trail (previous state, new state, event,
actor, time; a refusal is recorded as REJECTED with its code). The effects of an applied
transition are executed by `apply_effects`, the same function the customer's turn uses. A
control refusal (`InvalidTransition`, `UnauthorizedActor`, `GuardRejected`) is saved with the
log and then raised unchanged.

Every command first hands the conversation back to the agent if the human window has closed
(`sync_hours`), exactly as a customer turn does: out of hours only the AI answers, so an
operator cannot write to the customer, nor approve a draft, after closing time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from merchant_assistant.agent.application.conversation import Outbound
from merchant_assistant.agent.domain.drafts import Draft
from merchant_assistant.agent.domain.effects import apply_effects
from merchant_assistant.agent.domain.errors import (
    CommandNotAllowed,
    ConcurrentUpdate,
    InvalidOperatorMessage,
    OperatorReplyNotAllowed,
)
from merchant_assistant.agent.domain.messages import Author, Message, Role
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.agent.ports import ConversationStore
from merchant_assistant.control import (
    Actor,
    BusinessHours,
    ControlError,
    Event,
    State,
)
from merchant_assistant.shared.clock import Clock
from merchant_assistant.shared.errors import DomainError

OPERATOR_MESSAGE_LIMIT = 2000
_SAVE_ATTEMPTS = 3
_TEAM = frozenset({Actor.OPERATOR, Actor.SUPERVISOR})


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one command did, as plain facts. `outbound` is what reached the customer."""

    conversation_id: str
    command: str
    actor: Actor
    control_state: State
    outbound: tuple[Outbound, ...]
    events: tuple[str, ...]
    registered: tuple[str, ...] = ()


@dataclass
class _Change:
    """A command's work on one loaded state. Private and mutable while the command runs."""

    state: ConversationState
    outbound: list[Outbound] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    # A refusal that must be SAVED (it is in the control log) and then raised.
    refusal: DomainError | None = None


_Command = Callable[[_Change, datetime], None]


class OperatorService:
    def __init__(self, store: ConversationStore, *, hours: BusinessHours, clock: Clock) -> None:
        self._store = store
        self._hours = hours
        self._clock = clock

    # ------------------------------------------------------------------ control commands

    def takeover(self, conversation_id: str, actor: Actor) -> CommandResult:
        return self._control(conversation_id, "takeover", Event.OPERATOR_TAKEOVER, actor)

    def release(self, conversation_id: str, actor: Actor) -> CommandResult:
        return self._control(conversation_id, "release", Event.OPERATOR_RELEASE, actor)

    def block(self, conversation_id: str, actor: Actor) -> CommandResult:
        return self._control(conversation_id, "block", Event.OPERATOR_BLOCK, actor)

    def unblock(self, conversation_id: str, actor: Actor) -> CommandResult:
        return self._control(conversation_id, "unblock", Event.SUPERVISOR_UNBLOCK, actor)

    # ------------------------------------------------------------------ writing to the customer

    def operator_reply(self, conversation_id: str, actor: Actor, text: str) -> CommandResult:
        """Deliver an operator's message. Only in HUMAN, where the agent is silent.

        The message enters the history marked as the operator's, so after a release the model
        sees what the person told the customer.
        """
        _require_team(actor, "operator_reply")
        message = text.strip()
        if not message or len(message) > OPERATOR_MESSAGE_LIMIT:
            raise InvalidOperatorMessage(f"length={len(message)}")

        def write(change: _Change, now: datetime) -> None:
            if change.state.control_state is not State.HUMAN:
                raise OperatorReplyNotAllowed(f"state={change.state.control_state}")
            change.state = change.state.with_messages(
                Message(Role.ASSISTANT, message, author=Author.OPERATOR)
            )
            change.outbound.append(Outbound("operator", message))

        return self._run(conversation_id, "operator_reply", actor, write)

    # ------------------------------------------------------------------ drafts

    def drafts(self, conversation_id: str) -> tuple[Draft, ...]:
        """The drafts still waiting for a decision, oldest first."""
        state = self._store.load(conversation_id)
        return () if state is None else state.pending_drafts

    # ------------------------------------------------------------------ the machinery

    def _control(
        self, conversation_id: str, command: str, event: Event, actor: Actor
    ) -> CommandResult:
        def fire(change: _Change, now: datetime) -> None:
            self._fire(change, event, actor, now)

        return self._run(conversation_id, command, actor, fire)

    def _fire(self, change: _Change, event: Event, actor: Actor, now: datetime) -> None:
        control = change.state.control()
        try:
            outcome = control.apply(event, actor, at=now, hours=self._hours)
        except ControlError as refusal:
            change.state = change.state.with_control(control)  # the REJECTED record is kept
            change.events.append(f"{event}:rejected:{refusal.code}")
            change.refusal = refusal
            return
        change.events.append(f"{event}:applied")
        change.state, facts = apply_effects(change.state.with_control(control), outcome, at=now)
        change.events.extend(facts)
        if outcome.previous is State.BLOCKED and outcome.new is not State.BLOCKED:
            # A supervisor reviewed the conversation: the strikes that blocked it are spent,
            # so the next insult starts counting again instead of blocking at once.
            change.state = replace(change.state, abuse_strikes=0)

    def _run(
        self, conversation_id: str, command: str, actor: Actor, work: _Command
    ) -> CommandResult:
        """Load, sync the hours, do the work, save; replay on a concurrent write.

        `work` raises a `DomainError` BEFORE changing anything when the command is not allowed:
        the command stops, and only what happened before it (a hand-back at closing time) is
        saved. A control refusal put in `change.refusal` is saved with the rest (it is part of
        the audit trail). Either way the error reaches the caller unchanged, after the save.
        """
        for _ in range(_SAVE_ATTEMPTS):
            loaded = self._store.load(conversation_id)
            base_version = 0 if loaded is None else loaded.version
            state = loaded if loaded is not None else ConversationState(conversation_id)
            now = self._clock.now()
            change = _Change(state)
            self._sync_hours(change, now)
            synced = change.state
            try:
                work(change, now)
            except DomainError as refusal:
                if not change.events:
                    raise  # nothing happened at all: nothing to save
                change = _Change(synced, events=change.events, refusal=refusal)
            final = replace(change.state, version=base_version + 1)
            try:
                self._store.save(final, expected_version=base_version)
            except ConcurrentUpdate:
                continue
            if change.refusal is not None:
                raise change.refusal
            return CommandResult(
                conversation_id=conversation_id,
                command=command,
                actor=actor,
                control_state=final.control_state,
                outbound=tuple(change.outbound),
                events=tuple(change.events),
                registered=tuple(change.registered),
            )
        raise ConcurrentUpdate(f"conversation {conversation_id[:40]!r} keeps changing")

    def _sync_hours(self, change: _Change, now: datetime) -> None:
        control = change.state.control()
        outcome = control.sync_hours(at=now, hours=self._hours)
        change.state = change.state.with_control(control)
        if outcome is not None:
            change.events.append(f"{outcome.event}:applied")
            change.state, facts = apply_effects(change.state, outcome, at=now)
            change.events.extend(facts)


def _require_team(actor: Actor, command: str) -> None:
    if actor not in _TEAM:
        raise CommandNotAllowed(f"command={command} actor={actor}")
