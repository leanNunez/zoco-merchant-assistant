"""The follow-up decision table, row by row, and the kit's fixtures E02 to E05."""

from datetime import date
from typing import Any

import pytest

from cortex.chat.events import FollowupFacts
from cortex.chat.followup import Decision, decide

TODAY = date(2026, 10, 28)


def _decide(facts: dict[str, Any], *, human: bool = False) -> tuple[Decision, tuple[str, ...]]:
    outcome = decide(FollowupFacts(**facts), today=TODAY, human_in_control=human)
    return outcome.decision, outcome.reasons


@pytest.mark.parametrize(
    ("facts", "decision", "reasons"),
    [
        # E02 and T12: Zoco owes a simulation.
        (
            {
                "pending_on": "ZOCO",
                "responsible": "Benjamin",
                "contact_allowed": True,
                "opportunity_status": "OPEN",
            },
            Decision.TAREA_INTERNA,
            ("ZOCO_OWES_ACTION",),
        ),
        # E03: the customer asked to be contacted later.
        (
            {
                "pending_on": "CUSTOMER",
                "agreed_contact_date": date(2026, 11, 6),
                "contact_allowed": True,
            },
            Decision.REPROGRAMAR,
            ("CUSTOMER_REQUESTED_LATER",),
        ),
        # E04: signup completed.
        (
            {"opportunity_status": "COMPLETED", "contact_allowed": True},
            Decision.CANCELAR,
            ("OPPORTUNITY_CLOSED",),
        ),
        # E05: opted out.
        ({"contact_allowed": False, "opted_out": True}, Decision.CANCELAR, ("OPTED_OUT",)),
        # T13: both at once; every cancellation reason is recorded.
        (
            {"opportunity_status": "COMPLETED", "contact_allowed": False, "opted_out": True},
            Decision.CANCELAR,
            ("OPTED_OUT", "OPPORTUNITY_CLOSED"),
        ),
        (
            {"customer_replied": True, "contact_allowed": True},
            Decision.CANCELAR,
            ("CUSTOMER_REPLIED",),
        ),
        # No fixture produces CONTACTAR: the default case.
        (
            {"pending_on": "CUSTOMER", "contact_allowed": True, "opportunity_status": "OPEN"},
            Decision.CONTACTAR,
            ("FOLLOWUP_DUE",),
        ),
    ],
)
def test_each_case_of_the_kit(
    facts: dict[str, object], decision: Decision, reasons: tuple[str, ...]
) -> None:
    assert _decide(facts) == (decision, reasons)


def test_cancelling_wins_over_what_zoco_owes() -> None:
    assert _decide({"pending_on": "ZOCO", "opted_out": True})[0] is Decision.CANCELAR


def test_unknown_consent_never_allows_contact() -> None:
    assert _decide({"pending_on": "CUSTOMER"}) == (Decision.TAREA_INTERNA, ("CONTACT_NOT_ALLOWED",))


def test_a_person_in_control_turns_a_contact_into_an_internal_task() -> None:
    assert _decide({"contact_allowed": True}, human=True) == (
        Decision.TAREA_INTERNA,
        ("HUMAN_IN_CONTROL",),
    )


@pytest.mark.parametrize("agreed", [date(2026, 10, 28), date(2026, 10, 1)])
def test_a_date_that_already_arrived_means_contact_now(agreed: date) -> None:
    assert (
        _decide({"agreed_contact_date": agreed, "contact_allowed": True})[0] is Decision.CONTACTAR
    )


def test_the_rescheduled_date_and_the_responsible_travel_with_the_decision() -> None:
    outcome = decide(
        FollowupFacts(
            agreed_contact_date=date(2026, 11, 6), contact_allowed=True, responsible="Ana"
        ),
        today=TODAY,
        human_in_control=False,
    )

    assert (outcome.reschedule_to, outcome.responsible) == (date(2026, 11, 6), "Ana")
