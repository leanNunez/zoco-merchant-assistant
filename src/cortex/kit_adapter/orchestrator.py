"""Runs one kit scenario end to end and returns the contract's output.

Each scenario gets a fresh, isolated world: its own SQLite file in a temporary directory, a
fixed clock and a manual ticker. Nothing leaks from one scenario to the next, and running the
same scenario twice gives the same output.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from cortex.bootstrap import build_runner, load_harness_data, zoco_tool_binder
from cortex.config import ConfigError, RunLimits
from cortex.identity import AuthenticationFailed
from cortex.kit_adapter.observables import CallCounter, build_output
from cortex.kit_adapter.scenario import Scenario, UnsupportedScenario, load_scenario
from cortex.memory.database import Database
from cortex.runner.agent_runner import RunRequest
from cortex.runner.budget import ManualTicker
from cortex.runner.scripted_model import ScriptedModel
from merchant_assistant.shared.clock import FixedClock

# The instant of the contract's example. Fixed, so a decision that depends on "today" does not
# change with the day the evaluation is run; `--now` overrides it.
DEFAULT_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def run_scenario_file(
    path: Path, *, kit_dir: Path, now: datetime = DEFAULT_NOW
) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        raise UnsupportedScenario(f"cannot read {path.name}: {unreadable}") from None
    if not isinstance(raw, dict):
        raise UnsupportedScenario("a scenario must be a JSON object")
    scenario = load_scenario(raw)
    with tempfile.TemporaryDirectory(prefix="cortex-") as workdir:
        return run_scenario(scenario, kit_dir=kit_dir, workdir=Path(workdir), now=now)


def run_scenario(
    scenario: Scenario, *, kit_dir: Path, workdir: Path, now: datetime
) -> dict[str, object]:
    data = load_harness_data(kit_dir)
    limits = apply_preconditions(data.config.limits, scenario.preconditions)
    if scenario.faults or scenario.incoming_event is not None:
        raise UnsupportedScenario("faults and incoming events are not supported yet")

    clock = FixedClock(now)
    database = Database(workdir / "cortex.db", clock)
    database.initialize()
    run_id = f"run-{scenario.conversation_id}"
    try:
        auth = data.directory.authenticate(scenario.tenant_id, scenario.principal_id)
    except AuthenticationFailed:
        return _unauthenticated(scenario, run_id, limits)

    counter = CallCounter()
    runner = build_runner(
        data=data,
        database=database,
        model=ScriptedModel(scenario.model_script),
        bind_tools=counter.wrap(zoco_tool_binder(data)),
        clock=clock,
        ticker=ManualTicker(),
    )
    result = runner.run(
        RunRequest(
            run_id=run_id,
            conversation_id=scenario.conversation_id,
            message=scenario.message,
            auth=auth,
            limits=limits,
        )
    )
    return build_output(result, store=database.for_tenant(auth), counter=counter)


def apply_preconditions(limits: RunLimits, preconditions: Mapping[str, object]) -> RunLimits:
    for key, value in preconditions.items():
        if key != "max_steps":
            raise UnsupportedScenario(f"precondition {key!r} is not supported")
        if type(value) is not int:
            raise UnsupportedScenario("max_steps must be an integer")
        try:
            limits = limits.with_max_steps(value)
        except ConfigError as invalid:
            raise UnsupportedScenario(invalid.detail) from None
    return limits


def _unauthenticated(scenario: Scenario, run_id: str, limits: RunLimits) -> dict[str, object]:
    """The caller is not who it claims: nothing runs and nothing is stored."""
    return {
        "run_id": run_id,
        "tenant_id": scenario.tenant_id,
        "conversation_id": scenario.conversation_id,
        "finish_reason": "BLOCKED",
        "finish_detail": "auth_invalid",
        "step_count": 0,
        "max_steps": limits.max_steps,
        "events": [{"seq": 1, "type": "run_rejected", "reason": "auth_invalid"}],
        "effects": {"claims_created": 0, "customer_messages_sent": 0},
        "state": {"conversation_mode": None, "control_version": None},
    }
