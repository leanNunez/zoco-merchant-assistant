"""Runs one kit scenario end to end and returns the contract's output.

Each scenario gets a fresh, isolated world (see `world.py`): its own SQLite file in a temporary
directory, a fixed clock and a manual ticker. Nothing leaks from one scenario to the next, and
running the same scenario twice gives the same output.

The order follows the scenario: interleaved conversations, the starting state, the restart, and
then either a follow-up decision (no model involved) or a run, with the takeover applied before
it or, through the model, in the middle of it.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cortex.bootstrap import load_harness_data
from cortex.chat.events import (
    FOLLOWUP,
    EventOutOfScope,
    InvalidEvent,
    event_type,
    read_followup,
    read_takeover,
)
from cortex.chat.followup import FollowupDecision
from cortex.chat.service import RunIdConflict
from cortex.config import RunLimits
from cortex.identity import AuthContext, AuthenticationFailed
from cortex.kit_adapter.observables import ScenarioFacts, build_output, refused_observables
from cortex.kit_adapter.preconditions import Setup, read_faults, read_preconditions
from cortex.kit_adapter.scenario import Scenario, UnsupportedScenario, load_scenario, run_id_for
from cortex.kit_adapter.world import TakeoverDuringRun, World
from cortex.runner.agent_runner import RunResult
from cortex.runner.finish import REASON_OF, FinishDetail, FinishReason, missing_failure_event
from cortex.runner.model import ModelPort
from cortex.runner.scripted_model import ScriptedModel

# The instant of the contract's example. Fixed, so a decision that depends on "today" does not
# change with the day the evaluation is run; `--now` overrides it.
DEFAULT_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def run_scenario_file(
    path: Path, *, kit_dir: Path, now: datetime = DEFAULT_NOW, workdir: Path | None = None
) -> dict[str, object]:
    """Run a scenario file. With `workdir`, the world stays there for inspection afterwards."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        raise UnsupportedScenario(f"cannot read {path.name}: {unreadable}") from None
    if not isinstance(raw, dict):
        raise UnsupportedScenario("a scenario must be a JSON object")
    scenario = load_scenario(raw)
    if workdir is not None:
        workdir.mkdir(parents=True, exist_ok=True)
        return run_scenario(scenario, kit_dir=kit_dir, workdir=workdir, now=now)
    with tempfile.TemporaryDirectory(prefix="cortex-") as temporary:
        return run_scenario(scenario, kit_dir=kit_dir, workdir=Path(temporary), now=now)


def run_scenario(
    scenario: Scenario, *, kit_dir: Path, workdir: Path, now: datetime
) -> dict[str, object]:
    # The scenario's own run id for the main run; interleaved runs keep their derived ids.
    run_id = scenario.run_id or run_id_for(scenario.conversation_id)
    data = load_harness_data(kit_dir)
    setup = read_preconditions(scenario.preconditions, data.config.limits)
    world = World(
        data, workdir, now, read_faults(scenario.faults, data.catalog.names), setup.limits
    )
    conversation_id = scenario.conversation_id
    try:
        auth = data.directory.authenticate(scenario.tenant_id, scenario.principal_id)
        # Every interleaved writer too, before anything runs: one that does not belong to its
        # company refuses the whole scenario, exactly like the main principal.
        writers = tuple(world.auth_for(e.tenant_id, e.principal_id) for e in setup.interleaved)
    except AuthenticationFailed:
        return _unauthenticated(scenario, run_id, setup.limits, now)

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
        if event is not None:
            model = _takeover(world, auth, conversation_id, event, model, chat_log)
        result = world.run(auth, run_id, conversation_id, scenario.message, setup.limits, model)

    store = world.database.for_tenant(auth)
    applied = any(entry["type"] == "chat_event_applied" for entry in chat_log)
    facts = ScenarioFacts(
        contexts_separated=separated,
        state_after_restart=after_restart.mode if after_restart else None,
        pending_action_retained=after_restart.pending_retained if after_restart else None,
        takeover_seq=store.chat.last_takeover_seq(conversation_id) if applied else None,
        decision=decision.decision.value if decision else None,
        decision_reasons=decision.reasons if decision else (),
        chat_events=tuple(chat_log),
    )
    return build_output(result, store=store, counter=world.counter, audit=world.audit, facts=facts)


# --------------------------------------------------------------------------- restart


@dataclass(frozen=True, slots=True)
class _AfterRestart:
    mode: str | None
    pending_retained: bool


def _restart(world: World, auth: AuthContext, conversation_id: str, setup: Setup) -> _AfterRestart:
    """Restart, then read what survived with the objects of the new process."""
    world.restart()
    store = world.database.for_tenant(auth)
    conversation = store.conversation(conversation_id)
    still_open = {action.operation_id for action in store.ledger.open_actions(conversation_id)}
    return _AfterRestart(
        mode=conversation.mode.value if conversation else None,
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
) -> ModelPort:
    """Apply the takeover now, or arrange for it to land in the middle of the run."""
    event = dict(raw)
    when = event.pop("when", None)
    try:
        takeover = read_takeover(event, tenant_id=auth.tenant_id, conversation_id=conversation_id)
    except EventOutOfScope as refused:
        log.append({"type": "chat_event_rejected", "reason": refused.code})
        return model
    except InvalidEvent as invalid:
        raise UnsupportedScenario(invalid.detail) from None

    def take_over() -> None:
        try:
            outcome = world.chat.take_over(auth, conversation_id, takeover)
        except AuthenticationFailed:
            log.append({"type": "chat_event_rejected", "reason": "auth_invalid"})
            return
        log.append(
            {
                "type": "chat_event_applied" if outcome.applied else "chat_event_ignored",
                "version": outcome.version,
                "responsible": outcome.responsible,
            }
        )

    if when in (None, "before_run"):
        take_over()
        return model
    return TakeoverDuringRun(model, str(when), take_over)


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
        facts = read_followup(raw, tenant_id=auth.tenant_id, conversation_id=conversation_id)
    except EventOutOfScope:
        # The denial's tool_rejected is added at the end, like for any run that ends BLOCKED.
        tracer.emit("chat_event_rejected", step=0, reason="scope_violation")
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
    missing = missing_failure_event(reason, (event.type for event in tracer.events))
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
    scenario: Scenario, run_id: str, limits: RunLimits, now: datetime
) -> dict[str, object]:
    """The caller is not who it claims: nothing runs and nothing is stored.

    The refusal is reported by the event that says the run itself was refused and, like any run
    that ends BLOCKED, by the `tool_rejected` the contract asks every denial for.
    """
    stamp = now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    refusal = {"timestamp": stamp, "reason": FinishDetail.AUTH_INVALID.value}
    events: list[dict[str, object]] = [{"seq": 1, "type": "run_rejected", **refusal}]
    missing = missing_failure_event(FinishReason.BLOCKED, ["run_rejected"])
    if missing is not None:
        events.append({"seq": len(events) + 1, "type": missing, "tool": None, **refusal})
    return {
        "run_id": run_id,
        "tenant_id": scenario.tenant_id,
        "conversation_id": scenario.conversation_id,
        "finish_reason": "BLOCKED",
        "finish_detail": "auth_invalid",
        "step_count": 0,
        "max_steps": limits.max_steps,
        "events": events,
        "effects": {"claims_created": 0, "customer_messages_sent": 0},
        "state": {"conversation_mode": None, "control_version": None},
        **refused_observables(),
    }
