"""Runs one kit scenario end to end and returns the contract's output.

Each scenario gets a fresh, isolated world (see `world.py`): its own SQLite file in a temporary
directory, a fixed clock and a manual ticker. Nothing leaks from one scenario to the next, and
running the same scenario twice gives the same output.

The order follows the scenario: interleaved conversations, the starting state, the restart, and
then either a follow-up decision (no model involved) or a run, with the takeover applied before
it, after the model's first tool request, or right before the run's first delivery attempt (right
after the run when it requested no tool or tried no delivery).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from cortex.bootstrap import BUSINESS_TIMEZONE, DEFAULT_TRACE_KEY
from cortex.chat.events import (
    FOLLOWUP,
    EventOutOfScope,
    InvalidEvent,
    event_type,
    named_conversation,
    read_followup,
    read_takeover,
)
from cortex.chat.followup import FollowupDecision
from cortex.chat.service import RunIdConflict
from cortex.config import RunLimits
from cortex.identity import AuthContext, AuthenticationFailed
from cortex.kit_adapter.corpora import load_kit_data
from cortex.kit_adapter.observables import ScenarioFacts, build_output, refused_observables
from cortex.kit_adapter.preconditions import Setup, read_faults, read_preconditions
from cortex.kit_adapter.scenario import (
    Scenario,
    UnsupportedScenario,
    load_scenario,
    run_id_for,
)
from cortex.kit_adapter.scenario import conversation_id as checked_conversation_id
from cortex.kit_adapter.world import (
    AFTER_REPLY,
    AFTER_TOOL_REQUEST,
    BEFORE_RUN,
    TakeoverBeforeDelivery,
    TakeoverDuringRun,
    World,
    interleaved_run_ids,
)
from cortex.runner.agent_runner import RunResult
from cortex.runner.finish import REASON_OF, FinishDetail, FinishReason, missing_failure_event
from cortex.runner.model import ModelPort
from cortex.runner.scripted_model import ScriptedModel

# The instant of the contract's example. Fixed, so a decision that depends on "today" does not
# change with the day the evaluation is run; `--now` overrides it.
DEFAULT_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


@contextmanager
def private_files() -> Iterator[None]:
    """Everything created inside is readable by its owner only (SQLite files and directories).

    The umask belongs to the process, so it is restored on the way out even when the run fails:
    tests call the adapter in-process and must not inherit a stricter one.
    """
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def run_scenario_file(
    path: Path,
    *,
    kit_dir: Path,
    now: datetime = DEFAULT_NOW,
    workdir: Path | None = None,
    trace_key: str = DEFAULT_TRACE_KEY,
) -> dict[str, object]:
    """Run a scenario file. With `workdir`, the world stays there for inspection afterwards.
    `trace_key` keys the principal pseudonym of the traces."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        raise UnsupportedScenario(f"cannot read {path.name}: {unreadable}") from None
    if not isinstance(raw, dict):
        raise UnsupportedScenario("a scenario must be a JSON object")
    scenario = load_scenario(raw)
    with private_files():
        if workdir is not None:
            workdir.mkdir(parents=True, exist_ok=True)
            return run_scenario(
                scenario, kit_dir=kit_dir, workdir=workdir, now=now, trace_key=trace_key
            )
        with tempfile.TemporaryDirectory(prefix="cortex-") as temporary:
            return run_scenario(
                scenario, kit_dir=kit_dir, workdir=Path(temporary), now=now, trace_key=trace_key
            )


