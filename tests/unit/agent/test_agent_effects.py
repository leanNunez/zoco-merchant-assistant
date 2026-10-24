"""One function executes the effects of a transition, for the turn and for the commands."""

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from merchant_assistant.agent.adapters.scripted_model import tool_call
from merchant_assistant.agent.domain.drafts import Draft, DraftKind, DraftStatus
from merchant_assistant.agent.domain.effects import apply_effects
from merchant_assistant.agent.domain.guards import authorize
from merchant_assistant.agent.domain.pending import PendingAction, PendingState, propose
from merchant_assistant.agent.domain.state import ConversationState, OperatorNotice
from merchant_assistant.agent.domain.tools import CREAR_RECLAMO, TOOL_SPECS
from merchant_assistant.control import Actor, Effect, Event, Outcome, State

AT = datetime(2026, 10, 23, 12, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
ACTION = PendingAction.create(
    authorize(
        tool_call(CREAR_RECLAMO, {"motivo": "Pago doble", "descripcion": "Me cobraron dos veces."}),
        TOOL_SPECS,
    ),
    AT,
    turn=1,
    control_version=0,
)


def busy_state() -> ConversationState:
    pending, _ = propose(PendingState(), ACTION)
    drafts = (Draft("D-0001", DraftKind.REPLY, "Borrador.", AT, 1),)
    return replace(ConversationState("conv-1"), pending=pending, drafts=drafts)


def outcome(previous: State, event: Event, new: State, *effects: Effect) -> Outcome:
    return Outcome(previous, new, event, Actor.SYSTEM, frozenset(effects))


def test_leaving_suggest_drops_the_pending_action_and_discards_every_draft() -> None:
    state, facts = apply_effects(
        busy_state(),
        outcome(
            State.SUGGEST,
            Event.OPERATOR_TAKEOVER,
            State.HUMAN,
            Effect.INVALIDATE_PENDING_ACTION,
            Effect.DISCARD_UNAPPROVED_DRAFTS,
        ),
        at=AT,
    )

    assert state.pending.pending is None
    assert [draft.status for draft in state.drafts] == [DraftStatus.DISCARDED]
    assert facts == (
        "pending_action_invalidated",
        "discard_unapproved_drafts",
        "draft_discarded:D-0001",
    )


def test_abuse_in_human_writes_to_the_operator_inbox_and_keeps_everything_else() -> None:
    before = busy_state()

    state, facts = apply_effects(
        before,
        outcome(State.HUMAN, Event.ABUSE_DETECTED, State.HUMAN, Effect.NOTIFY_OPERATOR),
        at=AT,
    )

    assert state.inbox == (OperatorNotice(AT, "abuse_detected", "state=HUMAN"),)
    assert (state.pending, state.drafts) == (before.pending, before.drafts)
    assert facts == ("notify_operator",)


def test_an_outcome_without_effects_changes_nothing() -> None:
    before = busy_state()

    state, facts = apply_effects(
        before, outcome(State.AUTO, Event.OUT_OF_HOURS_STARTED, State.AUTO), at=AT
    )

    assert (state, facts) == (before, ())


def test_invalidating_with_nothing_pending_reports_nothing() -> None:
    state, facts = apply_effects(
        ConversationState("conv-1"),
        outcome(State.AUTO, Event.OPERATOR_TAKEOVER, State.HUMAN, Effect.INVALIDATE_PENDING_ACTION),
        at=AT,
    )

    assert facts == ()
    assert state == ConversationState("conv-1")
