"""Properties of the confirmation gate for any sequence of proposals, replies and resets."""

import json
from datetime import UTC, datetime
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from merchant_assistant.agent.domain.errors import AgentError
from merchant_assistant.agent.domain.guards import authorize
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.agent.domain.pending import (
    Confirmed,
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


def pending_action(motivo: str, descripcion: str) -> PendingAction:
    raw = json.dumps({"motivo": motivo, "descripcion": descripcion})
    return PendingAction.create(authorize(ToolCall("c", CREAR_RECLAMO, raw), TOOL_SPECS), AT)


@given(steps)
def test_a_confirmation_needs_a_pending_action_a_strict_yes_and_the_same_fingerprint(
    sequence: list[tuple[Any, ...]],
) -> None:
    state = PendingState()
    for step in sequence:
        if step[0] != "reply":
            state = _other_step(state, step)
            continue
        before = state
        state, decision = decide(state, step[1])
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
    for step in sequence:
        match step:
            case ("propose", motivo, descripcion):
                action = pending_action(motivo, descripcion)
                registered_since_last_proposal.discard(action.fingerprint)
                state, _ = propose(state, action)
            case ("reply", text, succeeded):
                state, decision = decide(state, text)
                if isinstance(decision, Confirmed):
                    assert decision.action.fingerprint not in registered_since_last_proposal
                    if succeeded:
                        state, outcome = mark_registered(state, decision, "REC-000001")
                        registered_since_last_proposal.add(outcome.ref.fingerprint)
            case _:
                state = _other_step(state, step)


@given(steps)
def test_the_gate_raises_nothing_but_agent_errors(sequence: list[tuple[Any, ...]]) -> None:
    state = PendingState()
    last: Confirmed | None = None
    for step in sequence:
        try:
            if step[0] == "reply":
                state, decision = decide(state, step[1])
                last = decision if isinstance(decision, Confirmed) else None
                if last is not None and step[2]:
                    state, _ = mark_registered(state, last, "REC-000001")
            elif step[0] == "register" and last is not None:
                # Sometimes stale on purpose: the confirmation may predate a newer proposal.
                state, _ = mark_registered(state, last, "REC-000001")
            else:
                state = _other_step(state, step)
        except AgentError:
            pass  # a refusal of record; anything else fails the test


def _other_step(state: PendingState, step: tuple[Any, ...]) -> PendingState:
    """Propose or invalidate; a register step with no fresh confirmation does nothing."""
    match step:
        case ("propose", motivo, descripcion):
            return propose(state, pending_action(motivo, descripcion))[0]
        case ("invalidate",):
            return invalidate(state)[0]
        case _:
            return state