def run_scenario(
    scenario: Scenario,
    *,
    kit_dir: Path,
    workdir: Path,
    now: datetime,
    trace_key: str = DEFAULT_TRACE_KEY,
) -> dict[str, object]:
    conversation_id = _acting_conversation(scenario)
    # The scenario's own run id for the main run; interleaved runs keep their derived ids.
    run_id = scenario.run_id or run_id_for(conversation_id)
    data = load_kit_data(kit_dir)
    unknown_tools = (scenario.tools or frozenset()) - set(data.catalog.names)
    if unknown_tools:
        raise UnsupportedScenario(f"tools names unknown tools: {sorted(unknown_tools)}")
    setup = read_preconditions(scenario.preconditions, data.config.limits)
    if scenario.run_id is not None and scenario.run_id in interleaved_run_ids(setup):
        raise UnsupportedScenario(f"the run_id {scenario.run_id!r} is an interleaved run's id")
    world = World(
        data,
        workdir,
        now,
        read_faults(scenario.faults, data.catalog.names),
        setup.limits,
        trace_key,
    )
    try:
        auth = data.directory.authenticate(scenario.tenant_id, scenario.principal_id)
        # Every interleaved writer too, before anything runs: one that does not belong to its
        # company refuses the whole scenario, exactly like the main principal.
        writers = tuple(world.auth_for(e.tenant_id, e.principal_id) for e in setup.interleaved)
    except AuthenticationFailed:
        return _unauthenticated(scenario, conversation_id, run_id, setup.limits, now)

    separated = world.interleave(setup, writers) if setup.interleaved else None
    world.seed(auth, conversation_id, setup)
    after_restart = _restart(world, auth, conversation_id, setup) if setup.restart else None

    event = scenario.incoming_event
    chat_log: list[dict[str, object]] = []
    decision: FollowupDecision | None = None
    if event is not None and _kind(event) == FOLLOWUP:
        result, decision = _follow_up(world, auth, conversation_id, run_id, event, setup.limits)
    else:
        model: ModelPort = ScriptedModel(scenario.model_script)
        landing: TakeoverBeforeDelivery | None = None
        if event is not None:
            model, landing = _takeover(world, auth, conversation_id, event, model, chat_log)
        result = world.run(
            auth,
            run_id,
            conversation_id,
            scenario.message,
            setup.limits,
            model,
            tools=scenario.tools,
            expected_control_version=scenario.expected_control_version,
            before_delivery=landing.land if landing else None,
        )
        if landing is not None:
            landing.land()  # the run never tried to deliver: the takeover lands after it
        if isinstance(model, TakeoverDuringRun):
            model.land()  # the run never asked for a tool: the takeover lands after it
        result = _with_refused_events(world, auth, result, chat_log)

    store = world.database.for_tenant(auth)
    applied = any(entry["type"] == "chat_event_applied" for entry in chat_log)
    # The mode after a restart is the mode at the end of the run the restarted process made.
    final = store.conversation(conversation_id)
    facts = ScenarioFacts(
        contexts_separated=separated,
        state_after_restart=(final.mode.value if final else None) if after_restart else None,
        pending_action_retained=after_restart.pending_retained if after_restart else None,
        takeover_seq=store.chat.last_takeover_seq(conversation_id) if applied else None,
        decision=decision.decision.value if decision else None,
        decision_reasons=decision.reasons if decision else (),
        chat_events=tuple(chat_log),
    )
    return build_output(result, store=store, counter=world.counter, audit=world.audit, facts=facts)


def _acting_conversation(scenario: Scenario) -> str:
    """The conversation the scenario acts on: the one its event names, when the event is of the
    scenario's company (Zoco Chat says which conversation the event is about). An event of
    another company names nothing to act on, and is refused later as out of scope."""
    event = scenario.incoming_event
    named = None if event is None else named_conversation(event, tenant_id=scenario.tenant_id)
    return scenario.conversation_id if named is None else checked_conversation_id(named)


# --------------------------------------------------------------------------- restart


@dataclass(frozen=True, slots=True)
class _AfterRestart:
    pending_retained: bool


def _restart(world: World, auth: AuthContext, conversation_id: str, setup: Setup) -> _AfterRestart:
    """Restart, then read what survived with the objects of the new process."""
    world.restart()
    store = world.database.for_tenant(auth)
    still_open = {action.operation_id for action in store.ledger.open_actions(conversation_id)}
    return _AfterRestart(
        pending_retained=all(operation_id in still_open for _, operation_id in setup.pending),
    )


# --------------------------------------------------------------------------- Zoco Chat events


def _kind(event: Mapping[str, object]) -> str:
    try:
        return event_type(event)
    except InvalidEvent as invalid:
        raise UnsupportedScenario(invalid.detail) from None


def _takeover(
    world: World,
    auth: AuthContext,
    conversation_id: str,
    raw: Mapping[str, object],
    model: ModelPort,
    log: list[dict[str, object]],
) -> tuple[ModelPort, TakeoverBeforeDelivery | None]:
    """Apply the takeover now, or arrange for it to land in the middle of the run: right after
    the model's first tool request, or right before the run's first delivery attempt."""
    event = dict(raw)
    when = event.pop("when", None)
    when = when.lower() if isinstance(when, str) else when
    try:
        takeover = read_takeover(event, tenant_id=auth.tenant_id, conversation_id=conversation_id)
    except EventOutOfScope as refused:
        log.append({"type": "chat_event_rejected", "reason": refused.code})
        return model, None
    except InvalidEvent as invalid:
        raise UnsupportedScenario(invalid.detail) from None

    def take_over() -> None:
        try:
            outcome = world.chat.take_over(auth, conversation_id, takeover)
        except AuthenticationFailed:
            log.append({"type": "chat_event_rejected", "reason": AuthenticationFailed.code})
            return
        entry: dict[str, object] = {
            "type": "chat_event_applied" if outcome.applied else "chat_event_ignored",
            "version": outcome.version,
            "responsible": outcome.responsible,
        }
        if outcome.conflict:
            entry["conflict"] = True
        log.append(entry)

    if when in (None, BEFORE_RUN):
        take_over()
        return model, None
    if when == AFTER_TOOL_REQUEST:
        return TakeoverDuringRun(model, take_over), None
    if when == AFTER_REPLY:
        return model, TakeoverBeforeDelivery(take_over)
    raise UnsupportedScenario(f"unknown moment for the takeover: {when!r}")


