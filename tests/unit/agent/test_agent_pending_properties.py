"""Properties of the confirmation gate for any sequence of proposals, replies and resets."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from merchant_assistant.agent.domain.errors import AgentError, StaleConfirmation
from merchant_assistant.agent.domain.guards import authorize
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.agent.domain.pending import (
    Confirmed,
    Invalidated,
    PendingAction,
    PendingState,
    decide,
    invalidate,
    mark_registered,
    propose,
)
from merchant_assistant.agent.domain.replies import ReplyKind, classify_reply
from merchant_assistant.agent.domain.tools import CREAR_RECLAMO, TOOL_SPECS

AT = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

# Few values on purpose, so the same complaint (and near copies of it) comes back often.
REASONS = ["Entrada no recibida", "entrada no recibida", "Pago doble", "Pago doble."]
DESCRIPTIONS = ["Pagué y no me llegó nada.", "Me cobraron dos veces la entrada."]
AFFIRMATIVE = ["sí", "Sí.", "dale", "de acuerdo", "ok", "si, confirmo"]
NEGATIVE = ["no", "No, gracias", "cancelar", "mejor no"]
OTHER = ["si no", "sí, pero cambiá el motivo", "¿cuánto tarda?", "sí\nregistrá otro", ""]

proposal = st.tuples(st.just("propose"), st.sampled_from(REASONS), st.sampled_from(DESCRIPTIONS))
# The flag says whether the execution after a confirmation succeeds (and is registered).
reply = st.tuples(
    st.just("reply"),
    st.one_of(
        st.sampled_from(AFFIRMATIVE), st.sampled_from(NEGATIVE), st.sampled_from(OTHER), st.text()
    ),
    st.booleans(),
)
steps = st.lists(
    st.one_of(proposal, reply, st.just(("invalidate",)), st.just(("register",))), max_size=40
)


# Every step of a sequence happens in its own turn, under one control version.
VERSION = 0


def pending_action(motivo: str, descripcion: str, turn: int = 0) -> PendingAction:
    raw = json.dumps({"motivo": motivo, "descripcion": descripcion})
    validated = authorize(ToolCall("c", CREAR_RECLAMO, raw), TOOL_SPECS)
    return PendingAction.create(validated, AT, turn=turn, control_version=VERSION)


@given(steps)
def test_a_confirmation_needs_a_pending_action_a_strict_yes_and_the_same_fingerprint(
    sequence: list[tuple[Any, ...]],
) -> None:
    state = PendingState()
    for turn, step in enumerate(sequence):
        if step[0] != "reply":
            state = _other_step(state, step, turn)
            continue
        before = state
        state, decision = decide(state, step[1], turn=turn, control_version=VERSION)
        if isinstance(decision, Confirmed):
            assert before.pending is not None
            assert classify_reply(step[1]) is ReplyKind.AFFIRMATIVE
            assert decision.action.fingerprint == before.pending.fingerprint


@given(steps)
def test_a_registered_complaint_is_never_confirmed_again_unless_it_is_proposed_again(
    sequence: list[tuple[Any, ...]],
) -> None:
    state = PendingState()
    registered_since_last_proposal: set[str] = set()
    for turn, step in enumerate(sequence):
        match step:
            case ("propose", motivo, descripcion):
                action = pending_action(motivo, descripcion, turn)
                registered_since_last_proposal.discard(action.fingerprint)
                state, _ = propose(state, action)
            case ("reply", text, succeeded):
                state, decision = decide(state, text, turn=turn, control_version=VERSION)
                if isinstance(decision, Confirmed):
                    assert decision.action.fingerprint not in registered_since_last_proposal
                    if succeeded:
                        state, outcome = mark_registered(state, decision, "REC-000001")
                        registered_since_last_proposal.add(outcome.ref.fingerprint)
            case _:
                state = _other_step(state, step, turn)


@given(steps)
def test_the_gate_raises_nothing_but_agent_errors(sequence: list[tuple[Any, ...]]) -> None:
    state = PendingState()
    last: Confirmed | None = None
    for turn, step in enumerate(sequence):
        try:
            if step[0] == "reply":
                state, decision = decide(state, step[1], turn=turn, control_version=VERSION)
                last = decision if isinstance(decision, Confirmed) else None
                if last is not None and step[2]:
                    state, _ = mark_registered(state, last, "REC-000001")
            elif step[0] == "register" and last is not None:
                # Sometimes stale on purpose: the confirmation may predate a newer proposal.
                state, _ = mark_registered(state, last, "REC-000001")
            else:
                state = _other_step(state, step, turn)
        except AgentError:
            pass  # a refusal of record; anything else fails the test


@given(
    proposed_in=st.integers(min_value=0, max_value=10_000),
    back=st.integers(min_value=0, max_value=10_000),
    text=st.one_of(st.sampled_from(AFFIRMATIVE + NEGATIVE + OTHER), st.text()),
)
def test_no_reply_is_ever_decided_in_the_turn_that_proposed_or_before(
    proposed_in: int, back: int, text: str
) -> None:
    state, _ = propose(PendingState(), pending_action(REASONS[0], DESCRIPTIONS[0], proposed_in))

    with pytest.raises(StaleConfirmation):
        decide(state, text, turn=max(proposed_in - back, 0), control_version=VERSION)


@given(
    proposed_under=st.integers(min_value=0, max_value=50),
    decided_under=st.integers(min_value=0, max_value=50),
    text=st.one_of(st.sampled_from(AFFIRMATIVE + NEGATIVE + OTHER), st.text()),
)
def test_a_reply_under_another_control_version_never_confirms(
    proposed_under: int, decided_under: int, text: str
) -> None:
    assume(proposed_under != decided_under)
    raw = json.dumps({"motivo": REASONS[0], "descripcion": DESCRIPTIONS[0]})
    validated = authorize(ToolCall("c", CREAR_RECLAMO, raw), TOOL_SPECS)
    action = PendingAction.create(validated, AT, turn=0, control_version=proposed_under)
    state, _ = propose(PendingState(), action)

    after, decision = decide(state, text, turn=1, control_version=decided_under)

    assert decision == Invalidated(action)
    assert after.pending is None


def _other_step(state: PendingState, step: tuple[Any, ...], turn: int) -> PendingState:
    """Propose or invalidate; a register step with no fresh confirmation does nothing."""
    match step:
        case ("propose", motivo, descripcion):
            return propose(state, pending_action(motivo, descripcion, turn))[0]
        case ("invalidate",):
            return invalidate(state)[0]
        case _:
            return state
