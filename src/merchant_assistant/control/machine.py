"""The transition function: pure, deterministic, with no clock and no I/O."""

from __future__ import annotations

from dataclasses import dataclass

from merchant_assistant.control.errors import GuardRejected, InvalidTransition, UnauthorizedActor
from merchant_assistant.control.rules import rule_for
from merchant_assistant.control.states import Actor, Effect, Event, State


@dataclass(frozen=True)
class Outcome:
    """A transition that was allowed, with what the application layer must do about it."""

    previous: State
    new: State
    event: Event
    actor: Actor
    effects: frozenset[Effect]


def effects_for(source: State, event: Event, target: State) -> frozenset[Effect]:
    """What the application must do when this transition happens. One place, four rules."""
    effects: set[Effect] = set()
    if source != target:
        # A half-confirmed complaint must never survive a change of who controls the chat.
        effects.add(Effect.INVALIDATE_PENDING_ACTION)
    if source == State.SUGGEST and target != State.SUGGEST:
        # A draft nobody approved must never be sent later, once the operator is out.
        effects.add(Effect.DISCARD_UNAPPROVED_DRAFTS)
    if source == State.HUMAN and event == Event.ABUSE_DETECTED:
        # The state does not change (nothing to invalidate): the operator decides.
        effects.add(Effect.NOTIFY_OPERATOR)
    if source == State.HUMAN and event == Event.OUT_OF_HOURS_STARTED:
        # Closing time takes the conversation away from a person: the operator must not find
        # the agent answering a chat they believed was theirs, with no trace of why.
        effects.add(Effect.NOTIFY_OPERATOR)
    return frozenset(effects)


def transition(state: State, event: Event, actor: Actor, *, hours_open: bool) -> Outcome:
    """Apply an event to a state.

    The order of the checks is part of the contract:

    1. the (state, event) pair must be defined, else `InvalidTransition`;
    2. the actor must be allowed to fire it, else `UnauthorizedActor`;
    3. the open-hours guard must hold, else `GuardRejected`.

    Authorization comes before the guard so that an unauthorized actor learns nothing
    about the attention hours.
    """
    rule = rule_for(state, event)
    if rule is None:
        raise InvalidTransition(state, event, actor)
    if actor not in rule.actors:
        raise UnauthorizedActor(state, event, actor)
    if rule.requires_open_hours and not hours_open:
        raise GuardRejected(state, event, actor)
    return Outcome(
        previous=state,
        new=rule.target,
        event=event,
        actor=actor,
        effects=effects_for(state, event, rule.target),
    )
