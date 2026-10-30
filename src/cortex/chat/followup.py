"""What to do with a due follow-up (brief section 6). Pure: a decision table, no I/O.

The first rule that applies wins, and every cancellation reason that applies is recorded. The
model is never asked and no message is sent: the decision is recorded, together with what is now
owed (an internal task, or the date the customer chose). There is no scheduler.

Anything the table does not recognize fails closed: an opportunity status that is neither open
nor closed, or consent that is not known, never leads to contacting the customer.

| # | Condition                                    | Decision      | Reason                     |
|---|----------------------------------------------|---------------|----------------------------|
| 1 | the customer opted out                       | CANCELAR      | OPTED_OUT                  |
| 2 | the opportunity is closed (signup done...)   | CANCELAR      | OPPORTUNITY_CLOSED         |
| 3 | the customer replied                         | CANCELAR      | CUSTOMER_REPLIED           |
| 4 | Zoco owes something (pending_on ZOCO)        | TAREA_INTERNA | ZOCO_OWES_ACTION           |
| 5 | the status is neither open nor closed        | TAREA_INTERNA | UNKNOWN_OPPORTUNITY_STATUS |
| 6 | contact is not allowed                       | TAREA_INTERNA | CONTACT_NOT_ALLOWED        |
|   | consent is unknown                           | TAREA_INTERNA | CONSENT_UNKNOWN            |
| 7 | the customer agreed a later contact date     | REPROGRAMAR   | CUSTOMER_REQUESTED_LATER   |
| 8 | a person controls the conversation           | TAREA_INTERNA | HUMAN_IN_CONTROL           |
| 9 | otherwise                                    | CONTACTAR     | FOLLOWUP_DUE               |
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from cortex.chat.events import FollowupFacts

CLOSED_STATUSES = frozenset({"COMPLETED", "WON", "CLOSED", "LOST", "CANCELLED"})
OPEN_STATUS = "OPEN"


class Decision(StrEnum):
    CONTACTAR = "CONTACTAR"
    TAREA_INTERNA = "TAREA_INTERNA"
    REPROGRAMAR = "REPROGRAMAR"
    CANCELAR = "CANCELAR"


@dataclass(frozen=True, slots=True)
class FollowupDecision:
    decision: Decision
    reasons: tuple[str, ...]
    responsible: str | None = None
    reschedule_to: date | None = None


def decide(facts: FollowupFacts, *, today: date, human_in_control: bool) -> FollowupDecision:
    cancellations = tuple(
        reason
        for applies, reason in (
            (facts.opted_out, "OPTED_OUT"),
            (facts.opportunity_status in CLOSED_STATUSES, "OPPORTUNITY_CLOSED"),
            (facts.customer_replied, "CUSTOMER_REPLIED"),
        )
        if applies
    )
    if cancellations:
        return FollowupDecision(Decision.CANCELAR, cancellations)
    internal = _internal_task_reason(facts)
    if internal is not None:
        return FollowupDecision(Decision.TAREA_INTERNA, (internal,), facts.responsible)
    if facts.agreed_contact_date is not None and facts.agreed_contact_date > today:
        return FollowupDecision(
            Decision.REPROGRAMAR,
            ("CUSTOMER_REQUESTED_LATER",),
            facts.responsible,
            reschedule_to=facts.agreed_contact_date,
        )
    if human_in_control:
        return FollowupDecision(Decision.TAREA_INTERNA, ("HUMAN_IN_CONTROL",), facts.responsible)
    return FollowupDecision(Decision.CONTACTAR, ("FOLLOWUP_DUE",), facts.responsible)


def _internal_task_reason(facts: FollowupFacts) -> str | None:
    """Rules 4 to 6: why the customer must not be contacted (nor rescheduled), if any."""
    if facts.pending_on == "ZOCO":
        return "ZOCO_OWES_ACTION"
    if facts.opportunity_status not in (None, OPEN_STATUS):
        return "UNKNOWN_OPPORTUNITY_STATUS"
    if facts.contact_allowed is False:
        return "CONTACT_NOT_ALLOWED"
    if facts.contact_allowed is None:
        return "CONSENT_UNKNOWN"
    return None
