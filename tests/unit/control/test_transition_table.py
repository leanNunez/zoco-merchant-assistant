"""The specification, as a literal table of record.

The expectations below are typed by hand from the resolved specification. They are never
derived from `merchant_assistant.control.rules`: a test that reads the answer from the code
it checks proves nothing.
"""

from dataclasses import dataclass
from itertools import product

import pytest

from merchant_assistant.control.errors import GuardRejected, InvalidTransition, UnauthorizedActor
from merchant_assistant.control.machine import transition
from merchant_assistant.control.rules import RULES
from merchant_assistant.control.states import Actor, Event, State

OPERATOR_OR_ABOVE = frozenset({Actor.OPERATOR, Actor.SUPERVISOR})
CUSTOMER_OR_SYSTEM = frozenset({Actor.CUSTOMER, Actor.SYSTEM})
SYSTEM_ONLY = frozenset({Actor.SYSTEM})
SUPERVISOR_ONLY = frozenset({Actor.SUPERVISOR})


@dataclass(frozen=True)
class Row:
    source: State
    event: Event
    target: State
    actors: frozenset[Actor]
    needs_open_hours: bool

    @property
    def id(self) -> str:
        return f"{self.source.value}+{self.event.value}"


A, S, H, B = State.AUTO, State.SUGGEST, State.HUMAN, State.BLOCKED

TABLE_OF_RECORD = (
    Row(A, Event.OPERATOR_TAKEOVER, H, OPERATOR_OR_ABOVE, True),
    Row(A, Event.CUSTOMER_REQUESTS_HUMAN, H, CUSTOMER_OR_SYSTEM, True),
    Row(A, Event.LOW_CONFIDENCE, S, SYSTEM_ONLY, True),
    Row(A, Event.ABUSE_DETECTED, B, SYSTEM_ONLY, False),
    Row(A, Event.OUT_OF_HOURS_STARTED, A, SYSTEM_ONLY, False),
    Row(S, Event.OPERATOR_TAKEOVER, H, OPERATOR_OR_ABOVE, True),
    Row(S, Event.CUSTOMER_REQUESTS_HUMAN, H, CUSTOMER_OR_SYSTEM, True),
    Row(S, Event.OPERATOR_RELEASE, A, OPERATOR_OR_ABOVE, False),
    Row(S, Event.ABUSE_DETECTED, B, SYSTEM_ONLY, False),
    Row(S, Event.OUT_OF_HOURS_STARTED, A, SYSTEM_ONLY, False),
    Row(H, Event.OPERATOR_RELEASE, A, OPERATOR_OR_ABOVE, False),
    Row(H, Event.ABUSE_DETECTED, H, SYSTEM_ONLY, False),
    Row(H, Event.OPERATOR_BLOCK, B, OPERATOR_OR_ABOVE, False),
    Row(H, Event.OUT_OF_HOURS_STARTED, A, SYSTEM_ONLY, False),
    Row(B, Event.OPERATOR_RELEASE, A, SUPERVISOR_ONLY, False),
    Row(B, Event.SUPERVISOR_UNBLOCK, A, SUPERVISOR_ONLY, False),
    Row(B, Event.OUT_OF_HOURS_STARTED, B, SYSTEM_ONLY, False),
)

DEFINED_PAIRS = {(row.source, row.event) for row in TABLE_OF_RECORD}
UNDEFINED_PAIRS = sorted(
    set(product(State, Event)) - DEFINED_PAIRS, key=lambda pair: (pair[0].value, pair[1].value)
)

ALL_ROWS = [pytest.param(row, id=row.id) for row in TABLE_OF_RECORD]
GUARDED_ROWS = [pytest.param(row, id=row.id) for row in TABLE_OF_RECORD if row.needs_open_hours]
UNGUARDED_ROWS = [
    pytest.param(row, id=row.id) for row in TABLE_OF_RECORD if not row.needs_open_hours
]


def an_allowed_actor(row: Row) -> Actor:
    return sorted(row.actors, key=lambda actor: actor.value)[0]


