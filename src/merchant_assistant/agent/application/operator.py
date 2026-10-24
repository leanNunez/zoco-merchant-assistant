"""What the merchant's team does to a conversation: operators and supervisors.

`OperatorService` shares the `ConversationStore` with the customer's turn
(`ConversationService`). Every command is load, change, save with optimistic concurrency
(`expected_version`), so a command that lands while a customer turn is running makes that
turn drop its answer (F16), and a command that loses the race is replayed on the fresh state.

Control commands (`takeover`, `release`, `block`, `unblock`) fire their event through
`ConversationControl`: the control log is the audit trail (previous state, new state, event,
actor, time; a refusal is recorded as REJECTED with its code). The effects of an applied
transition are executed by `apply_effects`, the same function the customer's turn uses. A
control refusal (`InvalidTransition`, `UnauthorizedActor`, `GuardRejected`) is saved with the
log and then raised unchanged.

Every command first hands the conversation back to the agent if the human window has closed
(`sync_hours`), exactly as a customer turn does: out of hours only the AI answers, so an
operator cannot write to the customer, nor approve a draft, after closing time.

Drafts (SUGGEST) are decided here, and the order of the two approvals a complaint needs in
SUGGEST is fixed (rule 6, SPEC_REVIEW F11):

1. The model proposes; the turn stores a CONFIRMATION_REQUEST draft and nothing is pending.
2. The operator approves it: the code-owned question is delivered and only NOW the action is
   proposed, with the turn and control version current at delivery. The customer's answer is
   necessarily a later turn.
3. The customer's strict yes yields a `Confirmed`; in SUGGEST the turn executes nothing and
   stores an ACTION_APPROVAL draft holding it.
4. The operator approves that draft: `execute_confirmed`, `mark_registered`, and the
   code-owned `registered` template. That message is the deterministic consequence of the
   operator's decision, so it counts as approved by that operator; so does the code-owned
   `not_approved` sent when the operator rejects instead.

There is no "edit the parameters" command: an operator who wants other values rejects, and
the customer states them again (and confirms them again).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

from merchant_assistant.agent.application.conversation import Outbound
from merchant_assistant.agent.application.executor import Execution, ExecutionFailed, ToolExecutor
from merchant_assistant.agent.domain.drafts import (
    Draft,
    DraftKind,
    DraftStatus,
    decide,
    find_pending,
)
from merchant_assistant.agent.domain.effects import apply_effects
from merchant_assistant.agent.domain.errors import (
    CommandNotAllowed,
    ConcurrentUpdate,
    DraftNotApprovable,
    InvalidOperatorMessage,
    OperatorReplyNotAllowed,
)
from merchant_assistant.agent.domain.messages import Author, Message, Role
from merchant_assistant.agent.domain.pending import (
    Confirmed,
    PendingState,
    RegisteredRef,
    mark_registered,
    propose,
)
from merchant_assistant.agent.domain.state import ConversationState, OperatorNotice
from merchant_assistant.agent.domain.templates import execution_failed, not_approved, registered
from merchant_assistant.agent.ports import ConversationStore
from merchant_assistant.control import (
    Actor,
    BusinessHours,
    ControlError,
    Event,
    State,
)
from merchant_assistant.shared.clock import Clock
from merchant_assistant.shared.errors import DomainError

OPERATOR_MESSAGE_LIMIT = 2000
_SAVE_ATTEMPTS = 3
_TEAM = frozenset({Actor.OPERATOR, Actor.SUPERVISOR})


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What one command did, as plain facts. `outbound` is what reached the customer."""

    conversation_id: str
    command: str
    actor: Actor
    control_state: State
    outbound: tuple[Outbound, ...]
    events: tuple[str, ...]
    registered: tuple[str, ...] = ()


@dataclass
class _Change:
    """A command's work on one loaded state. Private and mutable while the command runs."""

    state: ConversationState
    outbound: list[Outbound] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    registered: list[str] = field(default_factory=list)
    # A refusal that must be SAVED (it is in the control log) and then raised.
    refusal: DomainError | None = None


_Command = Callable[[_Change, datetime], None]


