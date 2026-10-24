"""One turn of the conversation: the model proposes, the code disposes (ADR-0001, ADR-0003).

`ConversationService.handle_user_message` runs these steps, in this order:

1. Load (or create) the conversation, count the turn, and hand it back to the agent if the
   human window has closed (`ConversationControl.sync_hours`; a change of hands invalidates
   the pending action).
2. Record the user message, then run the deterministic signals: abuse (repeated, it fires
   `abuse_detected`), a request for a human (`customer_requests_human`) and injection markers
   (a flag only). A transition made here is announced with a code-owned notice.
3. Read the capabilities of the state. If the state generates no reply (HUMAN, BLOCKED), the
   message is stored and nothing else happens.
4. The confirmation gate, BEFORE the model: a strict yes to the pending complaint executes it
   through the executor's only effect entry point and the turn ends without calling the
   model; a no cancels; a bare yes or no with nothing pending gets a template.
5. The model loop, at most `max_rounds` calls, offering only the tools the state allows.
   Every call crosses `authorize`; a sales date must be grounded in what the user said; the
   first accepted `crear_reclamo` only creates a pending action and ends the turn with the
   code-owned confirmation question.
6. Output checks on the model's answer: no tool call written as text, no citation of a
   document no tool returned in this conversation, no complaint number nobody mentioned.
7. Delivery against the state as stored NOW: if anyone saved the conversation while the turn
   ran (an operator took over while the model was answering), the answer is dropped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Literal

from merchant_assistant.agent.application.executor import (
    ExecutionFailed,
    ToolExecutor,
    envelope,
    outcome_envelope,
    refusal_envelope,
)
from merchant_assistant.agent.application.prompts import describe_hours, system_prompt
from merchant_assistant.agent.domain.dates import (
    assistant_message_before_last_user,
    dates_stated_by_assistant,
    is_grounded,
)
from merchant_assistant.agent.domain.errors import (
    ActionIgnored,
    AgentError,
    ConcurrentUpdate,
    DateNotConfirmed,
)
from merchant_assistant.agent.domain.guards import ValidatedCall, authorize
from merchant_assistant.agent.domain.messages import Message, Role, ToolCall
from merchant_assistant.agent.domain.pending import (
    Cancelled,
    Confirmed,
    Invalidated,
    NothingPending,
    PendingAction,
    decide,
    invalidate,
    mark_registered,
    propose,
)
from merchant_assistant.agent.domain.replies import ReplyKind, classify_reply
from merchant_assistant.agent.domain.signals import (
    abuse_score,
    injection_markers,
    looks_like_tool_call,
    wants_human,
)
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.agent.domain.templates import (
    answer_withheld,
    blocked_notice,
    cancelled,
    confirmation_request,
    execution_failed,
    handover_notice,
    hours_notice,
    human_unavailable,
    nothing_pending,
    registered,
    service_unavailable,
    turn_limit_reached,
    unavailable_in_this_state,
)
from merchant_assistant.agent.domain.tools import (
    BUSCAR_DOCUMENTACION,
    CONSULTAR_VENTAS,
    CREAR_RECLAMO,
    TOOL_SPECS,
    ConsultarVentasArgs,
    ToolKind,
    ToolSpec,
)
from merchant_assistant.agent.ports import ChatModel, ConversationStore
from merchant_assistant.control import (
    Actor,
    BusinessHours,
    Capabilities,
    ControlError,
    ConversationControl,
    Delivery,
    Effect,
    Event,
    Outcome,
    State,
    capabilities_for,
)
from merchant_assistant.shared.clock import Clock, business_today
from merchant_assistant.shared.errors import DomainError

OutboundKind = Literal["reply", "draft", "notice"]
ToolCallOutcome = Literal["executed", "rejected", "proposed", "ignored", "deduplicated"]

MAX_ROUNDS = 4
ABUSE_STRIKES_TO_BLOCK = 2
_SAVE_ATTEMPTS = 3

_CITATION_GROUP = re.compile(r"\[\s*(DOC-[0-9]+(?:\s*[,;]\s*DOC-[0-9]+)*)\s*\]")
_DOC_ID = re.compile(r"DOC-[0-9]+")
_COMPLAINT_ID = re.compile(r"REC-[0-9]{6}")
_UNAVAILABLE_TOOL = "<unavailable>"


@dataclass(frozen=True, slots=True)
class Outbound:
    kind: OutboundKind
    text: str


@dataclass(frozen=True, slots=True)
class ToolRecord:
    name: str
    arguments: str
    outcome: ToolCallOutcome
    status: str | None = None
    attempts: int = 0
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True, slots=True)
class PendingView:
    tool: str
    fingerprint_prefix: str


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What happened in one turn, as plain observable facts for tests and the evaluation."""

    conversation_id: str
    turn: int
    control_state: State
    outbound: tuple[Outbound, ...]
    tools: tuple[ToolRecord, ...]
    model_usage: tuple[ModelUsage, ...]
    violations: tuple[str, ...]
    pending: PendingView | None
    registered: tuple[str, ...]
    dropped: str | None
    gate: str | None  # confirmed, cancelled, invalidated, nothing_pending, or None
    model_error: str | None
    injection_markers: int
    events: tuple[str, ...]  # control events this turn, "event:applied" or "event:rejected:code"

    @property
    def model_calls(self) -> int:
        return len(self.model_usage)

    @property
    def served_models(self) -> tuple[str, ...]:
        return tuple(usage.model for usage in self.model_usage)


