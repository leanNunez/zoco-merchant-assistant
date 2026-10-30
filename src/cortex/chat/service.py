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
from cortex.observability.redaction import TraceRedactor
from cortex.observability.tracer import Tracer
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import Clock, business_today


class RunIdConflict(Exception):  # noqa: N818 - named after the situation
    """The run id was already used for another conversation or another event."""


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
    redactor: TraceRedactor

    def take_over(self, auth: AuthContext, conversation_id: str, event: Takeover) -> TakeoverResult:
        if event.actor is not None:
            # The person taking over must belong to the same company. Raises AuthenticationFailed.
            self.directory.authenticate(auth.tenant_id, event.actor)
        responsible = event.actor or auth.principal_id
        store = self.database.for_tenant(auth)
        store.open_conversation(conversation_id)
        applied = store.chat.apply_takeover(conversation_id, event.version, responsible)
        return TakeoverResult(applied, event.version, responsible)

    def tracer(self, auth: AuthContext, conversation_id: str, run_id: str) -> Tracer:
        """A tracer for an event handled outside a run, written through to the company's traces."""
        store = self.database.for_tenant(auth)
        store.open_conversation(conversation_id)
        tracer = Tracer(
            run_id=run_id,
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
            clock=self.clock,
            elapsed_ms=lambda: 0,
            redactor=self.redactor,
        )
        tracer.attach(store.traces.write)
        return tracer

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
        digest = facts.digest()
        previous = store.chat.decision_of(run_id)
        if previous is None:
            decision = decide(
                facts,
                today=business_today(self.clock, self.business_tz),
                human_in_control=conversation.mode is not State.AUTO,
            )
            recorded = store.chat.record_followup(
                conversation_id,
                run_id,
                digest,
                decision.decision,
                decision.reasons,
                _owed(decision),
            )
            if recorded:
                tracer.emit(
                    "followup_decided",
                    step=0,
                    decision=decision.decision.value,
                    reasons=",".join(decision.reasons),
                )
                return decision
            # Another handler of the same event decided first, between our read and our insert.
            previous = store.chat.decision_of(run_id)
            if previous is None:
                raise RuntimeError("a follow-up decision vanished after it was found")
        if (previous.conversation_id, previous.event_digest) != (conversation_id, digest):
            # The same id for another conversation or another event is not a repetition.
            raise RunIdConflict(run_id)
        # The same event, handled again: answer with what was decided, change nothing.
        tracer.emit("followup_replayed", step=0, decision=previous.decision)
        return FollowupDecision(Decision(previous.decision), previous.reasons)


def _owed(decision: FollowupDecision) -> Commitment | None:
    """What is owed after the decision: Zoco's task, or the date the customer chose."""
    if decision.decision is Decision.TAREA_INTERNA:
        return Commitment("internal_task", decision.responsible, None)
    if decision.decision is Decision.REPROGRAMAR and decision.reschedule_to is not None:
        return Commitment("contact_date", decision.responsible, decision.reschedule_to.isoformat())
    return None