# ----------------------------------------------------------------- the table of record


def test_the_specification_defines_exactly_seventeen_rules() -> None:
    assert len(TABLE_OF_RECORD) == 17
    assert len(DEFINED_PAIRS) == 17


def test_no_pair_has_two_rules_so_no_transition_is_ambiguous() -> None:
    pairs = [(rule.source, rule.event) for rule in RULES]

    assert len(pairs) == len(set(pairs)) == 17
    assert set(pairs) == DEFINED_PAIRS


@pytest.mark.parametrize("row", ALL_ROWS)
def test_every_defined_rule_leads_to_its_target(row: Row) -> None:
    outcome = transition(row.source, row.event, an_allowed_actor(row), hours_open=True)

    assert outcome.previous == row.source
    assert outcome.new == row.target
    assert outcome.event == row.event


@pytest.mark.parametrize("row", ALL_ROWS)
def test_every_allowed_actor_can_fire_the_rule(row: Row) -> None:
    for actor in row.actors:
        outcome = transition(row.source, row.event, actor, hours_open=True)

        assert outcome.actor == actor
        assert outcome.new == row.target


# ------------------------------------------------------------ undefined transitions


def test_exactly_fifteen_of_the_thirty_two_pairs_are_undefined() -> None:
    assert len(list(product(State, Event))) == 32
    assert len(UNDEFINED_PAIRS) == 15


@pytest.mark.parametrize(
    ("state", "event"),
    [pytest.param(s, e, id=f"{s.value}+{e.value}") for s, e in UNDEFINED_PAIRS],
)
def test_an_undefined_pair_is_refused_whoever_asks_and_whatever_the_hours(
    state: State, event: Event
) -> None:
    for actor, hours_open in product(Actor, (True, False)):
        with pytest.raises(InvalidTransition) as refused:
            transition(state, event, actor, hours_open=hours_open)

        assert refused.value.code == "invalid_transition"


@pytest.mark.parametrize(
    ("state", "event", "actor"),
    [
        pytest.param(H, Event.LOW_CONFIDENCE, Actor.SYSTEM, id="a human is already answering"),
        pytest.param(B, Event.OPERATOR_TAKEOVER, Actor.OPERATOR, id="blocked, no takeover"),
        pytest.param(B, Event.ABUSE_DETECTED, Actor.SYSTEM, id="blocked cannot be blocked again"),
        pytest.param(A, Event.OPERATOR_RELEASE, Actor.OPERATOR, id="nothing to release in AUTO"),
        pytest.param(S, Event.SUPERVISOR_UNBLOCK, Actor.SUPERVISOR, id="nothing blocked to undo"),
        pytest.param(S, Event.LOW_CONFIDENCE, Actor.SYSTEM, id="redundant event is not swallowed"),
        pytest.param(A, Event.OPERATOR_BLOCK, Actor.SUPERVISOR, id="only a human can be blocked"),
        pytest.param(B, Event.CUSTOMER_REQUESTS_HUMAN, Actor.CUSTOMER, id="blocked ignores asks"),
    ],
)
def test_readable_cases_the_specification_does_not_allow(
    state: State, event: Event, actor: Actor
) -> None:
    with pytest.raises(InvalidTransition):
        transition(state, event, actor, hours_open=True)


# ------------------------------------------------------------------------- roles


@pytest.mark.parametrize("row", ALL_ROWS)
def test_an_actor_outside_the_allowed_set_is_refused(row: Row) -> None:
    for actor in set(Actor) - row.actors:
        with pytest.raises(UnauthorizedActor) as refused:
            transition(row.source, row.event, actor, hours_open=True)

        assert refused.value.code == "unauthorized_actor"


@pytest.mark.parametrize("event", [Event.OPERATOR_RELEASE, Event.SUPERVISOR_UNBLOCK])
def test_only_a_supervisor_can_leave_blocked(event: Event) -> None:
    allowed = {actor for actor in Actor if _accepts(State.BLOCKED, event, actor)}

    assert allowed == {Actor.SUPERVISOR}


