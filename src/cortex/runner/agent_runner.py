"""The AgentRunner (brief section 2): model, tool request, verification by code, result, answer.

One run handles one message of one conversation. It keeps no state between runs: everything
that must survive lives in the database, and the per-run state lives in a `_Run` object that is
thrown away at the end.

The loop checks the control of the conversation at three points, because an operator can take
over at any moment:

- A, before every model call (a control change wins over the deadline, which wins over steps);
- B, before executing a tool;
- C, at delivery, where the check and the send happen in one transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import tzinfo
from types import MappingProxyType

from cortex.config import RunLimits
from cortex.identity import AuthContext
from cortex.memory.database import Database
from cortex.memory.ledger import PendingAction, PendingStatus
from cortex.memory.runs import Admission
from cortex.memory.tenant_store import ConversationState, TenantStore, check_external_id
from cortex.observability.tracer import TraceEvent, Tracer
from cortex.runner.budget import Budget, Ticker
from cortex.runner.finish import REASON_OF, FinishDetail, FinishReason
from cortex.runner.model import (
    FinalAnswer,
    InvalidModelOutput,
    ModelPort,
    ModelTurn,
    ModelUnavailable,
    Prompt,
    PromptMessage,
    ScriptExhausted,
)
from cortex.tools.catalog import Catalog, ToolKind
from cortex.tools.gateway import ToolAdapter, ToolBinder, ToolResult, WriteAdapter, execute
from cortex.tools.policy import (
    Approved,
    PolicyContext,
    Rejected,
    RejectionCode,
    display_name,
    review,
)
from cortex.tools.writes import (
    Done,
    Failed,
    NeedsConfirmation,
    Refused,
    WriteContext,
    WriteOutcome,
    write,
)
from merchant_assistant.agent.domain.messages import ToolCall
from merchant_assistant.agent.domain.replies import ReplyKind, classify_reply
from merchant_assistant.control.states import State
from merchant_assistant.shared.clock import Clock, business_today

_LOG = logging.getLogger(__name__)

# Written by the code, not by the model: the answer to an attempt to reach another company.
SCOPE_REFUSAL = "No puedo acceder a información de otra empresa."

_MODEL_FAILURES: Mapping[type[Exception], FinishDetail] = MappingProxyType(
    {
        ScriptExhausted: FinishDetail.SCRIPT_EXHAUSTED,
        InvalidModelOutput: FinishDetail.INVALID_MODEL_OUTPUT,
        ModelUnavailable: FinishDetail.MODEL_UNAVAILABLE,
    }
)


@dataclass(frozen=True, slots=True)
class RunRequest:
    run_id: str
    conversation_id: str
    message: str
    auth: AuthContext
    limits: RunLimits


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    tenant_id: str
    conversation_id: str
    finish_reason: FinishReason
    finish_detail: FinishDetail
    step_count: int
    max_steps: int
    events: tuple[TraceEvent, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "run_id": self.run_id,
                "tenant_id": self.tenant_id,
                "conversation_id": self.conversation_id,
                "finish_detail": self.finish_detail.value,
                "step_count": self.step_count,
                "max_steps": self.max_steps,
                "events": [event.as_dict() for event in self.events],
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, text: str) -> RunResult:
        data = json.loads(text)
        detail = FinishDetail(data["finish_detail"])
        return cls(
            run_id=data["run_id"],
            tenant_id=data["tenant_id"],
            conversation_id=data["conversation_id"],
            finish_reason=REASON_OF[detail],
            finish_detail=detail,
            step_count=data["step_count"],
            max_steps=data["max_steps"],
            events=tuple(TraceEvent.from_dict(event) for event in data["events"]),
        )


@dataclass(frozen=True, slots=True)
class AgentRunner:
    database: Database
    catalog: Catalog
    bind_tools: ToolBinder
    model: ModelPort
    clock: Clock
    ticker: Ticker
    business_tz: tzinfo

    def run(self, request: RunRequest) -> RunResult:
        check_external_id(request.run_id)
        check_external_id(request.conversation_id)
        return _Run(self, request).execute()


class _Run:
    """The state of one run. Never shared, never reused."""

    def __init__(self, runner: AgentRunner, request: RunRequest) -> None:
        self.runner = runner
        self.request = request
        self.budget = Budget(request.limits, runner.ticker)
        self.store: TenantStore = runner.database.for_tenant(request.auth)
        self.adapters: dict[str, ToolAdapter] = dict(runner.bind_tools(request.auth))
        # The tools offered in this run: the catalog's, limited to those bound to this company.
        self.catalog = runner.catalog.restricted_to(self.adapters)
        self.tracer = Tracer(
            run_id=request.run_id,
            tenant_id=request.auth.tenant_id,
            conversation_id=request.conversation_id,
            clock=runner.clock,
            elapsed_ms=self.budget.elapsed_ms,
        )
        self.base_version = 0

    # ------------------------------------------------------------------ the loop

    def execute(self) -> RunResult:
        """Admit the command once, run it, and store its result for any repetition."""
        request = self.request
        self._trace("run_started")
        conversation = self.store.open_conversation(request.conversation_id)
        digest = hashlib.sha256(request.message.encode("utf-8")).hexdigest()
        admission = self.store.runs.admit(request.run_id, request.conversation_id, digest)
        if admission.admission is Admission.REPLAY and admission.stored_result is not None:
            # The same command already ran: answer with its result, execute nothing.
            return RunResult.from_json(admission.stored_result)
        if admission.admission is Admission.CONFLICT:
            return self._finish(FinishDetail.RUN_ID_CONFLICT)
        if admission.admission is not Admission.ADMITTED:
            return self._finish(FinishDetail.CONVERSATION_BUSY)
        try:
            result = self._loop(conversation)
        except Exception:
            # A bug must not leave the run without an explicit ending. The traceback goes to the
            # log only; the result carries a code.
            _LOG.exception("run %s failed unexpectedly", request.run_id)
            result = self._finish(FinishDetail.INTERNAL_ERROR)
        self.store.runs.finish(request.run_id, request.conversation_id, result.to_json())
        return result

    def _loop(self, conversation: ConversationState) -> RunResult:
        conversation_id = self.request.conversation_id
        self.store.append_message(
            conversation_id, self.request.run_id, "user", self.request.message
        )
        if conversation.mode is not State.AUTO:
            # Nobody asks the model: the message is stored for the person in control.
            blocked = conversation.mode is State.BLOCKED
            return self._finish(
                FinishDetail.CONVERSATION_BLOCKED if blocked else FinishDetail.HUMAN_IN_CONTROL
            )
        self.base_version = conversation.control_version

        stop = self._answer_pending_confirmation()
        if stop is not None:
            return self._finish(stop)
        while True:
            stop = self._checkpoint_before_model()
            if stop is not None:
                return self._finish(stop)
            turn = self._ask_model()
            if isinstance(turn, FinishDetail):
                return self._finish(turn)
            if isinstance(turn, FinalAnswer):
                return self._deliver(turn.text)
            stop = self._use_tool(turn)
            if stop is not None:
                return self._finish(stop)

    # ------------------------------------------------------------------ checkpoints

    def _checkpoint_before_model(self) -> FinishDetail | None:
        if self._control_changed():
            return FinishDetail.OPERATOR_TOOK_OVER
        if self.budget.out_of_time():
            return FinishDetail.TOTAL_TIMEOUT
        if self.budget.out_of_steps():
            return FinishDetail.MAX_STEPS
        return None

    def _control_changed(self) -> bool:
        current = self._conversation()
        changed = current.mode is not State.AUTO or current.control_version != self.base_version
        if changed:
            self._trace("checkpoint_failed", mode=current.mode.value)
        return changed

    # ------------------------------------------------------------------ model

    def _ask_model(self) -> ModelTurn | FinishDetail:
        self.budget.take_step()
        self._trace("model_called")
        try:
            turn = self.runner.model.next_turn(
                self._prompt(), timeout_ms=self.budget.remaining_ms()
            )
        except (ScriptExhausted, InvalidModelOutput, ModelUnavailable) as failure:
            self._trace("model_failed", code=failure.code)
            return _MODEL_FAILURES[type(failure)]
        kind = "final_answer" if isinstance(turn, FinalAnswer) else "tool_request"
        self._trace("model_replied", kind=kind)
        return turn

    def _prompt(self) -> Prompt:
        history = self.store.recent_messages(
            self.request.conversation_id, self.request.limits.context_window_messages
        )
        conversation = self._conversation()
        return Prompt(
            messages=tuple(PromptMessage(message.role, message.content) for message in history),
            # Critical state travels structured, next to the history, and is never summarized.
            state={
                "conversation_mode": conversation.mode.value,
                "control_version": conversation.control_version,
                "pending_actions": [
                    _describe(action)
                    for action in self.store.ledger.open_actions(self.request.conversation_id)
                ],
            },
            tools=self.catalog.names,
        )

    # ------------------------------------------------------------------ tools

    def _use_tool(self, call: ToolCall) -> FinishDetail | None:
        self._trace("tool_requested", tool=display_name(call.name))
        verdict = review(
            call,
            self.catalog,
            PolicyContext(
                auth=self.request.auth,
                mode=self._conversation().mode,
                today=business_today(self.runner.clock, self.runner.business_tz),
            ),
        )
        if isinstance(verdict, Rejected):
            return self._reject(verdict)
        if self._control_changed():
            return FinishDetail.OPERATOR_TOOK_OVER
        if verdict.spec.kind is ToolKind.WRITE:
            return self._write(verdict)
        result = execute(verdict, self.adapters, conversation_id=self.request.conversation_id)
        self._record_result(result)
        return None

    # ------------------------------------------------------------------ writes

    def _answer_pending_confirmation(self) -> FinishDetail | None:
        """The user's reply to a confirmation question is read by code, before the model.

        A clear "sí" confirms the exact values at the current control version and runs the
        write; anything else cancels it, so a later "sí" can never confirm something stale.
        """
        waiting = [
            action
            for action in self.store.ledger.open_actions(self.request.conversation_id)
            if action.status is PendingStatus.PENDING and action.arguments is not None
        ]
        if not waiting:
            return None
        latest, older = waiting[-1], waiting[:-1]
        for action in older:
            self._cancel(action)
        if classify_reply(self.request.message) is not ReplyKind.AFFIRMATIVE:
            self._cancel(latest)
            return None
        self.store.ledger.record_pending(
            PendingAction(
                operation_id=latest.operation_id,
                conversation_id=latest.conversation_id,
                tool=latest.tool,
                arguments=latest.arguments,
                fingerprint=latest.fingerprint,
                control_version=self.base_version,
                status=PendingStatus.CONFIRMED,
            )
        )
        self._trace("confirmation_received", tool=latest.tool, operation_id=latest.operation_id)
        # The confirmed values go through the same verification as any request.
        call = ToolCall(f"confirmed-{latest.operation_id}", latest.tool, latest.arguments or "{}")
        return self._use_tool(call)

    def _cancel(self, action: PendingAction) -> None:
        self.store.ledger.move_pending(
            action.operation_id,
            from_status=PendingStatus.PENDING,
            to_status=PendingStatus.CANCELLED,
        )
        self._trace("confirmation_cancelled", tool=action.tool, operation_id=action.operation_id)

    def _write(self, approved: Approved) -> FinishDetail | None:
        adapter = self.adapters[approved.spec.name]
        if not isinstance(adapter, WriteAdapter):
            raise TypeError(f"{approved.spec.name} is a write tool without a lookup")
        outcome = write(
            approved,
            adapter,
            WriteContext(
                ledger=self.store.ledger,
                conversation_id=self.request.conversation_id,
                control_version=self.base_version,
                run_id=self.request.run_id,
            ),
        )
        return self._apply_write_outcome(approved, outcome)

    def _apply_write_outcome(
        self, approved: Approved, outcome: WriteOutcome
    ) -> FinishDetail | None:
        name = approved.spec.name
        if isinstance(outcome, Done):
            if outcome.how == "executed":
                self._record_result(outcome.result)
            else:
                event = "tool_deduplicated" if outcome.how == "deduplicated" else "write_reconciled"
                self._trace(event, tool=name, data_tenant=outcome.result.source_tenant)
                self._remember_tool_message(outcome.result.envelope())
            return None
        if isinstance(outcome, NeedsConfirmation):
            self._trace("tool_confirmation_required", tool=name, operation_id=outcome.operation_id)
            if self._deliver_text(outcome.question):
                return FinishDetail.AWAITING_CONFIRMATION
            return FinishDetail.OPERATOR_TOOK_OVER
        if isinstance(outcome, Refused):
            return self._reject(Rejected(name, RejectionCode(outcome.code), outcome.detail))
        if isinstance(outcome, Failed):
            self._record_result(outcome.result)
            return None
        self._trace("write_uncertain", tool=name, operation_id=outcome.operation_id)
        return FinishDetail.WRITE_OUTCOME_UNKNOWN

    def _reject(self, rejection: Rejected) -> FinishDetail | None:
        self._trace(
            "tool_rejected",
            tool=rejection.tool,
            reason=rejection.code.value,
            detail=rejection.detail,
        )
        envelope = {
            "untrusted_data": True,
            "tool": rejection.tool,
            "status": "rejected",
            "error": {"code": rejection.code.value, "detail": rejection.detail},
        }
        self._remember_tool_message(envelope)
        if rejection.ends_run:
            self._deliver_text(SCOPE_REFUSAL)
            return FinishDetail.SCOPE_VIOLATION
        return None

    def _record_result(self, result: ToolResult) -> None:
        if result.ok:
            self._trace("tool_executed", tool=result.tool, data_tenant=result.source_tenant)
        else:
            code = result.error.get("code") if result.error else None
            self._trace(
                "tool_error", tool=result.tool, reason=code, data_tenant=result.source_tenant
            )
        self._remember_tool_message(result.envelope())

    def _remember_tool_message(self, envelope: dict[str, object]) -> None:
        self.store.append_message(
            self.request.conversation_id,
            self.request.run_id,
            "tool",
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
        )

    # ------------------------------------------------------------------ delivery and ending

    def _deliver(self, text: str) -> RunResult:
        if self._deliver_text(text):
            return self._finish(FinishDetail.ANSWERED)
        return self._finish(FinishDetail.OPERATOR_TOOK_OVER)

    def _deliver_text(self, text: str) -> bool:
        delivered = self.store.deliver_if_current(
            self.request.conversation_id, self.request.run_id, text, self.base_version
        )
        self._trace("final_answer" if delivered else "reply_suppressed", chars=len(text))
        return delivered

    def _finish(self, detail: FinishDetail) -> RunResult:
        reason = REASON_OF[detail]
        self._trace("run_finished", finish_reason=reason.value, finish_detail=detail.value)
        return RunResult(
            run_id=self.request.run_id,
            tenant_id=self.request.auth.tenant_id,
            conversation_id=self.request.conversation_id,
            finish_reason=reason,
            finish_detail=detail,
            step_count=self.budget.steps_taken,
            max_steps=self.request.limits.max_steps,
            events=self.tracer.events,
        )

    # ------------------------------------------------------------------ helpers

    def _conversation(self) -> ConversationState:
        conversation = self.store.conversation(self.request.conversation_id)
        if conversation is None:
            raise RuntimeError("the conversation of a running run disappeared")
        return conversation

    def _trace(self, type_: str, **attributes: str | int | bool | None) -> None:
        self.tracer.emit(type_, step=self.budget.steps_taken, **attributes)


def _describe(action: PendingAction) -> dict[str, object]:
    """A pending action as the model sees it: exact values, never a summary."""
    return {
        "tool": action.tool,
        "operation_id": action.operation_id,
        "status": action.status.value,
        "arguments": json.loads(action.arguments) if action.arguments else None,
    }
