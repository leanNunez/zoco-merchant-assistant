"""Applies Zoco Chat events to a conversation (brief section 6).

A takeover is a fact Zoco Chat reports: the person already has the conversation. It is applied
when its version is newer than the stored one, with no attention-hours guard (the first
delivery's state machine required open hours, which would make the outcome depend on the time of
day). A follow-up is decided by code from the event and the stored control state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo

from cortex.chat.events import FollowupFacts, Takeover
from cortex.chat.followup import Decision, FollowupDecision, decide
from cortex.identity import AuthContext, PrincipalDirectory
from cortex.memory.chat_records import Commitment
from cortex.memory.database import Database
from cortex.observability.tracer import Tracer
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import Clock, business_today


@dataclass(frozen=True, slots=True)
class TakeoverResult:
    applied: bool  # False: the event was not newer than the stored state, so it changed nothing
    version: int
    responsible: str


@dataclass(frozen=True, slots=True)
class ChatEvents:
    database: Database
    directory: PrincipalDirectory
    clock: Clock
    business_tz: tzinfo

    def take_over(self, auth: AuthContext, conversation_id: str, event: Takeover) -> TakeoverResult:
        if event.actor is not None:
            # The person taking over must belong to the same company. Raises AuthenticationFailed.
            self.directory.authenticate(auth.tenant_id, event.actor)
        responsible = event.actor or auth.principal_id
        store = self.database.for_tenant(auth)
        store.open_conversation(conversation_id)
        applied = store.chat.apply_takeover(conversation_id, event.version, responsible)
        return TakeoverResult(applied, event.version, responsible)

    def follow_up(
        self,
        auth: AuthContext,
        conversation_id: str,
        run_id: str,
        facts: FollowupFacts,
        tracer: Tracer,
    ) -> FollowupDecision:
        store = self.database.for_tenant(auth)
        conversation = store.open_conversation(conversation_id)
        decision = decide(
            facts,
            today=business_today(self.clock, self.business_tz),
            human_in_control=conversation.mode is not State.AUTO,
        )
        store.chat.record_followup(
            conversation_id, run_id, decision.decision, decision.reasons, _owed(decision)
        )
        tracer.emit(
            "followup_decided",
            step=0,
            decision=decision.decision.value,
            reasons=",".join(decision.reasons),
        )
        return decision


def _owed(decision: FollowupDecision) -> Commitment | None:
    """What is owed after the decision: Zoco's task, or the date the customer chose."""
    if decision.decision is Decision.TAREA_INTERNA:
        return Commitment("internal_task", decision.responsible, None)
    if decision.decision is Decision.REPROGRAMAR and decision.reschedule_to is not None:
        return Commitment("contact_date", decision.responsible, decision.reschedule_to.isoformat())
    return None