@dataclass
class _Turn:
    """The facts collected while a turn runs. Private and mutable; frozen into a TurnResult."""

    tools: list[ToolRecord] = field(default_factory=list)
    usage: list[ModelUsage] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    gate: str | None = None
    model_error: str | None = None
    injection: int = 0
    human_refused: bool = False
    dropped: str | None = None


@dataclass(frozen=True, slots=True)
class _Reply:
    text: str
    kind: Literal["reply", "notice"] = "reply"


class ConversationService:
    def __init__(
        self,
        store: ConversationStore,
        executor: ToolExecutor,
        model: ChatModel,
        *,
        hours: BusinessHours,
        clock: Clock,
        max_rounds: int = MAX_ROUNDS,
        abuse_strikes_to_block: int = ABUSE_STRIKES_TO_BLOCK,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        if abuse_strikes_to_block < 1:
            raise ValueError("abuse_strikes_to_block must be >= 1")
        self._store = store
        self._executor = executor
        self._model = model
        self._hours = hours
        self._clock = clock
        self._max_rounds = max_rounds
        self._abuse_limit = abuse_strikes_to_block
        self._window = describe_hours(hours)

    def handle_user_message(self, conversation_id: str, text: str) -> TurnResult:
        loaded = self._store.load(conversation_id)
        base_version = 0 if loaded is None else loaded.version
        state = loaded if loaded is not None else ConversationState(conversation_id)
        now = self._clock.now()
        turn = _Turn()

        # 1. A new turn, and the human window closed since the last one hands control back.
        state = replace(state, turn=state.turn + 1)
        control = state.control()
        state = self._apply(state, control, control.sync_hours(at=now, hours=self._hours), turn)

        # 2. The message is recorded whatever happens next (also in HUMAN and BLOCKED).
        user_message = Message(Role.USER, text)
        state = state.with_messages(user_message)
        turn.injection = injection_markers(text)
        state, notice = self._signals(state, text, now, turn)

        # 3. What the state allows.
        capabilities = capabilities_for(state.control_state, hours_open=self._hours.is_open(now))
        if notice is not None:
            return self._deliver(state, base_version, notice, capabilities, user_message, turn)
        if not capabilities.generate_reply:
            return self._deliver(state, base_version, None, capabilities, user_message, turn)

        # 4. The gate decides before the model is asked anything.
        state, early = self._gate(state, text, capabilities, turn)
        if early is not None:
            return self._deliver(state, base_version, early, capabilities, user_message, turn)

        # 5 and 6. The model proposes; every proposal is checked.
        state, reply = self._model_loop(state, capabilities, now, turn)
        return self._deliver(state, base_version, reply, capabilities, user_message, turn)

    # ------------------------------------------------------------------ control and signals

    def _apply(
        self,
        state: ConversationState,
        control: ConversationControl,
        outcome: Outcome | None,
        turn: _Turn,
    ) -> ConversationState:
        state = state.with_control(control)
        if outcome is None:
            return state
        turn.events.append(f"{outcome.event}:applied")
        if Effect.INVALIDATE_PENDING_ACTION in outcome.effects:
            state = replace(state, pending=invalidate(state.pending)[0])
        if Effect.DISCARD_UNAPPROVED_DRAFTS in outcome.effects:
            # No draft outbox exists yet (operator block): recorded so the effect is visible.
            turn.events.append("discard_unapproved_drafts")
        if Effect.NOTIFY_OPERATOR in outcome.effects:
            turn.events.append("notify_operator")
        return state

    def _fire(
        self, state: ConversationState, event: Event, actor: Actor, now: datetime, turn: _Turn
    ) -> tuple[ConversationState, Outcome | None]:
        control = state.control()
        try:
            outcome = control.apply(event, actor, at=now, hours=self._hours)
        except ControlError as refusal:
            # The refused attempt is in the log (brief rule 5): keep it.
            turn.events.append(f"{event}:rejected:{refusal.code}")
            return state.with_control(control), None
        return self._apply(state, control, outcome, turn), outcome

    def _signals(
        self, state: ConversationState, text: str, now: datetime, turn: _Turn
    ) -> tuple[ConversationState, _Reply | None]:
        current = state.control_state
        if abuse_score(text) > 0:
            state = replace(state, abuse_strikes=state.abuse_strikes + 1)
            if state.abuse_strikes >= self._abuse_limit and current is not State.BLOCKED:
                state, outcome = self._fire(state, Event.ABUSE_DETECTED, Actor.SYSTEM, now, turn)
                if outcome is not None and outcome.new is State.BLOCKED:
                    return state, _Reply(blocked_notice(), kind="notice")
                return state, None
        if current in (State.AUTO, State.SUGGEST) and wants_human(text):
            state, outcome = self._fire(
                state, Event.CUSTOMER_REQUESTS_HUMAN, Actor.CUSTOMER, now, turn
            )
            if outcome is None:
                turn.human_refused = True  # out of hours: the agent answers and says when
            elif outcome.new is State.HUMAN:
                return state, _Reply(handover_notice(), kind="notice")
        return state, None

    # ------------------------------------------------------------------ the gate

    def _gate(
        self, state: ConversationState, text: str, capabilities: Capabilities, turn: _Turn
    ) -> tuple[ConversationState, _Reply | None]:
        pending_state, decision = decide(
            state.pending, text, turn=state.turn, control_version=state.control_version
        )
        state = replace(state, pending=pending_state)
        match decision:
            case Confirmed():
                turn.gate = "confirmed"
                return self._execute(state, decision, capabilities, turn)
            case Cancelled():
                turn.gate = "cancelled"
                return state, _Reply(cancelled())
            case Invalidated():
                turn.gate = "invalidated"
                return state, None
            case NothingPending(last_registered=last):
                if classify_reply(text) is ReplyKind.OTHER or _answers_a_date_question(state):
                    return state, None
                turn.gate = "nothing_pending"
                return state, _Reply(nothing_pending(None if last is None else last.complaint_id))

    def _execute(
        self,
        state: ConversationState,
        confirmed: Confirmed,
        capabilities: Capabilities,
        turn: _Turn,
    ) -> tuple[ConversationState, _Reply]:
        arguments = _canonical(confirmed.action)
        if not capabilities.effect_tools or capabilities.effects_need_operator_approval:
            # The approval flow is the operator block's: nothing runs, nothing stays pending.
            turn.tools.append(
                ToolRecord(
                    CREAR_RECLAMO, arguments, "rejected", error_code="operator_approval_required"
                )
            )
            return replace(state, pending=invalidate(state.pending)[0]), _Reply(
                unavailable_in_this_state()
            )
        try:
            execution = self._executor.execute_confirmed(confirmed, state.conversation_id)
        except DomainError as failure:
            # The action stays pending: a later "sí" retries with the same idempotency key.
            code, attempts = failure.code, 1
            if isinstance(failure, ExecutionFailed):
                code, attempts = failure.cause_code, failure.attempts
            turn.tools.append(
                ToolRecord(CREAR_RECLAMO, arguments, "executed", "error", attempts, code)
            )
            return state, _Reply(execution_failed())
        pending_state, outcome = mark_registered(state.pending, confirmed, execution.complaint_id)
        turn.registered.append(execution.complaint_id)
        turn.tools.append(
            ToolRecord(
                CREAR_RECLAMO,
                arguments,
                "executed" if execution.created_effective else "deduplicated",
                "ok",
                execution.attempts,
            )
        )
        reply = registered(outcome.ref.complaint_id, created=execution.created_effective)
        return replace(state, pending=pending_state), _Reply(reply)

    # ------------------------------------------------------------------ the model loop

    def _model_loop(
        self, state: ConversationState, capabilities: Capabilities, now: datetime, turn: _Turn
    ) -> tuple[ConversationState, _Reply]:
        offered = offered_tools(capabilities)
        specs = tuple(offered.values())
        today = business_today(self._clock, self._hours.tz)
        prompt = Message(
            Role.SYSTEM, system_prompt(today, _zone(self._hours), self._window, offered)
        )
        for _ in range(self._max_rounds):
            try:
                completion = self._model.complete((prompt, *state.messages), specs)
            except DomainError as failure:
                turn.model_error = failure.code
                return state, _Reply(service_unavailable())
            turn.usage.append(
                ModelUsage(completion.model, completion.prompt_tokens, completion.completion_tokens)
            )
            reply = completion.reply
            if not reply.tool_calls:
                return state, _Reply(self._checked(reply.text or "", state, turn))
            # The model's own text next to tool calls is never shown: only results speak.
            state = state.with_messages(Message(Role.ASSISTANT, None, reply.tool_calls))
            state, proposal = self._run_calls(state, reply.tool_calls, offered, now, turn)
            if proposal is not None:
                return state, _Reply(proposal)
        turn.violations.append("round_limit")
        return state, _Reply(turn_limit_reached())

    def _run_calls(
        self,
        state: ConversationState,
        calls: tuple[ToolCall, ...],
        offered: Mapping[str, ToolSpec],
        now: datetime,
        turn: _Turn,
    ) -> tuple[ConversationState, str | None]:
        proposal: str | None = None
        for call in calls:
            shown_name = call.name if call.name in offered else _UNAVAILABLE_TOOL
            if proposal is not None:
                turn.tools.append(
                    ToolRecord(call.name, call.arguments, "ignored", error_code=ActionIgnored.code)
                )
                content = refusal_envelope(shown_name, ActionIgnored())
                state = state.with_messages(_tool_message(call, content))
                continue
            try:
                validated = authorize(call, offered)
            except AgentError as refusal:
                turn.tools.append(
                    ToolRecord(call.name, call.arguments, "rejected", error_code=refusal.code)
                )
                state = state.with_messages(
                    _tool_message(call, refusal_envelope(shown_name, refusal))
                )
                continue
            if validated.spec.kind is ToolKind.EFFECT:
                state, proposal = self._propose(state, validated, call, now, turn)
                continue
            state = self._read(state, validated, call, turn)
        return state, proposal

    def _propose(
        self,
        state: ConversationState,
        validated: ValidatedCall,
        call: ToolCall,
        now: datetime,
        turn: _Turn,
    ) -> tuple[ConversationState, str]:
        action = PendingAction.create(
            validated, now, turn=state.turn, control_version=state.control_version
        )
        pending_state, _ = propose(state.pending, action)
        turn.tools.append(ToolRecord(call.name, call.arguments, "proposed", "pending_confirmation"))
        content = envelope(
            validated.spec.name,
            "pending_confirmation",
            data={
                "registered": False,
                "note": "Nothing was registered. The user is shown the exact values and must "
                "confirm them in their own next message.",
            },
        )
        state = replace(state, pending=pending_state).with_messages(_tool_message(call, content))
        return state, confirmation_request(action)

    def _read(
        self, state: ConversationState, validated: ValidatedCall, call: ToolCall, turn: _Turn
    ) -> ConversationState:
        arguments = validated.arguments
        if isinstance(arguments, ConsultarVentasArgs):
            day = _iso_day(arguments.fecha)
            # An unparseable date goes on to the capability, which refuses it with its code.
            if day is not None and not is_grounded(day, state.messages):
                refusal = DateNotConfirmed(f"fecha={day.isoformat()}")
                turn.tools.append(
                    ToolRecord(call.name, call.arguments, "rejected", error_code=refusal.code)
                )
                content = refusal_envelope(validated.spec.name, refusal)
                return state.with_messages(_tool_message(call, content))
        outcome = self._executor.run_read(validated)
        turn.tools.append(
            ToolRecord(
                call.name,
                call.arguments,
                "executed",
                outcome.status,
                outcome.attempts,
                outcome.error_code,
            )
        )
        content = outcome_envelope(validated.spec.name, outcome)
        return state.with_messages(_tool_message(call, content))

    # ------------------------------------------------------------------ output checks

    def _checked(self, text: str, state: ConversationState, turn: _Turn) -> str:
        if looks_like_tool_call(text):
            turn.violations.append("tool_call_in_text")
            return answer_withheld()
        known_ids = _complaint_ids_mentioned(state)
        unknown = sorted(set(_COMPLAINT_ID.findall(text)) - known_ids)
        if unknown:
            turn.violations.extend(f"unknown_complaint_id:{cid}" for cid in unknown)
            return answer_withheld()
        retrieved = _retrieved_doc_ids(state)
        cleaned, invalid = _strip_citations(text, retrieved)
        turn.violations.extend(f"invalid_citation:{doc_id}" for doc_id in invalid)
        return cleaned if cleaned.strip() else answer_withheld()

    # ------------------------------------------------------------------ delivery

    def _deliver(
        self,
        state: ConversationState,
        base_version: int,
        reply: _Reply | None,
        capabilities: Capabilities,
        user_message: Message,
        turn: _Turn,
    ) -> TurnResult:
        stored = self._store.load(state.conversation_id)
        if (0 if stored is None else stored.version) != base_version:
            return self._drop(state.conversation_id, user_message, turn)

        outbound: list[Outbound] = []
        if reply is not None:
            text = self._with_notices(reply, capabilities, turn)
            if reply.kind == "notice":
                outbound.append(Outbound("notice", text))
                state = state.with_messages(Message(Role.ASSISTANT, text))
            elif capabilities.delivery is Delivery.SEND:
                outbound.append(Outbound("reply", text))
                state = state.with_messages(Message(Role.ASSISTANT, text))
            elif capabilities.delivery is Delivery.DRAFT:
                # Not in the history: the user has not seen it. The operator block approves it.
                outbound.append(Outbound("draft", text))

        final = replace(state, version=base_version + 1)
        try:
            self._store.save(final, expected_version=base_version)
        except ConcurrentUpdate:
            return self._drop(state.conversation_id, user_message, turn)
        return _result(final, outbound, turn)

    def _with_notices(self, reply: _Reply, capabilities: Capabilities, turn: _Turn) -> str:
        if reply.kind == "notice":
            return reply.text
        if turn.human_refused:
            return f"{reply.text}\n\n{human_unavailable(self._window)}"
        if capabilities.inform_hours_window:
            return f"{reply.text}\n\n{hours_notice(self._window)}"
        return reply.text

    def _drop(self, conversation_id: str, user_message: Message, turn: _Turn) -> TurnResult:
        """Someone saved the conversation during this turn: deliver nothing, keep the message.

        Everything the turn computed (tool results, a proposal, the answer) is discarded and
        only the user's message is added to the state as it is stored now, so it is never lost.
        """
        turn.dropped = "control_changed"
        for _ in range(_SAVE_ATTEMPTS):
            fresh = self._store.load(conversation_id) or ConversationState(conversation_id)
            updated = replace(
                fresh.with_messages(user_message), turn=fresh.turn + 1, version=fresh.version + 1
            )
            try:
                self._store.save(updated, expected_version=fresh.version)
            except ConcurrentUpdate:
                continue
            return _result(updated, [], turn)
        raise ConcurrentUpdate(f"conversation {conversation_id[:40]!r} keeps changing")


# ---------------------------------------------------------------------- helpers


def _answers_a_date_question(state: ConversationState) -> bool:
    """A bare "sí" or "no" right after the assistant stated a date answers THAT question."""
    previous = assistant_message_before_last_user(state.messages)
    return previous is not None and bool(dates_stated_by_assistant(previous.content or ""))


def offered_tools(capabilities: Capabilities) -> dict[str, ToolSpec]:
    """The tools the state allows, which is exactly what the model is offered and what
    `authorize` accepts: one set for both, so they cannot disagree."""
    names: list[str] = []
    if capabilities.read_tools:
        names += [BUSCAR_DOCUMENTACION, CONSULTAR_VENTAS]
    if capabilities.effect_tools:
        names.append(CREAR_RECLAMO)
    return {name: TOOL_SPECS[name] for name in names}


def _tool_message(call: ToolCall, content: str) -> Message:
    return Message(Role.TOOL, content, tool_call_id=call.id)


def _canonical(action: PendingAction) -> str:
    return json.dumps(action.arguments.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def _iso_day(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _zone(hours: BusinessHours) -> str:
    return getattr(hours.tz, "key", None) or str(hours.tz)


def _retrieved_doc_ids(state: ConversationState) -> frozenset[str]:
    """Doc ids that a documentation search returned in this conversation (from our envelopes)."""
    found: set[str] = set()
    for message in state.messages:
        if message.role is not Role.TOOL or message.content is None:
            continue
        payload = json.loads(message.content)
        if payload.get("tool") != BUSCAR_DOCUMENTACION or payload.get("status") != "ok":
            continue
        found.update(passage["doc_id"] for passage in payload["data"]["passages"])
    return frozenset(found)


def _complaint_ids_mentioned(state: ConversationState) -> frozenset[str]:
    found: set[str] = set()
    for message in state.messages:
        if message.role in (Role.USER, Role.ASSISTANT) and message.content:
            found.update(_COMPLAINT_ID.findall(message.content))
    return frozenset(found)


def _strip_citations(text: str, valid: frozenset[str]) -> tuple[str, list[str]]:
    invalid: list[str] = []

    def rewrite(match: re.Match[str]) -> str:
        ids = _DOC_ID.findall(match.group(1))
        kept = [doc_id for doc_id in ids if doc_id in valid]
        invalid.extend(doc_id for doc_id in ids if doc_id not in valid and doc_id not in invalid)
        return f"[{', '.join(kept)}]" if kept else ""

    cleaned = _CITATION_GROUP.sub(rewrite, text)
    if invalid:
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+([.,;:])", r"\1", cleaned).strip()
    return cleaned, invalid


def _result(state: ConversationState, outbound: list[Outbound], turn: _Turn) -> TurnResult:
    action = state.pending.pending
    pending = None if action is None else PendingView(action.tool, action.fingerprint[:12])
    return TurnResult(
        conversation_id=state.conversation_id,
        turn=state.turn,
        control_state=state.control_state,
        outbound=tuple(outbound),
        tools=tuple(turn.tools),
        model_usage=tuple(turn.usage),
        violations=tuple(turn.violations),
        pending=pending,
        registered=tuple(turn.registered),
        dropped=turn.dropped,
        gate=turn.gate,
        model_error=turn.model_error,
        injection_markers=turn.injection,
        events=tuple(turn.events),
    )
