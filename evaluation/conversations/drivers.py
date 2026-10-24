"""Running the steps of a scenario against an agent and writing down what happened.

`drive` plays the steps in order against anything that implements `AgentUnderTest`: the real
agent (`RealAgent`, the production `ConversationService` and `OperatorService` over one store)
or the unsafe reference agent of `naive.py`. After every step it reads the number of rows
straight from the SQLite file, so a check can say "zero rows right after the first yes". At the
end `observe` adds the stored conversation (history, control log, inbox, drafts) and the rows.

Only what the customer actually received counts as visible: replies, code notices and operator
messages. A draft is recorded apart, because in SUGGEST it has not been sent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from evaluation.conversations.checks import parse_confirmation
from evaluation.conversations.observation import (
    DraftFact,
    HistoryEntry,
    InboxEntry,
    LogEntry,
    ModelCall,
    Observation,
    RowFact,
    StepFact,
    StepKind,
    ToolFact,
    Visible,
    VisibleKind,
)
from evaluation.conversations.scenario import (
    FaultPlan,
    MoveClock,
    Say,
    SetFaults,
    Step,
    Team,
    TeamCommand,
    When,
)
from merchant_assistant.agent.application.conversation import (
    ConversationService,
    Outbound,
    ToolRecord,
    TurnResult,
)
from merchant_assistant.agent.application.operator import CommandResult, OperatorService
from merchant_assistant.agent.domain.state import ConversationState
from merchant_assistant.agent.ports import ConversationStore
from merchant_assistant.shared.errors import DomainError

# The code-owned `execution_failed` template (agent/domain/templates.py); pinned by a test.
EXECUTION_FAILED_MARK = "no pude confirmar si el reclamo quedó guardado"
NO_MATCHING_DRAFT = "no_matching_draft"


@dataclass(frozen=True)
class Reaction:
    """What one step did, as the agent under test reports it."""

    visible: tuple[tuple[VisibleKind, str], ...] = ()
    drafts: tuple[str, ...] = ()
    tools: tuple[ToolRecord, ...] = ()
    events: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    model_calls: int = 0
    gate: str | None = None
    model_error: str | None = None
    error_code: str | None = None
    injection_markers: int = 0
    state_after: str | None = None


@dataclass(frozen=True)
class Snapshot:
    history: tuple[HistoryEntry, ...] = ()
    control_log: tuple[LogEntry, ...] = ()
    inbox: tuple[InboxEntry, ...] = ()
    drafts: tuple[tuple[str, str, str, str], ...] = ()  # (id, kind, text, status)
    final_state: str | None = None


class AgentUnderTest(Protocol):
    def say(self, text: str) -> Reaction: ...

    def command(self, step: Team) -> Reaction: ...

    def snapshot(self) -> Snapshot: ...


class Controls(Protocol):
    """What the harness can do to the world between steps."""

    def move_clock(self, instant: datetime) -> None: ...

    def set_faults(self, tool: str, plan: FaultPlan | None) -> None: ...

    def count_rows(self) -> int: ...


StopRule = Callable[[StepFact], str | None]


@dataclass(frozen=True)
class Drive:
    steps: tuple[StepFact, ...]
    tools: tuple[ToolFact, ...]
    drafts_by_step: tuple[tuple[str, int], ...]  # (draft id, the step that created it)
    stopped: str | None  # why the run stopped early (live: the provider failed), or None


def drive(
    steps: Sequence[Step],
    agent: AgentUnderTest,
    controls: Controls,
    *,
    stop: StopRule | None = None,
) -> Drive:
    facts: list[StepFact] = []
    tools: list[ToolFact] = []
    created: list[tuple[str, int]] = []
    last_visible: str | None = None
    for index, step in enumerate(steps):
        fact, records = _play(index, step, agent, controls, last_visible)
        facts.append(fact)
        tools.extend(records)
        created.extend((draft_id, index) for draft_id in fact.drafts)
        if fact.visible:
            last_visible = fact.visible[-1].text
        reason = stop(fact) if stop is not None else None
        if reason is not None:
            return Drive(tuple(facts), tuple(tools), tuple(created), reason)
    return Drive(tuple(facts), tuple(tools), tuple(created), None)


def observe(
    driven: Drive,
    snapshot: Snapshot,
    rows: tuple[RowFact, ...],
    *,
    model_calls: tuple[ModelCall, ...],
    system_prompts: tuple[str, ...],
    public_texts: tuple[str, ...],
) -> Observation:
    step_of = dict(driven.drafts_by_step)
    drafts = tuple(
        DraftFact(step_of.get(draft_id, -1), draft_id, kind, text, status)
        for draft_id, kind, text, status in snapshot.drafts
    )
    return Observation(
        steps=driven.steps,
        tools=driven.tools,
        rows=rows,
        history=snapshot.history,
        control_log=snapshot.control_log,
        inbox=snapshot.inbox,
        drafts=drafts,
        final_state=snapshot.final_state,
        model_calls=model_calls,
        system_prompts=system_prompts,
        public_texts=public_texts,
    )


def _play(
    index: int, step: Step, agent: AgentUnderTest, controls: Controls, last_visible: str | None
) -> tuple[StepFact, list[ToolFact]]:
    match step:
        case Say(text=text, tag=tag, when=when):
            if not _holds(when, last_visible):
                return StepFact(
                    index, tag, "user", text, skipped=True, rows_after=controls.count_rows()
                ), []
            reaction = agent.say(text)
            kind: StepKind = "user"
            detail = text
        case Team(tag=tag):
            reaction = agent.command(step)
            kind = "team"
            detail = _describe(step)
        case MoveClock(to=instant, tag=tag):
            controls.move_clock(instant)
            return StepFact(
                index, tag, "clock", instant.isoformat(), rows_after=controls.count_rows()
            ), []
        case SetFaults(tool=tool, plan=plan, tag=tag):
            controls.set_faults(tool, plan)
            shown = "sin fallas" if plan is None else plan.describe()
            return StepFact(
                index, tag, "faults", f"{tool}: {shown}", rows_after=controls.count_rows()
            ), []
    records = [
        ToolFact(index, r.name, r.arguments, r.outcome, r.status, r.attempts, r.error_code)
        for r in reaction.tools
    ]
    fact = StepFact(
        index=index,
        tag=tag,
        kind=kind,
        detail=detail,
        state_after=reaction.state_after,
        rows_after=controls.count_rows(),
        visible=tuple(Visible(index, k, text) for k, text in reaction.visible),
        drafts=reaction.drafts,
        events=reaction.events,
        violations=reaction.violations,
        flags=reaction.flags,
        model_calls=reaction.model_calls,
        gate=reaction.gate,
        model_error=reaction.model_error,
        error_code=reaction.error_code,
        injection_markers=reaction.injection_markers,
    )
    return fact, records


def _holds(when: When, last_visible: str | None) -> bool:
    match when:
        case When.ALWAYS:
            return True
        case When.CONFIRMATION_REQUESTED:
            return last_visible is not None and parse_confirmation(last_visible) is not None
        case When.EXECUTION_FAILED:
            return last_visible is not None and EXECUTION_FAILED_MARK in last_visible


def _describe(step: Team) -> str:
    parts = [str(step.command), str(step.actor)]
    if step.draft_kind is not None:
        parts.append(str(step.draft_kind))
    if step.text is not None:
        parts.append(step.text)
    return " ".join(parts)


# --------------------------------------------------------------------------- the real agent


class RealAgent:
    """The production services over one store, exactly as the console wires them."""

    def __init__(
        self,
        service: ConversationService,
        operators: OperatorService,
        store: ConversationStore,
        conversation_id: str,
    ) -> None:
        self._service = service
        self._operators = operators
        self._store = store
        self._cid = conversation_id

    def say(self, text: str) -> Reaction:
        result: TurnResult = self._service.handle_user_message(self._cid, text)
        return Reaction(
            visible=_visible(result.outbound),
            drafts=result.drafts,
            tools=result.tools,
            events=result.events,
            violations=result.violations,
            flags=result.flags,
            model_calls=result.model_calls,
            gate=result.gate,
            model_error=result.model_error,
            injection_markers=result.injection_markers,
            state_after=str(result.control_state),
        )

    def command(self, step: Team) -> Reaction:
        try:
            result = self._run(step)
        except DomainError as refusal:
            return Reaction(error_code=refusal.code, state_after=self._state())
        if result is None:
            return Reaction(error_code=NO_MATCHING_DRAFT, state_after=self._state())
        return Reaction(
            visible=_visible(result.outbound),
            events=result.events,
            state_after=str(result.control_state),
        )

    def _run(self, step: Team) -> CommandResult | None:
        cid, actor, team = self._cid, step.actor, self._operators
        match step.command:
            case TeamCommand.TAKEOVER:
                return team.takeover(cid, actor)
            case TeamCommand.RELEASE:
                return team.release(cid, actor)
            case TeamCommand.BLOCK:
                return team.block(cid, actor)
            case TeamCommand.UNBLOCK:
                return team.unblock(cid, actor)
            case TeamCommand.REPLY:
                return team.operator_reply(cid, actor, step.text or "")
            case TeamCommand.APPROVE | TeamCommand.REJECT:
                waiting = [d for d in team.drafts(cid) if d.kind is step.draft_kind]
                if not waiting:
                    return None
                if step.command is TeamCommand.APPROVE:
                    return team.approve_draft(cid, waiting[0].id, actor)
                return team.reject_draft(cid, waiting[0].id, actor)

    def _state(self) -> str:
        stored = self._store.load(self._cid)
        return str((stored or ConversationState(self._cid)).control_state)

    def snapshot(self) -> Snapshot:
        stored = self._store.load(self._cid)
        if stored is None:
            return Snapshot(final_state=str(ConversationState(self._cid).control_state))
        return Snapshot(
            history=tuple(
                HistoryEntry(
                    role=str(m.role),
                    author=str(m.author),
                    content=m.content,
                    tool_calls=tuple((c.id, c.name, c.arguments) for c in m.tool_calls),
                    tool_call_id=m.tool_call_id,
                )
                for m in stored.messages
            ),
            control_log=tuple(
                LogEntry(
                    previous=str(r.previous_state),
                    new=str(r.new_state),
                    event=str(r.event),
                    actor=str(r.actor),
                    outcome=str(r.outcome),
                    reason=r.reason,
                    at=r.at.isoformat(),
                )
                for r in stored.control_log
            ),
            inbox=tuple(InboxEntry(n.code, n.detail, n.at.isoformat()) for n in stored.inbox),
            drafts=tuple((d.id, str(d.kind), d.text, str(d.status)) for d in stored.drafts),
            final_state=str(stored.control_state),
        )


def _visible(outbound: Sequence[Outbound]) -> tuple[tuple[VisibleKind, str], ...]:
    kinds: dict[str, VisibleKind] = {"reply": "reply", "notice": "notice", "operator": "operator"}
    return tuple((kinds[m.kind], m.text) for m in outbound if m.kind in kinds)
