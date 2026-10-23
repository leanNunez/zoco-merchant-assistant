from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from merchant_assistant.control.conversation import ConversationControl
from merchant_assistant.control.errors import GuardRejected, InvalidTransition, UnauthorizedActor
from merchant_assistant.control.hours import BusinessHours
from merchant_assistant.control.log import AttemptOutcome, TransitionLog, TransitionRecord
from merchant_assistant.control.states import Actor, Effect, Event, State

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
OFFICE = BusinessHours(BUENOS_AIRES, time(9, 0), time(18, 0), frozenset({0, 1, 2, 3, 4}))

# Friday 2026-10-23, Buenos Aires time.
OPEN_AT = datetime(2026, 10, 23, 12, 0, tzinfo=BUENOS_AIRES)
LATER_OPEN_AT = datetime(2026, 10, 23, 13, 0, tzinfo=BUENOS_AIRES)
CLOSED_AT = datetime(2026, 10, 23, 20, 0, tzinfo=BUENOS_AIRES)


def a_conversation_in(state: State) -> ConversationControl:
    """Reach a state only through legal events, as production does."""
    control = ConversationControl()
    path = {
        State.AUTO: [],
        State.SUGGEST: [(Event.LOW_CONFIDENCE, Actor.SYSTEM)],
        State.HUMAN: [(Event.OPERATOR_TAKEOVER, Actor.OPERATOR)],
        State.BLOCKED: [(Event.ABUSE_DETECTED, Actor.SYSTEM)],
    }
    for event, actor in path[state]:
        control.apply(event, actor, at=OPEN_AT, hours=OFFICE)
    assert control.state == state
    return control


# ----------------------------------------------------------------- applying events


def test_a_new_conversation_starts_in_auto_with_an_empty_log() -> None:
    control = ConversationControl()

    assert control.state == State.AUTO
    assert len(control.log) == 0


def test_an_applied_transition_logs_the_five_fields_the_brief_requires() -> None:
    control = ConversationControl()

    control.apply(Event.OPERATOR_TAKEOVER, Actor.OPERATOR, at=OPEN_AT, hours=OFFICE)

    assert control.log.entries == (
        TransitionRecord(
            previous_state=State.AUTO,
            new_state=State.HUMAN,
            event=Event.OPERATOR_TAKEOVER,
            actor=Actor.OPERATOR,
            at=OPEN_AT,
            outcome=AttemptOutcome.APPLIED,
            reason=None,
        ),
    )


def test_applying_an_event_returns_what_the_application_must_do() -> None:
    control = a_conversation_in(State.SUGGEST)

    outcome = control.apply(Event.OPERATOR_RELEASE, Actor.OPERATOR, at=OPEN_AT, hours=OFFICE)

    assert outcome.new == State.AUTO
    assert outcome.effects == {Effect.INVALIDATE_PENDING_ACTION, Effect.DISCARD_UNAPPROVED_DRAFTS}


def test_the_state_is_the_one_the_log_says() -> None:
    control = ConversationControl()
    control.apply(Event.OPERATOR_TAKEOVER, Actor.OPERATOR, at=OPEN_AT, hours=OFFICE)
    control.apply(Event.OPERATOR_BLOCK, Actor.OPERATOR, at=OPEN_AT, hours=OFFICE)

    assert control.log.entries[-1].new_state == State.BLOCKED
    assert control.state == State.BLOCKED


def test_a_conversation_resumes_from_a_log_it_is_given() -> None:
    log = TransitionLog()
    log.append(
        TransitionRecord(
            State.AUTO,
            State.BLOCKED,
            Event.ABUSE_DETECTED,
            Actor.SYSTEM,
            OPEN_AT,
            AttemptOutcome.APPLIED,
        )
    )

    assert ConversationControl(log).state == State.BLOCKED


def test_a_conversation_and_its_log_cannot_disagree() -> None:
    log = TransitionLog()
    control = ConversationControl(log)

    control.apply(Event.ABUSE_DETECTED, Actor.SYSTEM, at=OPEN_AT, hours=OFFICE)

    assert control.log is log
    assert control.state == log.state == State.BLOCKED


# ---------------------------------------------------------------- refused attempts


def test_a_refused_attempt_is_logged_with_its_reason_and_raised_again() -> None:
    control = a_conversation_in(State.BLOCKED)

    with pytest.raises(UnauthorizedActor):
        control.apply(Event.OPERATOR_RELEASE, Actor.OPERATOR, at=LATER_OPEN_AT, hours=OFFICE)

    assert control.log.entries[-1] == TransitionRecord(
        previous_state=State.BLOCKED,
        new_state=State.BLOCKED,
        event=Event.OPERATOR_RELEASE,
        actor=Actor.OPERATOR,
        at=LATER_OPEN_AT,
        outcome=AttemptOutcome.REJECTED,
        reason="unauthorized_actor",
    )


def test_a_refused_attempt_does_not_change_the_state() -> None:
    control = a_conversation_in(State.BLOCKED)

    with pytest.raises(UnauthorizedActor):
        control.apply(Event.OPERATOR_RELEASE, Actor.OPERATOR, at=OPEN_AT, hours=OFFICE)

    assert control.state == State.BLOCKED


