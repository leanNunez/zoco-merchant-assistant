from datetime import UTC, datetime

import pytest

from merchant_assistant.control.log import AttemptOutcome, TransitionLog, TransitionRecord
from merchant_assistant.control.states import Actor, Event, State

NOON = datetime(2026, 10, 23, 15, 0, tzinfo=UTC)


def applied(previous: State, new: State, event: Event, actor: Actor) -> TransitionRecord:
    return TransitionRecord(previous, new, event, actor, NOON, AttemptOutcome.APPLIED)


def rejected(state: State, event: Event, actor: Actor, reason: str) -> TransitionRecord:
    return TransitionRecord(state, state, event, actor, NOON, AttemptOutcome.REJECTED, reason)


def test_an_empty_log_starts_in_auto() -> None:
    log = TransitionLog()

    assert log.state == State.AUTO
    assert len(log) == 0
    assert log.entries == ()


def test_a_record_keeps_the_five_fields_the_brief_requires() -> None:
    record = applied(State.AUTO, State.HUMAN, Event.OPERATOR_TAKEOVER, Actor.OPERATOR)

    assert record.previous_state == State.AUTO
    assert record.new_state == State.HUMAN
    assert record.event == Event.OPERATOR_TAKEOVER
    assert record.actor == Actor.OPERATOR
    assert record.at == NOON


def test_a_record_with_a_naive_date_is_rejected() -> None:
    naive = datetime(2026, 10, 23, 12, 0)  # noqa: DTZ001 - naive on purpose

    with pytest.raises(ValueError, match="timezone-aware"):
        TransitionRecord(
            State.AUTO,
            State.HUMAN,
            Event.OPERATOR_TAKEOVER,
            Actor.OPERATOR,
            naive,
            AttemptOutcome.APPLIED,
        )


def test_an_applied_record_cannot_carry_a_rejection_reason() -> None:
    with pytest.raises(ValueError, match="no rejection reason"):
        TransitionRecord(
            State.AUTO,
            State.HUMAN,
            Event.OPERATOR_TAKEOVER,
            Actor.OPERATOR,
            NOON,
            AttemptOutcome.APPLIED,
            "out_of_hours",
        )


def test_a_rejected_record_must_say_why() -> None:
    with pytest.raises(ValueError, match="code of its rejection"):
        TransitionRecord(
            State.AUTO,
            State.AUTO,
            Event.OPERATOR_TAKEOVER,
            Actor.OPERATOR,
            NOON,
            AttemptOutcome.REJECTED,
        )


def test_entries_come_back_in_the_order_they_were_appended() -> None:
    log = TransitionLog()
    first = applied(State.AUTO, State.HUMAN, Event.OPERATOR_TAKEOVER, Actor.OPERATOR)
    second = applied(State.HUMAN, State.AUTO, Event.OPERATOR_RELEASE, Actor.OPERATOR)

    log.append(first)
    log.append(second)

    assert log.entries == (first, second)
    assert list(log) == [first, second]
    assert len(log) == 2


def test_the_state_follows_the_last_applied_record() -> None:
    log = TransitionLog()

    log.append(applied(State.AUTO, State.HUMAN, Event.OPERATOR_TAKEOVER, Actor.OPERATOR))

    assert log.state == State.HUMAN


def test_a_rejected_record_leaves_the_state_where_it_was() -> None:
    log = TransitionLog()
    log.append(applied(State.AUTO, State.BLOCKED, Event.ABUSE_DETECTED, Actor.SYSTEM))

    log.append(
        rejected(State.BLOCKED, Event.OPERATOR_RELEASE, Actor.OPERATOR, "unauthorized_actor")
    )

    assert log.state == State.BLOCKED
    assert len(log) == 2


def test_an_applied_record_must_start_from_the_current_state() -> None:
    log = TransitionLog()

    with pytest.raises(ValueError, match="starts from HUMAN but the conversation is AUTO"):
        log.append(applied(State.HUMAN, State.AUTO, Event.OPERATOR_RELEASE, Actor.OPERATOR))

    assert len(log) == 0


def test_a_rejected_record_must_start_from_the_current_state() -> None:
    log = TransitionLog()

    with pytest.raises(ValueError, match="starts from BLOCKED"):
        log.append(rejected(State.BLOCKED, Event.OPERATOR_RELEASE, Actor.OPERATOR, "x"))


def test_a_rejected_record_cannot_claim_a_change_of_state() -> None:
    log = TransitionLog()
    forged = TransitionRecord(
        State.AUTO,
        State.HUMAN,
        Event.OPERATOR_TAKEOVER,
        Actor.CUSTOMER,
        NOON,
        AttemptOutcome.REJECTED,
        "unauthorized_actor",
    )

    with pytest.raises(ValueError, match="cannot change the state"):
        log.append(forged)

    assert log.state == State.AUTO


def test_the_entries_a_caller_holds_do_not_grow_with_later_appends() -> None:
    log = TransitionLog()
    snapshot = log.entries

    log.append(applied(State.AUTO, State.HUMAN, Event.OPERATOR_TAKEOVER, Actor.OPERATOR))

    assert snapshot == ()
    assert len(log.entries) == 1
