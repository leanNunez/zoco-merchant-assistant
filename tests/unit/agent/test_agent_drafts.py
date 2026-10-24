"""Drafts wait for one decision; leaving SUGGEST discards the ones nobody decided."""

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from merchant_assistant.agent.adapters.scripted_model import tool_call
from merchant_assistant.agent.domain.drafts import (
    Draft,
    DraftKind,
    DraftStatus,
    decide,
    discard_pending,
    find_pending,
    next_draft_id,
    pending_drafts,
)
from merchant_assistant.agent.domain.errors import DraftNotPending, InvalidDraft, UnknownDraft
from merchant_assistant.agent.domain.guards import authorize
from merchant_assistant.agent.domain.pending import Confirmed, PendingAction, PendingState, propose
from merchant_assistant.agent.domain.pending import decide as gate
from merchant_assistant.agent.domain.tools import CREAR_RECLAMO, TOOL_SPECS
from merchant_assistant.control import Actor

AT = datetime(2026, 10, 23, 12, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
LATER = datetime(2026, 10, 23, 12, 5, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
ACTION = PendingAction.create(
    authorize(
        tool_call(CREAR_RECLAMO, {"motivo": "Pago doble", "descripcion": "Me cobraron dos veces."}),
        TOOL_SPECS,
    ),
    AT,
    turn=1,
    control_version=0,
)


def reply(
    draft_id: str = "D-0001",
    status: DraftStatus = DraftStatus.PENDING,
    decided_by: Actor | None = None,
    decided_at: datetime | None = None,
) -> Draft:
    return Draft(
        draft_id,
        DraftKind.REPLY,
        "Borrador.",
        AT,
        1,
        status=status,
        decided_by=decided_by,
        decided_at=decided_at,
    )


def confirmed_for(action: PendingAction) -> Confirmed:
    pending, _ = propose(PendingState(), action)
    _, decision = gate(pending, "sí", turn=2, control_version=0)
    assert isinstance(decision, Confirmed)
    return decision


def test_identifiers_are_numbered_per_conversation_and_never_reused() -> None:
    assert next_draft_id(()) == "D-0001"
    assert next_draft_id(
        (
            reply("D-0001"),
            reply("D-0002", status=DraftStatus.DISCARDED, decided_by=Actor.SYSTEM, decided_at=AT),
        )
    ) == ("D-0003")


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: Draft("D-1", DraftKind.REPLY, "  ", AT, 1), id="blank text"),
        pytest.param(lambda: Draft(" ", DraftKind.REPLY, "x", AT, 1), id="blank id"),
        pytest.param(
            lambda: Draft("D-1", DraftKind.REPLY, "x", AT.replace(tzinfo=None), 1), id="naive time"
        ),
        pytest.param(
            lambda: Draft("D-1", DraftKind.CONFIRMATION_REQUEST, "x", AT, 1),
            id="question without its action",
        ),
        pytest.param(
            lambda: Draft("D-1", DraftKind.REPLY, "x", AT, 1, action=ACTION),
            id="reply with an action",
        ),
        pytest.param(
            lambda: Draft("D-1", DraftKind.ACTION_APPROVAL, "x", AT, 1),
            id="approval without the confirmation",
        ),
        pytest.param(
            lambda: Draft("D-1", DraftKind.REPLY, "x", AT, 1, decided_by=Actor.OPERATOR),
            id="pending with a decider",
        ),
        pytest.param(
            lambda: Draft(
                "D-1",
                DraftKind.REPLY,
                "x",
                AT,
                1,
                status=DraftStatus.APPROVED,
                decided_by=Actor.CUSTOMER,
                decided_at=AT,
            ),
            id="approved by the customer",
        ),
        pytest.param(
            lambda: Draft(
                "D-1",
                DraftKind.REPLY,
                "x",
                AT,
                1,
                status=DraftStatus.APPROVED,
                decided_by=Actor.SYSTEM,
                decided_at=AT,
            ),
            id="approved by the system",
        ),
        pytest.param(
            lambda: Draft(
                "D-1",
                DraftKind.REPLY,
                "x",
                AT,
                1,
                status=DraftStatus.DISCARDED,
                decided_by=Actor.OPERATOR,
                decided_at=AT,
            ),
            id="discarded by an operator",
        ),
    ],
)
def test_a_draft_whose_kind_payload_and_decision_disagree_is_refused(
    build: Callable[[], Draft],
) -> None:
    with pytest.raises(InvalidDraft):
        build()


def test_an_action_approval_carries_the_confirmation_the_gate_issued() -> None:
    draft = Draft(
        "D-1", DraftKind.ACTION_APPROVAL, "Aprobar.", AT, 2, confirmed=confirmed_for(ACTION)
    )

    assert draft.confirmed is not None
    assert draft.confirmed.action == ACTION


@pytest.mark.parametrize("status", [DraftStatus.APPROVED, DraftStatus.REJECTED])
def test_a_decision_records_who_and_when_and_touches_no_other_draft(status: DraftStatus) -> None:
    drafts = (reply("D-0001"), reply("D-0002"))

    decided = decide(drafts, "D-0002", status, actor=Actor.SUPERVISOR, at=LATER)

    assert decided[0] == drafts[0]
    assert (decided[1].status, decided[1].decided_by, decided[1].decided_at) == (
        status,
        Actor.SUPERVISOR,
        LATER,
    )
    assert pending_drafts(decided) == (drafts[0],)


def test_a_draft_is_decided_once() -> None:
    drafts = decide((reply(),), "D-0001", DraftStatus.REJECTED, actor=Actor.OPERATOR, at=LATER)

    with pytest.raises(DraftNotPending):
        decide(drafts, "D-0001", DraftStatus.APPROVED, actor=Actor.OPERATOR, at=LATER)


def test_an_unknown_draft_is_refused_without_echoing_long_input() -> None:
    with pytest.raises(UnknownDraft) as refused:
        find_pending((reply(),), "D-9" + "x" * 500)

    assert len(refused.value.detail) < 80
    assert refused.value.public() == {
        "code": "unknown_draft",
        "message": "There is no such draft in this conversation.",
    }


def test_discarding_marks_every_pending_draft_and_keeps_them_on_record() -> None:
    approved = reply(
        "D-0001", status=DraftStatus.APPROVED, decided_by=Actor.OPERATOR, decided_at=AT
    )
    drafts = (approved, reply("D-0002"), reply("D-0003"))

    kept, discarded = discard_pending(drafts, at=LATER)

    assert discarded == ("D-0002", "D-0003")
    assert [draft.status for draft in kept] == [
        DraftStatus.APPROVED,
        DraftStatus.DISCARDED,
        DraftStatus.DISCARDED,
    ]
    assert kept[1].decided_by is Actor.SYSTEM
    assert pending_drafts(kept) == ()
    with pytest.raises(DraftNotPending):
        find_pending(kept, "D-0002")
