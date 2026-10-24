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
6. Output checks on the model's answer: no tool call written as text, no claim that a
   complaint was registered (only code templates say that), no complaint number nobody
   mentioned, no mention of a document no search returned in this conversation; and an
   amount of money no tool payload contains is FLAGGED (not withheld).
   Then, in AUTO, if the model searched the documentation and EVERY search came back
   `no_data` (the search's calibrated relevance floor, never the model's own confidence),
   the turn fires `low_confidence` (SYSTEM). In hours the conversation moves to SUGGEST and
   this very answer becomes a draft; out of hours the guard refuses it, the refusal is
   logged, and the answer goes out with the hours notice.
7. Delivery against the state as stored NOW: if anyone saved the conversation while the turn
   ran (an operator took over while the model was answering), the answer is dropped.

In SUGGEST nothing reaches the customer from the turn: the answer becomes a REPLY draft, the
model's `crear_reclamo` becomes a CONFIRMATION_REQUEST draft (nothing is pending until an
operator approves it and the question is actually delivered), and a strict yes to a delivered
question becomes an ACTION_APPROVAL draft holding the gate's `Confirmed`. Only the operator's
approval executes it (`OperatorService.approve_draft`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
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
from merchant_assistant.agent.domain.drafts import Draft, DraftKind, DraftStatus, next_draft_id
from merchant_assistant.agent.domain.effects import apply_effects
from merchant_assistant.agent.domain.errors import (
    ActionIgnored,
    AgentError,
    ConcurrentUpdate,
    DateNotConfirmed,
)
from merchant_assistant.agent.domain.guards import ValidatedCall, authorize
from merchant_assistant.agent.domain.messages import Message, Role, ToolCall
from merchant_assistant.agent.domain.output_checks import (
    canonical_doc_id,
    claims_registration,
    money_amounts,
    rewrite_citations,
    unverified_amounts,
)
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
    action_approval_summary,
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
    Event,
    Outcome,
    State,
    capabilities_for,
)
from merchant_assistant.shared.clock import Clock, business_today
from merchant_assistant.shared.errors import DomainError

# reply: the agent's answer or a code template; notice: a template announcing a change of hands;
# operator: written by a person in HUMAN; draft: NOT sent, waiting for an operator in SUGGEST.
OutboundKind = Literal["reply", "draft", "notice", "operator"]
ToolCallOutcome = Literal[
    "executed", "rejected", "proposed", "ignored", "deduplicated", "awaiting_operator"
]

MAX_ROUNDS = 4
ABUSE_STRIKES_TO_BLOCK = 2
_SAVE_ATTEMPTS = 3

_SALES_FIGURES = ("bruto", "devoluciones", "neto")
_COMPLAINT_ID = re.compile(r"REC-[0-9]{6}")
_UNAVAILABLE_TOOL = "<unavailable>"


