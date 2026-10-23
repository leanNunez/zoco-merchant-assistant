"""Properties that must hold for any sequence of events, at any hour, from any actor."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from hypothesis import given
from hypothesis import strategies as st

from merchant_assistant.control.conversation import ConversationControl
from merchant_assistant.control.errors import ControlError
from merchant_assistant.control.hours import BusinessHours
from merchant_assistant.control.log import AttemptOutcome
from merchant_assistant.control.machine import transition
from merchant_assistant.control.states import Actor, Event, State

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")
OFFICE = BusinessHours(BUENOS_AIRES, time(9, 0), time(18, 0), frozenset({0, 1, 2, 3, 4}))

OPEN_AT = datetime(2026, 10, 23, 12, 0, tzinfo=BUENOS_AIRES)  # Friday noon
CLOSED_AT = datetime(2026, 10, 23, 20, 0, tzinfo=BUENOS_AIRES)  # Friday evening
LEAVES_WITH_HUMANS = {State.SUGGEST, State.HUMAN}

# One step is either an event fired by an actor, or the per-turn hours sync.
apply_step = st.tuples(
    st.just("apply"), st.sampled_from(Event), st.sampled_from(Actor), st.booleans()
)
sync_step = st.tuples(st.just("sync"), st.booleans())
steps = st.lists(st.one_of(apply_step, sync_step), max_size=40)


def run(control: ConversationControl, step: tuple[object, ...]) -> tuple[bool, bool, State]:
    """Run one step. Returns (is_open, was_refused, state_before)."""
    before = control.state
    refused = False
    if step[0] == "apply":
        _, event, actor, is_open = step
        assert isinstance(event, Event)
        assert isinstance(actor, Actor)
        try:
            control.apply(event, actor, at=OPEN_AT if is_open else CLOSED_AT, hours=OFFICE)
        except ControlError:  # the only family of errors the machine may raise
            refused = True
    else:
        _, is_open = step
        control.sync_hours(at=OPEN_AT if is_open else CLOSED_AT, hours=OFFICE)
    return bool(is_open), refused, before


@given(steps)
def test_a_conversation_only_leaves_blocked_through_a_supervisor(
    sequence: list[tuple[object, ...]],
) -> None:
    control = ConversationControl()

    for step in sequence:
        _, _, before = run(control, step)

        if before == State.BLOCKED and control.state != State.BLOCKED:
            last = control.log.entries[-1]
            assert last.outcome == AttemptOutcome.APPLIED
            assert last.actor == Actor.SUPERVISOR


@given(steps)
def test_replaying_the_applied_records_from_auto_reproduces_the_state(
    sequence: list[tuple[object, ...]],
) -> None:
    control = ConversationControl()
    for step in sequence:
        run(control, step)

    replayed = State.AUTO
    for record in control.log:
        assert record.previous_state == replayed
        if record.outcome == AttemptOutcome.APPLIED:
            # Whatever was applied must be a legal move in an open window.
            expected = transition(replayed, record.event, record.actor, hours_open=True)
            assert record.new_state == expected.new
            replayed = record.new_state
        else:
            assert record.new_state == replayed

    assert replayed == control.state


@given(steps)
def test_only_the_domain_errors_of_the_machine_ever_escape(
    sequence: list[tuple[object, ...]],
) -> None:
    control = ConversationControl()

    for step in sequence:
        run(control, step)  # any other exception type fails the property here


@given(steps)
def test_a_refused_attempt_never_changes_the_state(
    sequence: list[tuple[object, ...]],
) -> None:
    control = ConversationControl()

    for step in sequence:
        length_before = len(control.log)
        _, refused, before = run(control, step)

        if refused:
            assert control.state == before
            assert len(control.log) == length_before + 1
            assert control.log.entries[-1].outcome == AttemptOutcome.REJECTED


@given(steps)
def test_while_the_hours_are_closed_a_conversation_never_becomes_human_or_suggest(
    sequence: list[tuple[object, ...]],
) -> None:
    control = ConversationControl()

    for step in sequence:
        is_open, _, before = run(control, step)

        if not is_open and before not in LEAVES_WITH_HUMANS:
            assert control.state not in LEAVES_WITH_HUMANS
        if step[0] == "sync" and not is_open:
            assert control.state not in LEAVES_WITH_HUMANS
