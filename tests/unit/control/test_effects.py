"""What the application layer must do after each transition, typed by hand."""

import pytest

from merchant_assistant.control.machine import transition
from merchant_assistant.control.states import Actor, Effect, Event, State

INVALIDATE = Effect.INVALIDATE_PENDING_ACTION
DISCARD = Effect.DISCARD_UNAPPROVED_DRAFTS
NOTIFY = Effect.NOTIFY_OPERATOR

A, S, H, B = State.AUTO, State.SUGGEST, State.HUMAN, State.BLOCKED

# (source, event, actor, effects)
EFFECTS_OF_RECORD = [
    (A, Event.OPERATOR_TAKEOVER, Actor.OPERATOR, {INVALIDATE}),
    (A, Event.CUSTOMER_REQUESTS_HUMAN, Actor.CUSTOMER, {INVALIDATE}),
    (A, Event.LOW_CONFIDENCE, Actor.SYSTEM, {INVALIDATE}),
    (A, Event.ABUSE_DETECTED, Actor.SYSTEM, {INVALIDATE}),
    (A, Event.OUT_OF_HOURS_STARTED, Actor.SYSTEM, set()),
    (S, Event.OPERATOR_TAKEOVER, Actor.OPERATOR, {INVALIDATE, DISCARD}),
    (S, Event.CUSTOMER_REQUESTS_HUMAN, Actor.CUSTOMER, {INVALIDATE, DISCARD}),
    (S, Event.OPERATOR_RELEASE, Actor.OPERATOR, {INVALIDATE, DISCARD}),
    (S, Event.ABUSE_DETECTED, Actor.SYSTEM, {INVALIDATE, DISCARD}),
    (S, Event.OUT_OF_HOURS_STARTED, Actor.SYSTEM, {INVALIDATE, DISCARD}),
    (H, Event.OPERATOR_RELEASE, Actor.OPERATOR, {INVALIDATE}),
    (H, Event.ABUSE_DETECTED, Actor.SYSTEM, {NOTIFY}),
    (H, Event.OPERATOR_BLOCK, Actor.OPERATOR, {INVALIDATE}),
    (H, Event.OUT_OF_HOURS_STARTED, Actor.SYSTEM, {INVALIDATE, NOTIFY}),
    (B, Event.OPERATOR_RELEASE, Actor.SUPERVISOR, {INVALIDATE}),
    (B, Event.SUPERVISOR_UNBLOCK, Actor.SUPERVISOR, {INVALIDATE}),
    (B, Event.OUT_OF_HOURS_STARTED, Actor.SYSTEM, set()),
]


@pytest.mark.parametrize(
    ("source", "event", "actor", "expected"),
    [pytest.param(*row, id=f"{row[0].value}+{row[1].value}") for row in EFFECTS_OF_RECORD],
)
def test_every_rule_carries_exactly_the_effects_of_record(
    source: State, event: Event, actor: Actor, expected: set[Effect]
) -> None:
    outcome = transition(source, event, actor, hours_open=True)

    assert outcome.effects == frozenset(expected)


def test_a_change_of_who_controls_the_chat_invalidates_the_pending_action() -> None:
    outcome = transition(A, Event.OPERATOR_TAKEOVER, Actor.OPERATOR, hours_open=True)

    assert INVALIDATE in outcome.effects


def test_leaving_suggest_discards_the_drafts_nobody_approved() -> None:
    outcome = transition(S, Event.OPERATOR_RELEASE, Actor.OPERATOR, hours_open=True)

    assert DISCARD in outcome.effects


def test_entering_suggest_discards_nothing() -> None:
    outcome = transition(A, Event.LOW_CONFIDENCE, Actor.SYSTEM, hours_open=True)

    assert DISCARD not in outcome.effects


def test_abuse_while_a_human_answers_notifies_without_invalidating() -> None:
    outcome = transition(H, Event.ABUSE_DETECTED, Actor.SYSTEM, hours_open=True)

    assert outcome.new == H
    assert outcome.effects == {NOTIFY}


def test_closing_time_in_human_notifies_the_operator_and_hands_the_chat_back() -> None:
    outcome = transition(H, Event.OUT_OF_HOURS_STARTED, Actor.SYSTEM, hours_open=False)

    assert outcome.new == A
    assert outcome.effects == {INVALIDATE, NOTIFY}


@pytest.mark.parametrize("source", [A, S, B])
def test_closing_time_outside_human_does_not_notify_the_operator(source: State) -> None:
    outcome = transition(source, Event.OUT_OF_HOURS_STARTED, Actor.SYSTEM, hours_open=False)

    assert NOTIFY not in outcome.effects


def test_the_effects_cannot_be_changed_after_the_fact() -> None:
    outcome = transition(A, Event.OPERATOR_TAKEOVER, Actor.OPERATOR, hours_open=True)

    assert isinstance(outcome.effects, frozenset)
