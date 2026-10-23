"""The confirmation gate: a complaint runs only on a later, strict yes to its exact parameters."""

import copy
import json
import pickle
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from merchant_assistant.agent.domain.errors import (
    AgentError,
    InvalidPendingAction,
    InvalidRegistration,
    StaleConfirmation,
)
from merchant_assistant.agent.domain.guards import ValidatedCall, authorize
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.agent.domain.pending import (
    Cancelled,
    Confirmed,
    Invalidated,
    NothingPending,
    PendingAction,
    PendingState,
    Proposed,
    RegisteredRef,
    decide,
    fingerprint,
    invalidate,
    mark_registered,
    propose,
)
from merchant_assistant.agent.domain.tools import (
    CONSULTAR_VENTAS,
    CREAR_RECLAMO,
    TOOL_SPECS,
    CrearReclamoArgs,
)

AT = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
EMPTY = PendingState()


def call(motivo: str, descripcion: str, *, raw: str | None = None) -> ValidatedCall:
    arguments = raw or json.dumps({"motivo": motivo, "descripcion": descripcion})
    return authorize(ToolCall("c1", CREAR_RECLAMO, arguments), TOOL_SPECS)


def action(
    motivo: str = "Entrada no recibida", descripcion: str = "Pagué y no llegó."
) -> PendingAction:
    return PendingAction.create(call(motivo, descripcion), AT)


def confirm(state: PendingState) -> tuple[PendingState, Confirmed]:
    after, decision = decide(state, "sí")
    assert isinstance(decision, Confirmed)
    return after, decision


# --------------------------------------------------------------------------- fingerprint


def test_the_fingerprint_ignores_extra_whitespace_and_key_order() -> None:
    tidy = call("Pago doble", "Me cobraron dos veces.")
    messy = call(
        "", "", raw='{ "descripcion" : "Me cobraron\\n  dos veces. " , "motivo":"  Pago   doble"}'
    )

    assert fingerprint(CREAR_RECLAMO, tidy.arguments) == fingerprint(CREAR_RECLAMO, messy.arguments)


@pytest.mark.parametrize(
    ("motivo", "descripcion"),
    [
        pytest.param("pago doble", "Me cobraron dos veces.", id="case of the reason"),
        pytest.param("Pago doble", "me cobraron dos veces.", id="case of the description"),
        pytest.param("Pago doble.", "Me cobraron dos veces.", id="punctuation of the reason"),
        pytest.param("Pago doble", "Me cobraron dos veces", id="punctuation of the description"),
        pytest.param("Pago doble", "Me cobraron tres veces.", id="one word"),
    ],
)
def test_the_fingerprint_changes_with_any_change_the_user_could_see(
    motivo: str, descripcion: str
) -> None:
    original = call("Pago doble", "Me cobraron dos veces.").arguments

    assert fingerprint(CREAR_RECLAMO, call(motivo, descripcion).arguments) != fingerprint(
        CREAR_RECLAMO, original
    )


def test_the_fingerprint_depends_on_the_tool() -> None:
    arguments = call("Pago doble", "Me cobraron dos veces.").arguments

    assert fingerprint(CREAR_RECLAMO, arguments) != fingerprint("otra_herramienta", arguments)


def test_the_fingerprint_is_a_sha256_hex_digest() -> None:
    digest = action().fingerprint

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


# --------------------------------------------------------------------------- pending action


def test_a_pending_action_is_created_with_its_own_fingerprint() -> None:
    pending = action("Pago doble", "Me cobraron dos veces.")

    assert pending.tool == CREAR_RECLAMO
    assert pending.fingerprint == fingerprint(CREAR_RECLAMO, pending.arguments)
    assert pending.proposed_at == AT


def test_a_read_tool_can_never_become_a_pending_action() -> None:
    lookup = authorize(ToolCall("c1", CONSULTAR_VENTAS, '{"fecha":"2026-10-03"}'), TOOL_SPECS)

    with pytest.raises(InvalidPendingAction):
        PendingAction.create(lookup, AT)


def test_a_pending_action_needs_an_aware_time() -> None:
    with pytest.raises(InvalidPendingAction):
        PendingAction.create(call("Pago doble", "Me cobraron dos veces."), AT.replace(tzinfo=None))


def test_a_pending_action_refuses_a_fingerprint_that_does_not_match() -> None:
    with pytest.raises(InvalidPendingAction):
        replace(action(), fingerprint="0" * 64)


def test_a_pending_action_refuses_arguments_that_skipped_validation() -> None:
    pending = action()
    forged = pending.arguments.model_copy(update={"motivo": "x"})

    with pytest.raises(InvalidPendingAction):
        PendingAction(CREAR_RECLAMO, forged, fingerprint(CREAR_RECLAMO, forged), AT)


def test_a_pending_action_refuses_arguments_that_are_not_normalized() -> None:
    raw = CrearReclamoArgs.model_construct(
        motivo="Pago\ndoble", descripcion="Me cobraron dos veces."
    )

    with pytest.raises(InvalidPendingAction):
        PendingAction(CREAR_RECLAMO, raw, fingerprint(CREAR_RECLAMO, raw), AT)


def test_a_pending_action_refuses_a_read_tool_built_by_hand() -> None:
    arguments = action().arguments

    with pytest.raises(InvalidPendingAction):
        PendingAction(CONSULTAR_VENTAS, arguments, fingerprint(CONSULTAR_VENTAS, arguments), AT)


# --------------------------------------------------------------------------- confirmed


def test_a_confirmed_cannot_be_built_outside_the_gate() -> None:
    with pytest.raises(TypeError):
        Confirmed(action())


def test_a_confirmed_cannot_be_built_with_a_made_up_proof() -> None:
    with pytest.raises(TypeError):
        Confirmed(action(), _proof=object())


