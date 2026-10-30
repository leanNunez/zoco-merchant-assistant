"""The AgentRunner (brief section 2): model, tool request, verification by code, result, answer.

One run handles one message of one conversation. It keeps no state between runs: everything
that must survive lives in the database, and the per-run state lives in a `_Run` object that is
thrown away at the end.

The loop checks the control of the conversation at three points, because an operator can take
over at any moment:

- A, before every model call (a control change wins over the deadline, which wins over steps);
- B, before verifying and executing a tool;
- C, at delivery, where the check and the send happen in one transaction.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import tzinfo
from types import MappingProxyType

from cortex.config import MAX_ANSWER_CHARS, MAX_MESSAGE_CHARS, RunLimits
from cortex.identity import AuthContext
from cortex.memory.database import Database
from cortex.memory.ledger import PendingAction, PendingStatus
from cortex.memory.runs import Admission
from cortex.memory.tenant_store import (
    ConversationState,
    StoredMessage,
    TenantStore,
    check_external_id,
)
from cortex.observability.redaction import TraceRedactor
from cortex.observability.tracer import TraceEvent, Tracer
from cortex.runner.budget import Budget
from cortex.runner.finish import REASON_OF, FinishDetail, FinishReason, missing_failure_event
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
from cortex.timekeeping import Ticker
from cortex.tools.catalog import Catalog, ToolKind
from cortex.tools.failures import ToolFailure
from cortex.tools.gateway import ToolAdapter, ToolBinder, ToolResult, WriteAdapter, execute
from cortex.tools.policy import (
    Approved,
    PolicyContext,
    Rejected,
    display_name,
    review,
)
from cortex.tools.retry import Retrier, jitter_for
from cortex.tools.timeouts import time_limited
from cortex.tools.writes import (
    ControlChanged,
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

# Tool attempts run in a pool of this many worker threads per run, so that each can be abandoned
# at its time limit. An abandoned attempt cannot be killed (Python has no way): it keeps its
# thread until it returns on its own, so abandoned attempts stay in this pool. The pool does not
# grow; an attempt that has to wait for a free thread is still bound by its own time limit.
MAX_TOOL_WORKERS = 4

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
    # The tools this run may use; None means the whole catalog. It can only narrow what the
    # company's adapters offer: a tool outside it is rejected as tool_not_allowed.
    tools: frozenset[str] | None = None
    # The control version the caller saw. A run that finds another one is refused (BLOCKED,
    # stale_version) before the model is asked; None means the caller does not check it.
    expected_control_version: int | None = None


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
    redactor: TraceRedactor

    def run(self, request: RunRequest) -> RunResult:
        check_external_id(request.run_id)
        check_external_id(request.conversation_id)
        _check_encodable(request.message)
        return _Run(self, request).execute()


class _Run:
    """The state of one run. Never shared, never reused."""

    def __init__(self, runner: AgentRunner, request: RunRequest) -> None:
        self.runner = runner
        self.request = request
        self.budget = Budget(request.limits, runner.ticker)
        self.store: TenantStore = runner.database.for_tenant(request.auth)
        # Tool attempts run in worker threads so that each one can be abandoned at its time limit.
        self.executor = ThreadPoolExecutor(
            max_workers=MAX_TOOL_WORKERS, thread_name_prefix=request.run_id
        )
        # A tool outside the request's list is not even bound: the policy then rejects it as
        # not allowed, and nothing can reach its adapter.
        self.adapters: dict[str, ToolAdapter] = {
            name: time_limited(adapter, self.executor, self.budget.attempt_timeout_ms)
            for name, adapter in runner.bind_tools(request.auth).items()
            if request.tools is None or name in request.tools
        }
        self.jitter = jitter_for(request.run_id)
        # The tools offered in this run: the catalog's, limited to those bound to this company.
        self.catalog = runner.catalog.restricted_to(self.adapters)
        self.tracer = Tracer(
            run_id=request.run_id,
            tenant_id=request.auth.tenant_id,
            conversation_id=request.conversation_id,
            clock=runner.clock,
            elapsed_ms=self.budget.elapsed_ms,
            redactor=runner.redactor,
        )
        self.base_version = 0
        # The message this run answers: every delivery of the run is a reply to it.
        self.inbound_message_id: int | None = None

    # ------------------------------------------------------------------ the loop

    def execute(self) -> RunResult:
        """Admit the command once, run it, and store its result for any repetition."""
        request = self.request
        self._trace("run_started", principal=self.tracer.principal(request.auth.principal_id))
        conversation = self.store.open_conversation(request.conversation_id)
        digest = hashlib.sha256(request.message.encode("utf-8")).hexdigest()
        admission = self.store.runs.admit(request.run_id, request.conversation_id, digest)
        if admission.admission is Admission.REPLAY and admission.stored_result is not None:
            # The same command already ran: answer with its result, execute nothing.
            return RunResult.from_json(admission.stored_result)
        if admission.admission is Admission.CONFLICT:
            return self._finish(FinishDetail.RUN_ID_CONFLICT)
        if admission.admission is Admission.INTERRUPTED:
            return self._finish(FinishDetail.RUN_INTERRUPTED)
        if admission.admission is not Admission.ADMITTED:
            return self._finish(FinishDetail.CONVERSATION_BUSY)
        # Only an admitted run writes its trace: a replay or a refusal must not collide with it.
        self.tracer.attach(self.store.traces.write)
        try:
            result = self._loop(conversation)
        except Exception as error:
            # A bug must not leave the run without an explicit ending. The log names the run and
            # the kind of error, never the message (it may quote user data); the result carries a
            # code.
            _LOG.error("run %s failed unexpectedly: %s", request.run_id, type(error).__name__)
            result = self._finish(FinishDetail.INTERNAL_ERROR)
        finally:
            # A call abandoned at its time limit may still be running; nothing waits for it.
            self.executor.shutdown(wait=False, cancel_futures=True)
        self.store.runs.finish(request.run_id, request.conversation_id, result.to_json())
        return result

    def _loop(self, conversation: ConversationState) -> RunResult:
        conversation_id = self.request.conversation_id
        if len(self.request.message) > MAX_MESSAGE_CHARS:
            # Refused at admission: the text is neither stored nor shown to the model.
            return self._finish(FinishDetail.MESSAGE_TOO_LONG)
        expected = self.request.expected_control_version
        if expected is not None and expected != conversation.control_version:
            # The caller acted on a control state that is no longer current: its message is
            # not taken, and nobody asks the model.
            self._trace("checkpoint_failed", mode=conversation.mode.value)
            return self._finish(FinishDetail.STALE_VERSION)
        self.inbound_message_id = self.store.append_message(
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
            _check_answer_length(turn)
        except (ScriptExhausted, InvalidModelOutput, ModelUnavailable) as failure:
            self._trace("model_failed", code=failure.code)
            return _MODEL_FAILURES[type(failure)]
        kind = "final_answer" if isinstance(turn, FinalAnswer) else "tool_request"
        self._trace("model_replied", kind=kind)
        return turn

    def _prompt(self) -> Prompt:
        history = _starting_at_a_user_message(
            self.store.recent_messages(
                self.request.conversation_id, self.request.limits.context_window_messages
            )
        )
        conversation = self._conversation()
        return Prompt(
            messages=tuple(PromptMessage(message.role, message.content) for message in history),
            # Critical state travels structured, next to the history, and is never summarized.
            state={
                "conversation_mode": conversation.mode.value,
                "control_version": conversation.control_version,
                "pending_actions": [
                    action.view()
                    for action in self.store.ledger.open_actions(self.request.conversation_id)
                ],
                "commitments": [
                    c.view() for c in self.store.chat.open_commitments(self.request.conversation_id)
                ],
            },
            tools=self.catalog.names,
        )

    # ------------------------------------------------------------------ tools

    def _use_tool(self, call: ToolCall) -> FinishDetail | None:
        self._trace("tool_requested", tool=display_name(call.name))
        # Checkpoint B comes before the policy: after a takeover the mode no longer offers the
        # tool, and the run must end as a change of control, not as a rejected tool.
        if self._control_changed():
            return FinishDetail.OPERATOR_TOOK_OVER
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
        if verdict.spec.kind is ToolKind.WRITE:
            return self._write(verdict)
        result = execute(
            verdict,
            self.adapters,
            conversation_id=self.request.conversation_id,
            retrier=self._retrier(verdict.spec.name),
        )
        self._record_result(result)
        return None

    def _retrier(self, tool: str) -> Retrier:
        def note(retry_number: int, failure: ToolFailure) -> None:
            # The attempt that failed, then the one about to be made. The last failed attempt,
            # the one that is not retried, is reported with the result (`_record_result`).
            self._trace(
                "tool_error", tool=tool, reason=failure.code, transient=True, attempt=retry_number
            )
            self._trace("tool_retry", tool=tool, attempt=retry_number + 1, reason=failure.code)

        return Retrier(
            max_retries=self.request.limits.max_read_retries,
            remaining_ms=self.budget.remaining_ms,
            sleep=self.runner.ticker.sleep,
            jitter=self.jitter,
            on_retry=note,
        )

    # ------------------------------------------------------------------ writes

    def _answer_pending_confirmation(self) -> FinishDetail | None:
        """The user's reply to a confirmation question is read by code, before the model.

        A clear "sí" confirms the exact values, and only if they were asked under the current
        control version (a question asked before a takeover is stale) and the agent still
        controls the conversation. Anything else cancels it, so a later "sí" can never confirm
        something stale. A confirmation an earlier attempt already used (its outcome stayed
        uncertain) is cancelled by any new message, even a "sí" nobody asked for: if the model
        asks for the command again, it is proposed and asked anew.
        """
        # One confirmation, one attempt: a later message never stretches it to a second one.
        for spent in self.store.ledger.spent_confirmations(self.request.conversation_id):
            self._cancel(spent, PendingStatus.CONFIRMED)
        reply = classify_reply(self.request.message)
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
        if reply is not ReplyKind.AFFIRMATIVE:
            self._cancel(latest)
            return None
        confirmed = self.store.ledger.confirm_pending(
            latest.operation_id, self.request.conversation_id, self.base_version
        )
        if not confirmed:
            self._cancel(
                latest
            )  # asked under another control version: the user must be asked again
            return None
        self._trace("confirmation_received", tool=latest.tool, operation_id=latest.operation_id)
        # The confirmed values go through the same verification as any request.
        call = ToolCall(f"confirmed-{latest.operation_id}", latest.tool, latest.arguments or "{}")
        try:
            return self._use_tool(call)
        finally:
            # Rejected before any attempt (a tool this run may not use, invalid values): the
            # confirmation was not used, and must not wait for a later run to execute it.
            if self.store.ledger.cancel_unused_confirmation(latest.operation_id):
                self._trace(
                    "confirmation_cancelled", tool=latest.tool, operation_id=latest.operation_id
                )

    def _cancel(
        self, action: PendingAction, from_status: PendingStatus = PendingStatus.PENDING
    ) -> None:
        self.store.ledger.move_pending(
            action.operation_id,
            from_status=from_status,
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
                trace=self._trace,
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
            return self._reject(Rejected(name, outcome.code, outcome.detail))
        if isinstance(outcome, Failed):
            self._record_result(outcome.result)
            return None
        if isinstance(outcome, ControlChanged):
            # Caught inside the write's own transaction: a takeover between the checkpoints.
            self._trace("checkpoint_failed", mode="changed")
            return FinishDetail.OPERATOR_TOOK_OVER
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
            error = result.error or {}
            attempt: dict[str, int | bool] = {}
            if "retryable" in error:
                attempt["transient"] = error["retryable"] == "true"
            if "attempts" in error:
                attempt["attempt"] = int(error["attempts"])
            self._trace(
                "tool_error",
                tool=result.tool,
                reason=error.get("code"),
                data_tenant=result.source_tenant,
                **attempt,
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
            self.request.conversation_id,
            self.request.run_id,
            text,
            self.base_version,
            reply_to=self.inbound_message_id,
        )
        self._trace("final_answer" if delivered else "reply_suppressed", chars=len(text))
        return delivered

    def _finish(self, detail: FinishDetail) -> RunResult:
        reason = REASON_OF[detail]
        # Every run that ends BLOCKED or FAILED says why with a tool event (the contract's rule).
        missing = missing_failure_event(detail, _reasons(self.tracer.events))
        if missing is not None:
            self._trace(missing, tool=None, reason=detail.value)
        self._trace(
            "run_finished",
            finish_reason=reason.value,
            finish_detail=detail.value,
            steps=self.budget.steps_taken,
            tools_executed=_count_tools(self.tracer.events, "tool_executed"),
            # The event that says why a whole run was refused names no tool: it is not counted.
            tools_rejected=_count_tools(self.tracer.events, "tool_rejected"),
            retries=self.tracer.count("tool_retry"),
            duration_ms=self.budget.elapsed_ms(),
        )
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


def _reasons(events: Iterable[TraceEvent]) -> Iterator[tuple[str, object]]:
    """(type, reason) of each event, for `missing_failure_event`."""
    return ((event.type, event.attributes.get("reason")) for event in events)


def _count_tools(events: Iterable[TraceEvent], type_: str) -> int:
    """How many events of a type name a tool."""
    return sum(1 for e in events if e.type == type_ and e.attributes.get("tool") is not None)


def _check_encodable(message: str) -> None:
    """A message is stored, hashed and shown as UTF-8: one that holds a lone surrogate cannot be,
    so it is an invalid request, refused before anything runs."""
    try:
        message.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("the message holds a character UTF-8 cannot encode") from None


def _check_answer_length(turn: ModelTurn) -> None:
    """A final answer longer than a message can be is invalid output, like any other the harness
    cannot use: it ends the run and nothing is delivered."""
    if isinstance(turn, FinalAnswer) and len(turn.text) > MAX_ANSWER_CHARS:
        raise InvalidModelOutput("the final answer is longer than the limit")


def _starting_at_a_user_message(history: tuple[StoredMessage, ...]) -> tuple[StoredMessage, ...]:
    """The window never starts in the middle of a turn: a tool result without the request that
    produced it, or an answer without its question, would only confuse the model. If the window
    holds no user message at all, it is kept as it is."""
    for index, message in enumerate(history):
        if message.role == "user":
            return history[index:]
    return history
