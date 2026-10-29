"""Failures of the outside world: transient, permanent and uncertain (brief section 5).

The scenarios are the kit's own, with the `faults` of `kit/fixtures/faults.json` and the limits
IT may vary. Each test reads real effects (the claims files, the ledger) and the trace.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from cortex.kit_adapter.orchestrator import run_scenario_file

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES, KIT = ROOT / "examples", ROOT / "kit"


def _run(tmp_path: Path, base: str, **changes: Any) -> dict[str, Any]:
    data = json.loads((EXAMPLES / f"{base}.json").read_text(encoding="utf-8"))
    preconditions = {**data.get("preconditions", {}), **changes.pop("preconditions", {})}
    data.update(changes)
    data["preconditions"] = preconditions
    path = tmp_path / f"{base}-variant.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    output: dict[str, Any] = run_scenario_file(path, kit_dir=KIT)
    return output


def _types(output: dict[str, Any]) -> list[str]:
    return [event["type"] for event in output["events"]]


def _event(output: dict[str, Any], type_: str) -> dict[str, Any]:
    return next(event for event in output["events"] if event["type"] == type_)


# --------------------------------------------------------------------------- reads


def test_t07_a_transient_read_failure_is_retried_once_and_counted_once(tmp_path: Path) -> None:
    output = _run(tmp_path, "T07")

    assert (output["retry_count"], output["executed_tools"]) == (1, ["consultar_ventas"])
    assert (output["finish_reason"], output["step_count"]) == ("COMPLETED", 2)
    assert _types(output).count("tool_executed") == 1


def test_two_transient_failures_are_still_inside_the_retry_budget(tmp_path: Path) -> None:
    output = _run(tmp_path, "T07", faults={"consultar_ventas": ["TIMEOUT", "TIMEOUT", "SUCCESS"]})

    assert (output["retry_count"], output["executed_tools"]) == (2, ["consultar_ventas"])


def test_when_the_retries_run_out_the_model_gets_a_clean_error_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    output = _run(
        tmp_path, "T07", faults={"consultar_ventas": ["TIMEOUT", "TIMEOUT", "TIMEOUT", "SUCCESS"]}
    )
    error = _event(output, "tool_error")

    assert output["retry_count"] == 2  # three attempts: the original and two retries
    assert output["executed_tools"] == []
    assert error["reason"] == "tool_unavailable"
    assert (output["finish_reason"], output["finish_detail"]) == ("COMPLETED", "answered")


def test_a_permanent_read_failure_is_never_retried(tmp_path: Path) -> None:
    output = _run(tmp_path, "T07", faults={"consultar_ventas": ["INVALID_REQUEST", "SUCCESS"]})

    assert output["retry_count"] == 0
    assert output["executed_tools"] == []
    assert _event(output, "tool_error")["reason"] == "tool_refused"


def test_no_retries_configured_means_a_single_attempt(tmp_path: Path) -> None:
    output = _run(tmp_path, "T07", preconditions={"max_read_retries": 0})

    assert (output["retry_count"], output["executed_tools"]) == (0, [])


def test_a_retry_that_does_not_fit_in_the_total_time_is_not_made(tmp_path: Path) -> None:
    # The first timeout already costs the whole 3 s of a 3 s run: there is nothing left to retry.
    output = _run(
        tmp_path, "T07", preconditions={"total_timeout_ms": 3000, "tool_timeout_ms": 3000}
    )

    assert output["retry_count"] == 0
    assert (output["finish_reason"], output["finish_detail"]) == ("LIMIT_REACHED", "total_timeout")


def test_retries_spend_the_run_time_they_use(tmp_path: Path) -> None:
    output = _run(tmp_path, "T07", faults={"consultar_ventas": ["TIMEOUT", "TIMEOUT", "SUCCESS"]})

    # Two timeouts of 3 s plus the waits between attempts, on the simulated clock.
    assert 6000 < _event(output, "run_finished")["elapsed_ms"] < 7500


# --------------------------------------------------------------------------- writes


def _t09(tmp_path: Path, *faults: str, calls: int = 2) -> dict[str, Any]:
    script = json.loads((EXAMPLES / "T09.json").read_text())["model_script"]
    script = script[:1] * calls + script[-1:]
    return _run(tmp_path, "T09", model_script=script, faults={"crear_reclamo": list(faults)})


def test_a_write_committed_before_the_timeout_is_found_not_repeated(tmp_path: Path) -> None:
    output = _t09(tmp_path, "COMMITTED_BUT_TIMEOUT")

    assert output["claims_created"] == output["unique_claims_created"] == 1
    assert "write_reconciled" in _types(output)
    assert "tool_retry" not in _types(output)  # writes never go through the read retries


def test_a_plain_write_timeout_is_looked_up_and_attempted_once_more(tmp_path: Path) -> None:
    output = _t09(tmp_path, "TIMEOUT", calls=1)

    assert output["claims_created"] == 1
    assert "write_reconciled" in _types(output)


def test_a_write_that_cannot_be_reconciled_fails_the_run_and_files_nothing_twice(
    tmp_path: Path,
) -> None:
    output = _t09(tmp_path, "TIMEOUT", "TIMEOUT", calls=1)

    assert (output["finish_reason"], output["finish_detail"]) == ("FAILED", "write_outcome_unknown")
    assert output["claims_created"] == 0
    assert "write_uncertain" in _types(output)


def test_a_refused_write_is_not_retried_and_files_nothing(tmp_path: Path) -> None:
    output = _t09(tmp_path, "INVALID_REQUEST", calls=1)

    assert output["claims_created"] == 0
    assert _event(output, "tool_error")["reason"] == "tool_refused"
    assert "write_reconciled" not in _types(output)


# --------------------------------------------------------------------------- the kit's contract


@pytest.mark.parametrize("scenario", ["T07", "T09"])
def test_a_failure_scenario_always_leaves_a_tool_event_with_a_reason_and_no_secret(
    tmp_path: Path, scenario: str
) -> None:
    faults = {
        "T07": {"consultar_ventas": ["INVALID_REQUEST"]},
        "T09": {"crear_reclamo": ["INVALID_REQUEST"]},
    }
    output = _run(tmp_path, scenario, faults=faults[scenario])
    failures = [e for e in output["events"] if e["type"] in {"tool_rejected", "tool_error"}]

    assert failures
    assert all(event.get("reason") for event in failures)
    assert "traceback" not in json.dumps(output).lower()


def test_an_unknown_fault_or_tool_fails_loudly(tmp_path: Path) -> None:
    from cortex.kit_adapter.scenario import UnsupportedScenario

    with pytest.raises(UnsupportedScenario, match="unknown fault"):
        _run(tmp_path, "T07", faults={"consultar_ventas": ["MELTDOWN"]})
    with pytest.raises(UnsupportedScenario, match="unknown tool"):
        _run(tmp_path, "T07", faults={"borrar_todo": ["TIMEOUT"]})