class OperatorService:
    def __init__(
        self,
        store: ConversationStore,
        executor: ToolExecutor,
        *,
        hours: BusinessHours,
        clock: Clock,
    ) -> None:
        self._store = store
        self._executor = executor
        self._hours = hours
        self._clock = clock

    # ------------------------------------------------------------------ control commands

    def takeover(self, conversation_id: str, actor: Actor) -> CommandResult:
        return self._control(conversation_id, "takeover", Event.OPERATOR_TAKEOVER, actor)

    def release(self, conversation_id: str, actor: Actor) -> CommandResult:
        return self._control(conversation_id, "release", Event.OPERATOR_RELEASE, actor)

    def block(self, conversation_id: str, actor: Actor) -> CommandResult:
        return self._control(conversation_id, "block", Event.OPERATOR_BLOCK, actor)

    def unblock(self, conversation_id: str, actor: Actor) -> CommandResult:
        return self._control(conversation_id, "unblock", Event.SUPERVISOR_UNBLOCK, actor)

    # ------------------------------------------------------------------ writing to the customer

    def operator_reply(self, conversation_id: str, actor: Actor, text: str) -> CommandResult:
        """Deliver an operator's message. Only in HUMAN, where the agent is silent.

        The message enters the history marked as the operator's, so after a release the model
        sees what the person told the customer.
        """
        _require_team(actor, "operator_reply")
        message = text.strip()
        if not message or len(message) > OPERATOR_MESSAGE_LIMIT:
            raise InvalidOperatorMessage(f"length={len(message)}")

        def write(change: _Change, now: datetime) -> None:
            if change.state.control_state is not State.HUMAN:
                raise OperatorReplyNotAllowed(f"state={change.state.control_state}")
            change.state = change.state.with_messages(
                Message(Role.ASSISTANT, message, author=Author.OPERATOR)
            )
            change.outbound.append(Outbound("operator", message))

        return self._run(conversation_id, "operator_reply", actor, write)

    # ------------------------------------------------------------------ drafts

    def drafts(self, conversation_id: str) -> tuple[Draft, ...]:
        """The drafts still waiting for a decision, oldest first."""
        state = self._store.load(conversation_id)
        return () if state is None else state.pending_drafts

    def approve_draft(self, conversation_id: str, draft_id: str, actor: Actor) -> CommandResult:
        """Approve a pending draft while the conversation is still in SUGGEST.

        REPLY: the text is delivered. CONFIRMATION_REQUEST: the question is delivered and the
        action becomes pending. ACTION_APPROVAL: the complaint is executed and the outcome is
        told with a code-owned template; it is refused (and the draft discarded) if the action
        it holds is no longer the pending one or control changed hands since.
        """
        _require_team(actor, "approve_draft")
        # The effect runs at most once per command, even when a lost race replays the save.
        executed: list[tuple[Confirmed, Execution]] = []

        def approve(change: _Change, now: datetime) -> None:
            if executed:
                confirmed, execution = executed[0]
                self._announce(change, draft_id, actor, confirmed, execution, now)
                return
            draft = find_pending(change.state.drafts, draft_id)
            if change.state.control_state is not State.SUGGEST:
                # Unreachable while every exit from SUGGEST discards the drafts; kept as a guard.
                raise DraftNotApprovable(f"draft={draft.id} state={change.state.control_state}")
            match draft.kind:
                case DraftKind.REPLY:
                    self._deliver_draft(change, draft, actor, now)
                case DraftKind.CONFIRMATION_REQUEST:
                    self._deliver_question(change, draft, actor, now)
                case DraftKind.ACTION_APPROVAL:
                    self._execute(change, draft, actor, now, executed)

        return self._run(conversation_id, "approve_draft", actor, approve)

    def reject_draft(self, conversation_id: str, draft_id: str, actor: Actor) -> CommandResult:
        """Reject a pending draft: nothing of it is sent. Rejecting an ACTION_APPROVAL also
        cancels the pending complaint and tells the customer, with a template, that it was not
        registered."""
        _require_team(actor, "reject_draft")

        def reject(change: _Change, now: datetime) -> None:
            state = change.state
            draft = find_pending(state.drafts, draft_id)
            drafts = decide(state.drafts, draft.id, DraftStatus.REJECTED, actor=actor, at=now)
            change.state = replace(state, drafts=drafts)
            change.events.append(f"draft_rejected:{draft.id}")
            if draft.kind is not DraftKind.ACTION_APPROVAL or draft.confirmed is None:
                return
            pending = state.pending.pending
            if pending is not None and pending.fingerprint == draft.confirmed.action.fingerprint:
                change.state = replace(change.state, pending=replace(state.pending, pending=None))
                change.events.append("pending_action_cancelled")
            if state.control_state is State.SUGGEST:
                self._say(change, not_approved(), draft.id)

        return self._run(conversation_id, "reject_draft", actor, reject)

    # ------------------------------------------------------------------ approving, per kind

    def _deliver_draft(self, change: _Change, draft: Draft, actor: Actor, now: datetime) -> None:
        self._mark_approved(change, draft, actor, now)
        self._say(change, draft.text, draft.id)

    def _deliver_question(self, change: _Change, draft: Draft, actor: Actor, now: datetime) -> None:
        if draft.action is None:
            raise DraftNotApprovable(f"draft={draft.id} carries no action")
        state = change.state
        # Proposed NOW, when the customer is about to see the values: the gate then requires
        # the answer in a later turn and under this same control version.
        action = replace(
            draft.action,
            proposed_at=now,
            proposed_turn=state.turn,
            control_version=state.control_version,
        )
        pending_state, _ = propose(state.pending, action)
        change.state = replace(state, pending=pending_state)
        change.events.append("confirmation_pending")
        self._mark_approved(change, draft, actor, now)
        self._say(change, draft.text, draft.id)

    def _execute(
        self,
        change: _Change,
        draft: Draft,
        actor: Actor,
        now: datetime,
        executed: list[tuple[Confirmed, Execution]],
    ) -> None:
        state = change.state
        confirmed = draft.confirmed
        if confirmed is None:
            raise DraftNotApprovable(f"draft={draft.id} carries no confirmation")
        if not _is_current(state, confirmed):
            # The customer's yes was for an action that is no longer the one pending, or given
            # under other hands: approving it would execute what nobody confirmed now.
            drafts = tuple(
                replace(d, status=DraftStatus.DISCARDED, decided_by=Actor.SYSTEM, decided_at=now)
                if d.id == draft.id
                else d
                for d in state.drafts
            )
            change.state = replace(state, drafts=drafts)
            change.events.append(f"draft_discarded:{draft.id}")
            change.refusal = DraftNotApprovable(f"draft={draft.id} is stale")
            return
        try:
            execution = self._executor.execute_confirmed(confirmed, state.conversation_id)
        except DomainError as failure:
            # Whether it was stored is unknown: the action stays pending, and a later yes asks
            # the operator again with the same idempotency key.
            code = failure.cause_code if isinstance(failure, ExecutionFailed) else failure.code
            change.events.append(f"crear_reclamo:failed:{code}")
            self._mark_approved(change, draft, actor, now)
            self._say(change, execution_failed(), draft.id)
            return
        executed.append((confirmed, execution))
        self._announce(change, draft.id, actor, confirmed, execution, now)

    def _announce(
        self,
        change: _Change,
        draft_id: str,
        actor: Actor,
        confirmed: Confirmed,
        execution: Execution,
        now: datetime,
    ) -> None:
        """Record a complaint known to be stored, and tell the customer when still possible."""
        state = change.state
        draft = next((d for d in state.drafts if d.id == draft_id), None)
        change.registered.append(execution.complaint_id)
        change.events.append(f"crear_reclamo:registered:{execution.complaint_id}")
        if (
            draft is not None
            and draft.is_pending
            and state.control_state is State.SUGGEST
            and _is_current(state, confirmed)
        ):
            pending_state, _ = mark_registered(state.pending, confirmed, execution.complaint_id)
            change.state = replace(state, pending=pending_state)
            self._mark_approved(change, draft, actor, now)
            text = registered(execution.complaint_id, created=execution.created_effective)
            self._say(change, text, draft_id)
            return
        # Only after a lost race: the complaint was stored, but the conversation moved on
        # before it could be told. It is recorded and the team is told; the customer is not,
        # because the state no longer lets anything be sent.
        pending = state.pending.pending
        still_this = pending is not None and pending.fingerprint == confirmed.action.fingerprint
        ref = RegisteredRef(execution.complaint_id, confirmed.action.fingerprint)
        notice = OperatorNotice(now, "complaint_registered_unannounced", execution.complaint_id)
        change.state = replace(
            state,
            pending=PendingState(pending=None if still_this else pending, last_registered=ref),
            inbox=(*state.inbox, notice),
        )
        if draft is not None and draft.is_pending:
            self._mark_approved(change, draft, actor, now)

    def _mark_approved(self, change: _Change, draft: Draft, actor: Actor, now: datetime) -> None:
        drafts = decide(change.state.drafts, draft.id, DraftStatus.APPROVED, actor=actor, at=now)
        change.state = replace(change.state, drafts=drafts)
        change.events.append(f"draft_approved:{draft.id}")

    def _say(self, change: _Change, text: str, draft_id: str) -> None:
        """A message that follows an operator's decision: delivered and added to the history."""
        change.state = change.state.with_messages(Message(Role.ASSISTANT, text))
        change.outbound.append(Outbound("reply", text, draft_id))

    # ------------------------------------------------------------------ the machinery

    def _control(
        self, conversation_id: str, command: str, event: Event, actor: Actor
    ) -> CommandResult:
        def fire(change: _Change, now: datetime) -> None:
            self._fire(change, event, actor, now)

        return self._run(conversation_id, command, actor, fire)

    def _fire(self, change: _Change, event: Event, actor: Actor, now: datetime) -> None:
        control = change.state.control()
        try:
            outcome = control.apply(event, actor, at=now, hours=self._hours)
        except ControlError as refusal:
            change.state = change.state.with_control(control)  # the REJECTED record is kept
            change.events.append(f"{event}:rejected:{refusal.code}")
            change.refusal = refusal
            return
        change.events.append(f"{event}:applied")
        change.state, facts = apply_effects(change.state.with_control(control), outcome, at=now)
        change.events.extend(facts)
        if outcome.previous is State.BLOCKED and outcome.new is not State.BLOCKED:
            # A supervisor reviewed the conversation: the strikes that blocked it are spent,
            # so the next insult starts counting again instead of blocking at once.
            change.state = replace(change.state, abuse_strikes=0)

    def _run(
        self, conversation_id: str, command: str, actor: Actor, work: _Command
    ) -> CommandResult:
        """Load, sync the hours, do the work, save; replay on a concurrent write.

        `work` raises a `DomainError` BEFORE changing anything when the command is not allowed:
        the command stops, and only what happened before it (a hand-back at closing time) is
        saved. A control refusal put in `change.refusal` is saved with the rest (it is part of
        the audit trail). Either way the error reaches the caller unchanged, after the save.
        """
        for _ in range(_SAVE_ATTEMPTS):
            loaded = self._store.load(conversation_id)
            base_version = 0 if loaded is None else loaded.version
            state = loaded if loaded is not None else ConversationState(conversation_id)
            now = self._clock.now()
            change = _Change(state)
            self._sync_hours(change, now)
            synced = change.state
            try:
                work(change, now)
            except DomainError as refusal:
                if not change.events:
                    raise  # nothing happened at all: nothing to save
                change = _Change(synced, events=change.events, refusal=refusal)
            final = replace(change.state, version=base_version + 1)
            try:
                self._store.save(final, expected_version=base_version)
            except ConcurrentUpdate:
                continue
            if change.refusal is not None:
                raise change.refusal
            return CommandResult(
                conversation_id=conversation_id,
                command=command,
                actor=actor,
                control_state=final.control_state,
                outbound=tuple(change.outbound),
                events=tuple(change.events),
                registered=tuple(change.registered),
            )
        raise ConcurrentUpdate(f"conversation {conversation_id[:40]!r} keeps changing")

    def _sync_hours(self, change: _Change, now: datetime) -> None:
        control = change.state.control()
        outcome = control.sync_hours(at=now, hours=self._hours)
        change.state = change.state.with_control(control)
        if outcome is not None:
            change.events.append(f"{outcome.event}:applied")
            change.state, facts = apply_effects(change.state, outcome, at=now)
            change.events.extend(facts)


def _is_current(state: ConversationState, confirmed: Confirmed) -> bool:
    """The confirmed action is still the pending one, under the same control version."""
    pending = state.pending.pending
    return (
        pending is not None
        and pending.fingerprint == confirmed.action.fingerprint
        and state.control_version == confirmed.action.control_version
    )


def _require_team(actor: Actor, command: str) -> None:
    if actor not in _TEAM:
        raise CommandNotAllowed(f"command={command} actor={actor}")