def _accepts(state: State, event: Event, actor: Actor) -> bool:
    try:
        transition(state, event, actor, hours_open=True)
    except UnauthorizedActor:
        return False
    return True


def test_an_operator_cannot_release_a_blocked_conversation() -> None:
    with pytest.raises(UnauthorizedActor):
        transition(B, Event.OPERATOR_RELEASE, Actor.OPERATOR, hours_open=True)


def test_a_supervisor_can_release_a_blocked_conversation() -> None:
    assert transition(B, Event.OPERATOR_RELEASE, Actor.SUPERVISOR, hours_open=True).new == A
    assert transition(B, Event.SUPERVISOR_UNBLOCK, Actor.SUPERVISOR, hours_open=True).new == A


@pytest.mark.parametrize("state", [A, S])
def test_a_customer_cannot_take_over_a_conversation(state: State) -> None:
    with pytest.raises(UnauthorizedActor):
        transition(state, Event.OPERATOR_TAKEOVER, Actor.CUSTOMER, hours_open=True)


@pytest.mark.parametrize("row", ALL_ROWS)
def test_a_supervisor_may_do_everything_an_operator_may(row: Row) -> None:
    if Actor.OPERATOR in row.actors:
        assert Actor.SUPERVISOR in row.actors
        assert (
            transition(row.source, row.event, Actor.SUPERVISOR, hours_open=True).new == row.target
        )


def test_only_the_system_reports_abuse_or_low_confidence() -> None:
    reports = [(A, Event.ABUSE_DETECTED), (S, Event.ABUSE_DETECTED), (A, Event.LOW_CONFIDENCE)]
    for state, event in reports:
        for actor in (Actor.CUSTOMER, Actor.OPERATOR, Actor.SUPERVISOR):
            with pytest.raises(UnauthorizedActor):
                transition(state, event, actor, hours_open=True)


# ----------------------------------------------------------------- the hours guard


def test_five_rules_are_guarded_by_the_open_hours() -> None:
    assert len(TABLE_OF_RECORD) - len(UNGUARDED_ROWS) == 5
    assert {row.id for row in TABLE_OF_RECORD if row.needs_open_hours} == {
        "AUTO+operator_takeover",
        "AUTO+customer_requests_human",
        "AUTO+low_confidence",
        "SUGGEST+operator_takeover",
        "SUGGEST+customer_requests_human",
    }


@pytest.mark.parametrize("row", GUARDED_ROWS)
def test_a_guarded_rule_is_refused_when_the_hours_are_closed(row: Row) -> None:
    with pytest.raises(GuardRejected) as refused:
        transition(row.source, row.event, an_allowed_actor(row), hours_open=False)

    assert refused.value.code == "out_of_hours"


@pytest.mark.parametrize("row", GUARDED_ROWS)
def test_a_guarded_rule_passes_when_the_hours_are_open(row: Row) -> None:
    outcome = transition(row.source, row.event, an_allowed_actor(row), hours_open=True)

    assert outcome.new == row.target


@pytest.mark.parametrize("row", UNGUARDED_ROWS)
def test_an_unguarded_rule_works_when_the_hours_are_closed(row: Row) -> None:
    for actor in row.actors:
        assert transition(row.source, row.event, actor, hours_open=False).new == row.target


@pytest.mark.parametrize("row", GUARDED_ROWS)
def test_an_unauthorized_actor_learns_nothing_about_the_hours(row: Row) -> None:
    outsiders = set(Actor) - row.actors

    for actor in outsiders:
        with pytest.raises(UnauthorizedActor):
            transition(row.source, row.event, actor, hours_open=False)


def test_an_undefined_pair_is_reported_before_the_hours_are_looked_at() -> None:
    with pytest.raises(InvalidTransition):
        transition(H, Event.LOW_CONFIDENCE, Actor.SYSTEM, hours_open=False)
