"""The contract's rule: a failure or a denial leaves a tool_rejected or tool_error event.

"En un escenario de fallo o autorizacion denegada se debe incluir evento tool_rejected o
tool_error, con razon y sin secretos." Each test runs a real scenario (or the runner itself) and
reads the trace it returned.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cortex.bootstrap import HarnessData, build_runner, zoco_tool_binder
from cortex.kit_adapter.orchestrator import run_scenario_file
from cortex.memory.database import Database
from cortex.runner.agent_runner import RunRequest, RunResult
from cortex.runner.finish import FinishDetail, FinishReason
from cortex.runner.scripted_model import ScriptedModel
from cortex.timekeeping import ManualTicker
from cortex.tools.adapters.claims import ClaimsDesk
from merchant_assistant.shared.clock import FixedClock

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES, KIT = ROOT / "examples", ROOT / "kit"
FAILURE_EVENTS = {"tool_rejected", "tool_error"}


def _run(tmp_path: Path, base: str, **changes: Any) -> dict[str, Any]:
    data = json.loads((EXAMPLES / f"{base}.json").read_text(encoding="utf-8"))
    data["preconditions"] = {**data.get("preconditions", {}), **changes.pop("preconditions", {})}
    data.update(changes)
    path = tmp_path / f"{base}-variant.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    output: dict[str, Any] = run_scenario_file(path, kit_dir=KIT)
    return output


def _of(events: list[dict[str, Any]], *types: str) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] in types]


# --------------------------------------------------------------------------- reads


def test_t07_each_failed_read_attempt_is_a_tool_error_before_its_retry(tmp_path: Path) -> None:
    output = _run(tmp_path, "T07")
    kinds = [event["type"] for event in output["events"]]
    error = _of(output["events"], "tool_error")

    assert kinds.index("tool_error") < kinds.index("tool_retry") < kinds.index("tool_executed")
    assert len(error) == 1
    assert (error[0]["reason"], error[0]["transient"], error[0]["attempt"]) == ("timeout", True, 1)
    # The retry semantics are unchanged.
    assert (output["retry_count"], output["executed_tools"]) == (1, ["consultar_ventas"])


def test_every_failed_attempt_has_its_own_tool_error_numbered(tmp_path: Path) -> None:
    output = _run(
        tmp_path, "T07", faults={"consultar_ventas": ["TIMEOUT", "TIMEOUT", "TIMEOUT", "SUCCESS"]}
    )
    errors = _of(output["events"], "tool_error")

    assert [(e["attempt"], e["reason"], e["transient"]) for e in errors] == [
        (1, "timeout", True),
        (2, "timeout", True),
        (3, "timeout", True),
    ]
    assert output["retry_count"] == 2


# --------------------------------------------------------------------------- writes


def test_an_uncertain_write_attempt_is_a_tool_error_marked_uncertain(tmp_path: Path) -> None:
    script = json.loads((EXAMPLES / "T09.json").read_text())["model_script"]
    output = _run(
        tmp_path,
        "T09",
        model_script=[script[0], script[-1]],
        faults={"crear_reclamo": ["TIMEOUT", "TIMEOUT"]},
    )
    errors = _of(output["events"], "tool_error")

    assert "write_attempt_failed" not in [event["type"] for event in output["events"]]
    assert [(e["reason"], e["uncertain"]) for e in errors] == [
        ("outcome_unknown", True),
        ("outcome_unknown", True),
    ]
    assert output["finish_detail"] == "write_outcome_unknown"


# --------------------------------------------------------------------------- run endings


def _ending(output: dict[str, Any]) -> list[str]:
    return [event["type"] for event in output["events"]][-2:]


def test_a_blocked_conversation_says_why_with_one_tool_rejected(tmp_path: Path) -> None:
    output = _run(tmp_path, "T10", preconditions={"initial_state": "BLOCKED"})
    rejected = _of(output["events"], *FAILURE_EVENTS)

    assert (output["finish_reason"], output["finish_detail"]) == ("BLOCKED", "conversation_blocked")
    assert _ending(output) == ["tool_rejected", "run_finished"]
    assert [(e["type"], e["reason"]) for e in rejected] == [
        ("tool_rejected", "conversation_blocked")
    ]
    assert output["rejected_tools"] == []  # no tool was asked for, so none is listed


def test_a_model_that_fails_ends_the_run_with_a_tool_error(tmp_path: Path) -> None:
    output = _run(tmp_path, "T01", model_script=[])

    assert (output["finish_reason"], output["finish_detail"]) == ("FAILED", "script_exhausted")
    assert _ending(output) == ["tool_error", "run_finished"]
    assert _of(output["events"], "tool_error")[0]["reason"] == "script_exhausted"


def test_a_run_that_already_said_why_gets_no_extra_event(tmp_path: Path) -> None:
    output = _run(tmp_path, "T05")

    assert output["finish_reason"] == "BLOCKED"
    assert len(_of(output["events"], *FAILURE_EVENTS)) == 1  # the scope rejection itself


def _runner(tmp_path: Path, harness_data: HarnessData, script: list[dict[str, Any]]) -> Any:
    clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
    database = Database(tmp_path / "cortex.db", clock)
    database.initialize()
    return build_runner(
        data=harness_data,
        database=database,
        model=ScriptedModel(script),
        bind_tools=zoco_tool_binder(harness_data, ClaimsDesk(tmp_path / "claims", clock)),
        clock=clock,
        ticker=ManualTicker(),
    )


def test_an_admission_refusal_ends_with_a_tool_rejected(
    tmp_path: Path, harness_data: HarnessData
) -> None:
    runner = _runner(tmp_path, harness_data, [{"final": "OK"}, {"final": "OK"}])
    auth = harness_data.directory.authenticate("comercio_a", "operador_a")
    limits = harness_data.config.limits
    runner.run(RunRequest("run-1", "conv_1", "Hola", auth, limits))

    result: RunResult = runner.run(RunRequest("run-1", "conv_1", "otro mensaje", auth, limits))

    assert result.finish_detail is FinishDetail.RUN_ID_CONFLICT
    assert result.finish_reason is FinishReason.BLOCKED
    assert [event.type for event in result.events][-2:] == ["tool_rejected", "run_finished"]
    assert result.events[-2].attributes["reason"] == "run_id_conflict"