def test_a_confirmed_cannot_be_changed_copied_or_serialized() -> None:
    _, confirmed = confirm(propose(EMPTY, action())[0])

    with pytest.raises(AttributeError):
        confirmed.action = action("Otro motivo", "Otra descripción larga.")
    with pytest.raises(TypeError):
        copy.copy(confirmed)
    with pytest.raises(TypeError):
        pickle.dumps(confirmed)


# --------------------------------------------------------------------------- scenarios

A = action("Entrada no recibida", "Pagué la entrada y no llegó.")
B = action("Entrada no llegó", "Pagué la entrada y no llegó.")  # the reason changed


def test_a_propose_then_yes_confirms_the_same_fingerprint() -> None:
    state, proposed = propose(EMPTY, A)
    _, confirmed = confirm(state)

    assert proposed == Proposed(action=A, superseded=None)
    assert confirmed.action.fingerprint == A.fingerprint


def test_b_a_normal_sentence_invalidates_and_a_later_yes_finds_nothing() -> None:
    state, _ = propose(EMPTY, A)

    state, first = decide(state, "¿cuánto tarda en resolverse?")
    state, second = decide(state, "sí")

    assert first == Invalidated(A)
    assert second == NothingPending(None)


def test_c_a_changed_proposal_supersedes_and_only_the_new_one_is_confirmed() -> None:
    state, _ = propose(EMPTY, A)
    state, proposed = propose(state, B)
    after, confirmed = confirm(state)

    assert proposed.superseded == A
    assert confirmed.action == B
    assert after.pending == B
    with pytest.raises(StaleConfirmation):
        mark_registered(propose(after, A)[0], confirmed, "REC-000001")


def test_d_yes_with_nothing_pending_is_nothing_pending() -> None:
    state, decision = decide(EMPTY, "sí")

    assert decision == NothingPending(None)
    assert state == EMPTY


def test_e_repeated_yes_after_registering_carries_the_reference() -> None:
    state, confirmed = confirm(propose(EMPTY, A)[0])
    state, registered = mark_registered(state, confirmed, "REC-000001")

    state, second = decide(state, "sí")
    state, third = decide(state, "Sí, dale")

    ref = RegisteredRef("REC-000001", A.fingerprint)
    assert registered.ref == ref
    assert second == NothingPending(ref)
    assert third == NothingPending(ref)


def test_f_no_cancels_and_a_later_yes_finds_nothing() -> None:
    state, _ = propose(EMPTY, A)

    state, first = decide(state, "no, gracias")
    state, second = decide(state, "sí")

    assert first == Cancelled(A)
    assert second == NothingPending(None)


def test_g_a_failed_execution_can_be_retried_with_the_same_fingerprint() -> None:
    state, first = confirm(propose(EMPTY, A)[0])
    # The execution failed: the application does not call mark_registered.
    state, second = confirm(state)

    assert first == second
    assert second.action.fingerprint == A.fingerprint


def test_h_invalidate_clears_the_pending_action() -> None:
    state, _ = propose(EMPTY, A)

    state, dropped = invalidate(state)
    _, decision = decide(state, "sí")

    assert dropped == Invalidated(A)
    assert decision == NothingPending(None)


def test_h_invalidate_with_nothing_pending_changes_nothing() -> None:
    assert invalidate(EMPTY) == (EMPTY, NothingPending(None))


def test_i_no_operation_mutates_the_state_it_was_given() -> None:
    proposed_state, _ = propose(EMPTY, A)
    snapshot = PendingState(pending=A)

    decide(proposed_state, "sí")
    decide(proposed_state, "no")
    decide(proposed_state, "otra cosa")
    invalidate(proposed_state)
    propose(proposed_state, B)
    mark_registered(proposed_state, confirm(proposed_state)[1], "REC-000001")

    assert proposed_state == snapshot
    with pytest.raises(AttributeError):
        proposed_state.pending = None  # type: ignore[misc]


def test_registering_requires_the_confirmation_of_what_is_pending_now() -> None:
    state, confirmed = confirm(propose(EMPTY, A)[0])
    cancelled, _ = decide(state, "no")

    with pytest.raises(StaleConfirmation):
        mark_registered(cancelled, confirmed, "REC-000001")


def test_a_confirmation_cannot_be_registered_twice() -> None:
    state, confirmed = confirm(propose(EMPTY, A)[0])
    state, _ = mark_registered(state, confirmed, "REC-000001")

    with pytest.raises(StaleConfirmation):
        mark_registered(state, confirmed, "REC-000002")


def test_registering_requires_a_complaint_identifier() -> None:
    state, confirmed = confirm(propose(EMPTY, A)[0])

    with pytest.raises(InvalidRegistration):
        mark_registered(state, confirmed, "  ")


def test_proposing_again_after_registering_can_be_confirmed_again() -> None:
    # The gate does not dedupe on its own: the store's idempotency key does, and answers
    # with the existing complaint instead of a new one.
    state, confirmed = confirm(propose(EMPTY, A)[0])
    state, _ = mark_registered(state, confirmed, "REC-000001")

    state, _ = propose(state, A)
    _, again = confirm(state)

    assert again.action.fingerprint == A.fingerprint


@pytest.mark.parametrize("error", [InvalidPendingAction, StaleConfirmation, InvalidRegistration])
def test_gate_refusals_belong_to_the_agent_family(error: type[AgentError]) -> None:
    assert issubclass(error, AgentError)


def test_gate_refusals_have_stable_codes() -> None:
    assert InvalidPendingAction.code == "invalid_pending_action"
    assert StaleConfirmation.code == "stale_confirmation"
    assert InvalidRegistration.code == "invalid_registration"