def _with_refused_events(
    world: World, auth: AuthContext, result: RunResult, log: list[dict[str, object]]
) -> RunResult:
    """A takeover refused for its company or its actor is an authorization denial.

    Like the follow-up path, the denial is traced as `chat_event_rejected` plus the
    `tool_rejected` with its reason that the contract asks every denial for. Both are added to
    the run's trace after its own events, written through when the run's trace was stored. The
    run itself is unchanged: a refused takeover changes nothing.
    """
    refusals = [entry for entry in log if entry["type"] == "chat_event_rejected"]
    if not refusals:
        return result
    stored = world.database.for_tenant(auth).traces.events_of(result.run_id)
    tracer = world.chat.tracer(
        auth,
        result.conversation_id,
        result.run_id,
        first_seq=len(result.events) + 1,
        write_through=len(stored) == len(result.events),
    )
    for refusal in refusals:
        reason = str(refusal["reason"])
        tracer.emit("chat_event_rejected", step=result.step_count, reason=reason)
        tracer.emit("tool_rejected", step=result.step_count, tool=None, reason=reason)
    return replace(result, events=(*result.events, *tracer.events))


def _follow_up(
    world: World,
    auth: AuthContext,
    conversation_id: str,
    run_id: str,
    raw: Mapping[str, object],
    limits: RunLimits,
) -> tuple[RunResult, FollowupDecision | None]:
    """Decide a due follow-up by code. The model is never asked and nothing is sent."""
    tracer = world.chat.tracer(auth, conversation_id, run_id)
    decision: FollowupDecision | None = None
    try:
        facts = read_followup(
            raw,
            tenant_id=auth.tenant_id,
            conversation_id=conversation_id,
            business_tz=BUSINESS_TIMEZONE,
        )
    except EventOutOfScope:
        # The denial's tool_rejected is added at the end, like for any run that ends BLOCKED.
        tracer.emit("chat_event_rejected", step=0, reason=EventOutOfScope.code)
        detail = FinishDetail.SCOPE_VIOLATION
    except InvalidEvent as invalid:
        raise UnsupportedScenario(invalid.detail) from None
    else:
        try:
            decision = world.chat.follow_up(auth, conversation_id, run_id, facts, tracer)
            detail = FinishDetail.FOLLOWUP_DECIDED
        except RunIdConflict:
            detail = FinishDetail.RUN_ID_CONFLICT
    reason = REASON_OF[detail]
    missing = missing_failure_event(
        detail, ((event.type, event.attributes.get("reason")) for event in tracer.events)
    )
    if missing is not None:
        tracer.emit(missing, step=0, tool=None, reason=detail.value)
    tracer.emit("run_finished", step=0, finish_reason=reason.value, finish_detail=detail.value)
    result = RunResult(
        run_id=run_id,
        tenant_id=auth.tenant_id,
        conversation_id=conversation_id,
        finish_reason=reason,
        finish_detail=detail,
        step_count=0,
        max_steps=limits.max_steps,
        events=tracer.events,
    )
    return result, decision


def _unauthenticated(
    scenario: Scenario, conversation_id: str, run_id: str, limits: RunLimits, now: datetime
) -> dict[str, object]:
    """The caller is not who it claims: nothing runs and nothing is stored.

    The refusal is reported by the event that says the run itself was refused and, like any run
    that ends BLOCKED, by the `tool_rejected` the contract asks every denial for.
    """
    stamp = now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    # Named like every other event, from the request itself: nothing is stored for this caller.
    ids = {"run_id": run_id, "tenant_id": scenario.tenant_id, "conversation_id": conversation_id}
    refusal = {"timestamp": stamp, "reason": FinishDetail.AUTH_INVALID.value}
    events: list[dict[str, object]] = [{"seq": 1, "type": "run_rejected", **ids, **refusal}]
    missing = missing_failure_event(
        FinishDetail.AUTH_INVALID, [("run_rejected", FinishDetail.AUTH_INVALID.value)]
    )
    if missing is not None:
        events.append({"seq": len(events) + 1, "type": missing, **ids, "tool": None, **refusal})
    return {
        "run_id": run_id,
        "tenant_id": scenario.tenant_id,
        "conversation_id": conversation_id,
        "finish_reason": FinishReason.BLOCKED.value,
        "finish_detail": FinishDetail.AUTH_INVALID.value,
        "step_count": 0,
        "max_steps": limits.max_steps,
        "events": events,
        "effects": {"claims_created": 0, "customer_messages_sent": 0, "ai_replies_delivered": 0},
        "state": {"conversation_mode": None, "control_version": None},
        **refused_observables(),
    }