@dataclass(frozen=True, slots=True)
class Outbound:
    kind: OutboundKind
    text: str
    draft_id: str | None = None  # the draft a "draft" holds, or the approved draft a reply sent


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
    violations: tuple[str, ...]  # what was withheld or removed from the model's answer
    pending: PendingView | None
    registered: tuple[str, ...]
    dropped: str | None
    gate: str | None  # confirmed, cancelled, invalidated, nothing_pending, or None
    model_error: str | None
    injection_markers: int
    events: tuple[str, ...]  # control events this turn, "event:applied" or "event:rejected:code"
    drafts: tuple[str, ...] = ()  # drafts this turn created for an operator (SUGGEST)
    flags: tuple[str, ...] = ()  # delivered but unverified, e.g. "unverified_amount:$45.000"

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
    flags: list[str] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    gate: str | None = None
    model_error: str | None = None
    injection: int = 0
    human_refused: bool = False
    dropped: str | None = None
    drafts: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Reply:
    """What the turn would say. In SUGGEST it becomes a draft instead of being sent.

    `action` marks the confirmation question of a complaint the model proposed in SUGGEST;
    `confirmed` is a customer's yes in SUGGEST, which says nothing and waits for an operator.
    """

    text: str | None
    kind: Literal["reply", "notice"] = "reply"
    action: PendingAction | None = None
    confirmed: Confirmed | None = None
    from_model: bool = False  # the model's own answer (checked), not a code template


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
        if _found_nothing(state, reply, turn):
            # The transition lives in this turn's state only: the stored version is untouched
            # until delivery saves, so the turn's own change never reads as a concurrent one.
            state, outcome = self._fire(state, Event.LOW_CONFIDENCE, Actor.SYSTEM, now, turn)
            if outcome is not None:
                capabilities = capabilities_for(
                    state.control_state, hours_open=self._hours.is_open(now)
                )
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
        state, facts = apply_effects(state, outcome, at=self._clock.now())
        turn.events.extend(facts)
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
            case Cancelled(action=dropped):
                turn.gate = "cancelled"
                return self._forget_approvals(state, dropped, turn), _Reply(cancelled())
            case Invalidated(action=dropped):
                turn.gate = "invalidated"
                return self._forget_approvals(state, dropped, turn), None
            case NothingPending(last_registered=last):
                if classify_reply(text) is ReplyKind.OTHER or _answers_a_date_question(state):
                    return state, None
                turn.gate = "nothing_pending"
                return state, _Reply(nothing_pending(None if last is None else last.complaint_id))

    def _forget_approvals(
        self, state: ConversationState, dropped: PendingAction, turn: _Turn
    ) -> ConversationState:
        """The customer said no, or something else, to an action an operator was asked to
        approve: that approval can no longer be given, so its draft is discarded now."""
        stale = [
            draft.id
            for draft in state.pending_drafts
            if draft.confirmed is not None
            and draft.confirmed.action.fingerprint == dropped.fingerprint
        ]
        if not stale:
            return state
        kept = tuple(
            replace(
                draft,
                status=DraftStatus.DISCARDED,
                decided_by=Actor.SYSTEM,
                decided_at=self._clock.now(),
            )
            if draft.id in stale
            else draft
            for draft in state.drafts
        )
        turn.events.extend(f"draft_discarded:{draft_id}" for draft_id in stale)
        return replace(state, drafts=kept)

    def _execute(
        self,
        state: ConversationState,
        confirmed: Confirmed,
        capabilities: Capabilities,
        turn: _Turn,
    ) -> tuple[ConversationState, _Reply]:
        arguments = _canonical(confirmed.action)
        if capabilities.effect_tools and capabilities.effects_need_operator_approval:
            # Rule 6: the customer confirmed; now an operator must approve. Nothing runs and
            # the action stays pending, so the approval can still execute it.
            turn.tools.append(
                ToolRecord(
                    CREAR_RECLAMO,
                    arguments,
                    "awaiting_operator",
                    "operator_approval_required",
                )
            )
            return state, _Reply(None, confirmed=confirmed)
        if not capabilities.effect_tools:
            # No state offers replies without effects today; refused, nothing stays pending.
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
                checked = self._checked(reply.text or "", state, turn)
                return state, _Reply(checked, from_model=True)
            # The model's own text next to tool calls is never shown: only results speak.
            state = state.with_messages(Message(Role.ASSISTANT, None, reply.tool_calls))
            state, proposal = self._run_calls(
                state, reply.tool_calls, offered, capabilities, now, turn
            )
            if proposal is not None:
                return state, proposal
        turn.violations.append("round_limit")
        return state, _Reply(turn_limit_reached())

    def _run_calls(
        self,
        state: ConversationState,
        calls: tuple[ToolCall, ...],
        offered: Mapping[str, ToolSpec],
        capabilities: Capabilities,
        now: datetime,
        turn: _Turn,
    ) -> tuple[ConversationState, _Reply | None]:
        proposal: _Reply | None = None
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
                state, proposal = self._propose(state, validated, call, capabilities, now, turn)
                continue
            state = self._read(state, validated, call, turn)
        return state, proposal

    def _propose(
        self,
        state: ConversationState,
        validated: ValidatedCall,
        call: ToolCall,
        capabilities: Capabilities,
        now: datetime,
        turn: _Turn,
    ) -> tuple[ConversationState, _Reply]:
        action = PendingAction.create(
            validated, now, turn=state.turn, control_version=state.control_version
        )
        question = confirmation_request(action)
        if capabilities.effects_need_operator_approval:
            # SUGGEST: the question itself is a draft. Nothing is pending until an operator
            # approves it, because only then has the customer seen these exact values.
            turn.tools.append(
                ToolRecord(call.name, call.arguments, "proposed", "awaiting_operator")
            )
            content = envelope(
                validated.spec.name,
                "pending_confirmation",
                data={
                    "registered": False,
                    "note": "Nothing was registered. An operator reviews the question before "
                    "the user sees it; the user must then confirm the exact values.",
                },
            )
            return state.with_messages(_tool_message(call, content)), _Reply(
                question, action=action
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
        return state, _Reply(question)

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
        if claims_registration(text):
            # Registrations are told by code templates only: the model's claim is false.
            turn.violations.append("registration_claim")
            return answer_withheld()
        known_ids = _complaint_ids_mentioned(state)
        unknown = sorted(set(_COMPLAINT_ID.findall(text)) - known_ids)
        if unknown:
            turn.violations.extend(f"unknown_complaint_id:{cid}" for cid in unknown)
            return answer_withheld()
        citations = rewrite_citations(text, _retrieved_doc_ids(state))
        turn.violations.extend(f"invalid_citation:{doc_id}" for doc_id in citations.invalid)
        if citations.withhold or not citations.text.strip():
            return answer_withheld()
        known_amounts = _amounts_in_payloads(state)
        turn.flags.extend(
            f"unverified_amount:{amount.written}"
            for amount in unverified_amounts(citations.text, known_amounts)
        )
        return citations.text

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
        if reply is not None and reply.confirmed is not None:
            state = self._ask_for_approval(state, reply.confirmed, turn)
        elif reply is not None and reply.text is not None:
            text = reply.text
            if reply.kind != "notice":
                text = self._with_notices(text, capabilities, turn)
            if reply.kind == "notice":
                outbound.append(Outbound("notice", text))
                state = state.with_messages(Message(Role.ASSISTANT, text))
            elif capabilities.delivery is Delivery.SEND and reply.action is None:
                outbound.append(Outbound("reply", text))
                state = state.with_messages(Message(Role.ASSISTANT, text))
            elif capabilities.delivery is Delivery.DRAFT:
                # Not in the history: the customer has not seen it until an operator approves.
                kind = DraftKind.REPLY if reply.action is None else DraftKind.CONFIRMATION_REQUEST
                draft = Draft(
                    next_draft_id(state.drafts),
                    kind,
                    text,
                    self._clock.now(),
                    state.turn,
                    action=reply.action,
                )
                state = replace(state, drafts=(*state.drafts, draft))
                turn.drafts.append(draft.id)
                outbound.append(Outbound("draft", text, draft.id))

        final = replace(state, version=base_version + 1)
        try:
            self._store.save(final, expected_version=base_version)
        except ConcurrentUpdate:
            return self._drop(state.conversation_id, user_message, turn)
        return _result(final, outbound, turn)

    def _ask_for_approval(
        self, state: ConversationState, confirmed: Confirmed, turn: _Turn
    ) -> ConversationState:
        """An ACTION_APPROVAL draft for the operator; one per action, however often the
        customer repeats the yes. Nothing is said to the customer."""
        fingerprint = confirmed.action.fingerprint
        for draft in state.pending_drafts:
            if draft.confirmed is not None and draft.confirmed.action.fingerprint == fingerprint:
                return state
        draft = Draft(
            next_draft_id(state.drafts),
            DraftKind.ACTION_APPROVAL,
            action_approval_summary(confirmed.action),
            self._clock.now(),
            state.turn,
            confirmed=confirmed,
        )
        turn.drafts.append(draft.id)
        return replace(state, drafts=(*state.drafts, draft))

    def _with_notices(self, text: str, capabilities: Capabilities, turn: _Turn) -> str:
        if turn.human_refused:
            return f"{text}\n\n{human_unavailable(self._window)}"
        if capabilities.inform_hours_window:
            return f"{text}\n\n{hours_notice(self._window)}"
        return text

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


def _found_nothing(state: ConversationState, reply: _Reply, turn: _Turn) -> bool:
    """AUTO, the model answered in its own words, and every documentation search of this
    turn came back `no_data`: the deterministic low-confidence signal (SPEC_REVIEW F9)."""
    searches = [
        record.status
        for record in turn.tools
        if record.name == BUSCAR_DOCUMENTACION and record.outcome == "executed"
    ]
    return (
        state.control_state is State.AUTO
        and reply.from_model
        and bool(searches)
        and all(status == "no_data" for status in searches)
    )


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
    for payload in _tool_payloads(state):
        data = payload.get("data")
        if payload.get("tool") != BUSCAR_DOCUMENTACION or payload.get("status") != "ok":
            continue
        if isinstance(data, dict):
            found.update(canonical_doc_id(str(p["doc_id"])) for p in data.get("passages", ()))
    return frozenset(found)


def _complaint_ids_mentioned(state: ConversationState) -> frozenset[str]:
    found: set[str] = set()
    for message in state.messages:
        if message.role in (Role.USER, Role.ASSISTANT) and message.content:
            found.update(_COMPLAINT_ID.findall(message.content))
    return frozenset(found)


def _amounts_in_payloads(state: ConversationState) -> frozenset[Decimal]:
    """Every figure a tool gave in this conversation: sales and money in document text."""
    found: set[Decimal] = set()
    for payload in _tool_payloads(state):
        data = payload.get("data")
        if not isinstance(data, dict) or payload.get("status") != "ok":
            continue
        if payload.get("tool") == CONSULTAR_VENTAS:
            found.update(abs(Decimal(str(data[key]))) for key in _SALES_FIGURES if key in data)
        elif payload.get("tool") == BUSCAR_DOCUMENTACION:
            for passage in data.get("passages", ()):
                found.update(amount.value for amount in money_amounts(str(passage["text"])))
    return frozenset(found)


def _tool_payloads(state: ConversationState) -> list[dict[str, object]]:
    """The envelopes this agent wrote in the history (one JSON object per tool message)."""
    payloads: list[dict[str, object]] = []
    for message in state.messages:
        if message.role is Role.TOOL and message.content is not None:
            payload = json.loads(message.content)
            if isinstance(payload, dict):
                payloads.append(payload)
    return payloads


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
        drafts=tuple(turn.drafts),
        flags=tuple(turn.flags),
    )