def test_an_undefined_event_is_logged_as_rejected_with_its_code() -> None:
    control = ConversationControl()

    with pytest.raises(InvalidTransition):
        control.apply(Event.OPERATOR_RELEASE, Actor.OPERATOR, at=OPEN_AT, hours=OFFICE)

    assert control.log.entries[-1].reason == "invalid_transition"
    assert control.log.entries[-1].outcome == AttemptOutcome.REJECTED


def test_a_takeover_out_of_hours_is_logged_as_rejected_by_the_guard() -> None:
    control = ConversationControl()

    with pytest.raises(GuardRejected):
        control.apply(Event.OPERATOR_TAKEOVER, Actor.OPERATOR, at=CLOSED_AT, hours=OFFICE)

    assert control.log.entries[-1].reason == "out_of_hours"
    assert control.state == State.AUTO


def test_an_attempt_with_a_naive_date_is_refused_before_anything_is_logged() -> None:
    control = ConversationControl()
    naive = datetime(2026, 10, 23, 12, 0)  # noqa: DTZ001 - naive on purpose

    with pytest.raises(ValueError, match="timezone-aware"):
        control.apply(Event.OPERATOR_TAKEOVER, Actor.OPERATOR, at=naive, hours=OFFICE)

    assert len(control.log) == 0


def test_the_hours_are_judged_at_the_instant_of_the_event() -> None:
    control = ConversationControl()
    in_utc = datetime(2026, 10, 23, 23, 30, tzinfo=UTC)  # 20:30 in Buenos Aires

    with pytest.raises(GuardRejected):
        control.apply(Event.OPERATOR_TAKEOVER, Actor.OPERATOR, at=in_utc, hours=OFFICE)


# ---------------------------------------------------------------------- sync_hours


@pytest.mark.parametrize("state", [State.SUGGEST, State.HUMAN])
def test_closing_time_hands_a_human_or_suggest_conversation_back_to_the_agent(
    state: State,
) -> None:
    control = a_conversation_in(state)

    outcome = control.sync_hours(at=CLOSED_AT, hours=OFFICE)

    assert outcome is not None
    assert outcome.new == State.AUTO
    assert control.state == State.AUTO


def test_the_hand_back_is_recorded_as_a_system_event() -> None:
    control = a_conversation_in(State.HUMAN)

    control.sync_hours(at=CLOSED_AT, hours=OFFICE)

    assert control.log.entries[-1] == TransitionRecord(
        previous_state=State.HUMAN,
        new_state=State.AUTO,
        event=Event.OUT_OF_HOURS_STARTED,
        actor=Actor.SYSTEM,
        at=CLOSED_AT,
        outcome=AttemptOutcome.APPLIED,
    )


def test_when_the_operator_is_in_human_and_the_window_closes_the_agent_resumes() -> None:
    control = ConversationControl()
    control.apply(Event.OPERATOR_TAKEOVER, Actor.OPERATOR, at=OPEN_AT, hours=OFFICE)

    outcome = control.sync_hours(at=CLOSED_AT, hours=OFFICE)

    assert control.state == State.AUTO
    assert outcome is not None
    assert Effect.INVALIDATE_PENDING_ACTION in outcome.effects
    assert [(r.actor, r.new_state) for r in control.log][-1] == (Actor.SYSTEM, State.AUTO)


def test_leaving_suggest_at_closing_time_discards_the_drafts() -> None:
    control = a_conversation_in(State.SUGGEST)

    outcome = control.sync_hours(at=CLOSED_AT, hours=OFFICE)

    assert outcome is not None
    assert Effect.DISCARD_UNAPPROVED_DRAFTS in outcome.effects


@pytest.mark.parametrize("state", [State.AUTO, State.BLOCKED])
def test_closing_time_changes_nothing_in_auto_or_blocked(state: State) -> None:
    control = a_conversation_in(state)
    entries_before = control.log.entries

    assert control.sync_hours(at=CLOSED_AT, hours=OFFICE) is None

    assert control.state == state
    assert control.log.entries == entries_before


@pytest.mark.parametrize("state", [State.AUTO, State.SUGGEST, State.HUMAN, State.BLOCKED])
def test_while_the_window_is_open_syncing_changes_nothing(state: State) -> None:
    control = a_conversation_in(state)
    entries_before = control.log.entries

    assert control.sync_hours(at=LATER_OPEN_AT, hours=OFFICE) is None

    assert control.state == state
    assert control.log.entries == entries_before


def test_syncing_twice_after_closing_logs_only_one_hand_back() -> None:
    control = a_conversation_in(State.HUMAN)
    control.sync_hours(at=CLOSED_AT, hours=OFFICE)
    entries_after_first = len(control.log)

    assert control.sync_hours(at=CLOSED_AT, hours=OFFICE) is None

    assert len(control.log) == entries_after_first


def test_a_blocked_conversation_stays_blocked_through_closing_time() -> None:
    control = a_conversation_in(State.BLOCKED)

    control.apply(Event.OUT_OF_HOURS_STARTED, Actor.SYSTEM, at=CLOSED_AT, hours=OFFICE)

    assert control.state == State.BLOCKED
    assert control.log.entries[-1].outcome == AttemptOutcome.APPLIED
