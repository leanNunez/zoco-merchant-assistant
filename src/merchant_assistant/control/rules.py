"""The specification as data: the whole truth about which transitions exist.

Any (state, event) pair that is not a row here is undefined, and the machine refuses it.
`SPEC_REVIEW.md` explains each row that differs from the brief.
"""

from __future__ import annotations

from dataclasses import dataclass

from merchant_assistant.control.states import Actor, Event, State

OPERATOR_OR_ABOVE = frozenset({Actor.OPERATOR, Actor.SUPERVISOR})
CUSTOMER_OR_SYSTEM = frozenset({Actor.CUSTOMER, Actor.SYSTEM})
SYSTEM_ONLY = frozenset({Actor.SYSTEM})
SUPERVISOR_ONLY = frozenset({Actor.SUPERVISOR})


@dataclass(frozen=True)
class Rule:
    source: State
    event: Event
    target: State
    actors: frozenset[Actor]
    # Rule 3 of the brief: out of hours only the AI answers, so handing the conversation
    # to a human who will not answer is refused.
    requires_open_hours: bool


def _rule(
    source: State,
    event: Event,
    target: State,
    actors: frozenset[Actor],
    *,
    open_hours: bool,
) -> Rule:
    return Rule(source, event, target, actors, requires_open_hours=open_hours)


RULES: tuple[Rule, ...] = (
    _rule(State.AUTO, Event.OPERATOR_TAKEOVER, State.HUMAN, OPERATOR_OR_ABOVE, open_hours=True),
    _rule(
        State.AUTO, Event.CUSTOMER_REQUESTS_HUMAN, State.HUMAN, CUSTOMER_OR_SYSTEM, open_hours=True
    ),
    _rule(State.AUTO, Event.LOW_CONFIDENCE, State.SUGGEST, SYSTEM_ONLY, open_hours=True),
    _rule(State.AUTO, Event.ABUSE_DETECTED, State.BLOCKED, SYSTEM_ONLY, open_hours=False),
    _rule(State.AUTO, Event.OUT_OF_HOURS_STARTED, State.AUTO, SYSTEM_ONLY, open_hours=False),
    _rule(State.SUGGEST, Event.OPERATOR_TAKEOVER, State.HUMAN, OPERATOR_OR_ABOVE, open_hours=True),
    _rule(
        State.SUGGEST,
        Event.CUSTOMER_REQUESTS_HUMAN,
        State.HUMAN,
        CUSTOMER_OR_SYSTEM,
        open_hours=True,
    ),
    _rule(State.SUGGEST, Event.OPERATOR_RELEASE, State.AUTO, OPERATOR_OR_ABOVE, open_hours=False),
    _rule(State.SUGGEST, Event.ABUSE_DETECTED, State.BLOCKED, SYSTEM_ONLY, open_hours=False),
    _rule(State.SUGGEST, Event.OUT_OF_HOURS_STARTED, State.AUTO, SYSTEM_ONLY, open_hours=False),
    _rule(State.HUMAN, Event.OPERATOR_RELEASE, State.AUTO, OPERATOR_OR_ABOVE, open_hours=False),
    _rule(State.HUMAN, Event.ABUSE_DETECTED, State.HUMAN, SYSTEM_ONLY, open_hours=False),
    _rule(State.HUMAN, Event.OPERATOR_BLOCK, State.BLOCKED, OPERATOR_OR_ABOVE, open_hours=False),
    _rule(State.HUMAN, Event.OUT_OF_HOURS_STARTED, State.AUTO, SYSTEM_ONLY, open_hours=False),
    _rule(State.BLOCKED, Event.OPERATOR_RELEASE, State.AUTO, SUPERVISOR_ONLY, open_hours=False),
    _rule(State.BLOCKED, Event.SUPERVISOR_UNBLOCK, State.AUTO, SUPERVISOR_ONLY, open_hours=False),
    _rule(State.BLOCKED, Event.OUT_OF_HOURS_STARTED, State.BLOCKED, SYSTEM_ONLY, open_hours=False),
)


def _index(rules: tuple[Rule, ...]) -> dict[tuple[State, Event], Rule]:
    by_pair: dict[tuple[State, Event], Rule] = {}
    for rule in rules:
        key = (rule.source, rule.event)
        if key in by_pair:
            # The brief had a duplicated pair with two different targets; never again.
            raise ValueError(f"duplicate rule for {rule.source} + {rule.event}")
        by_pair[key] = rule
    return by_pair


_BY_PAIR = _index(RULES)


def rule_for(state: State, event: Event) -> Rule | None:
    """The rule for this pair, or None when the pair is undefined."""
    return _BY_PAIR.get((state, event))
