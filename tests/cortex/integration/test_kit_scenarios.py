"""The first public scenarios, end to end through the same entry point IT will run."""

import json
from pathlib import Path
from typing import Any

import pytest

from cortex.kit_adapter.orchestrator import run_scenario_file
from cortex.kit_adapter.scenario import UnsupportedScenario

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = ROOT / "examples"
KIT = ROOT / "kit"


def _run(name: str) -> dict[str, Any]:
    return run_scenario_file(EXAMPLES / f"{name}.json", kit_dir=KIT)


def _scenario(tmp_path: Path, **changes: object) -> Path:
    data = json.loads((EXAMPLES / "T02.json").read_text(encoding="utf-8"))
    data.update(changes)
    path = tmp_path / "variant.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _types(output: dict[str, Any]) -> list[str]:
    return [event["type"] for event in output["events"]]


def test_t01_a_plain_answer_completes_in_one_step() -> None:
    output = _run("T01")

    assert (output["finish_reason"], output["step_count"]) == ("COMPLETED", 1)
    # The contract's own example is T01: answering the user is not contacting the customer.
    assert output["effects"]["customer_messages_sent"] == 0
    assert output["effects"]["ai_replies_delivered"] == 1
    assert (output["customer_messages_sent"], output["ai_replies_delivered"]) == (0, 1)


def test_t02_an_allowed_tool_runs_once_and_the_answer_is_delivered() -> None:
    output = _run("T02")

    assert output["executed_tools"] == ["consultar_ventas"]
    assert output["finish_reason"] == "COMPLETED"


def test_t03_an_unknown_tool_is_rejected_without_any_external_call() -> None:
    output = _run("T03")

    assert output["rejected_tools"] == ["eliminar_ventas"]
    assert output["external_calls_to_rejected_tool"] == 0
    assert "tool_executed" not in _types(output)


def test_t04_invalid_arguments_are_rejected_before_the_adapter() -> None:
    output = _run("T04")
    rejection = next(event for event in output["events"] if event["type"] == "tool_rejected")

    assert output["rejected_tools"] == ["consultar_ventas"]
    assert rejection["reason"] == "invalid_arguments"
    assert output["external_calls_to_rejected_tool"] == 0


def test_t05_naming_another_company_ends_the_run_with_a_refusal() -> None:
    output = _run("T05")

    assert (output["finish_reason"], output["finish_detail"]) == ("BLOCKED", "scope_violation")
    assert output["executed_tools"] == []
    assert output["external_calls_to_rejected_tool"] == 0


def test_t08_the_scenario_step_limit_overrides_the_default() -> None:
    output = _run("T08")

    assert (output["finish_reason"], output["finish_detail"]) == ("LIMIT_REACHED", "max_steps")
    assert (output["step_count"], output["max_steps"]) == (3, 3)
    assert output["effects"]["customer_messages_sent"] == 0


def test_the_same_scenario_twice_gives_the_same_output() -> None:
    assert _run("T02") == _run("T02")


def test_the_contract_fields_are_always_present() -> None:
    output = _run("T01")

    for field in ("run_id", "tenant_id", "conversation_id", "finish_reason", "step_count"):
        assert field in output
    assert {"claims_created", "customer_messages_sent", "ai_replies_delivered"} <= set(
        output["effects"]
    )
    assert "conversation_mode" in output["state"]
    assert all({"type", "timestamp"} <= set(event) for event in output["events"])


def test_company_b_sees_its_own_sales(tmp_path: Path) -> None:
    output = run_scenario_file(
        _scenario(tmp_path, tenant_id="comercio_b", principal_id="operador_b"), kit_dir=KIT
    )

    assert output["tenant_id"] == "comercio_b"
    assert output["executed_tools"] == ["consultar_ventas"]


def test_a_principal_of_another_company_is_refused_before_anything_runs(tmp_path: Path) -> None:
    output = run_scenario_file(_scenario(tmp_path, principal_id="operador_b"), kit_dir=KIT)

    assert (output["finish_reason"], output["finish_detail"]) == ("BLOCKED", "auth_invalid")
    assert output["step_count"] == 0


def test_a_malformed_model_turn_fails_the_run_explicitly(tmp_path: Path) -> None:
    output = run_scenario_file(_scenario(tmp_path, model_script=[{"say": "hola"}]), kit_dir=KIT)

    assert (output["finish_reason"], output["finish_detail"]) == ("FAILED", "invalid_model_output")


@pytest.mark.parametrize(
    "changes",
    [{"preconditions": {"unknown_switch": True}}, {"surprise": 1}, {"model_script": "final"}],
)
def test_an_unknown_or_malformed_scenario_fails_loudly(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(UnsupportedScenario):
        run_scenario_file(_scenario(tmp_path, **changes), kit_dir=KIT)


def test_a_scenario_may_name_the_run_id_of_its_main_run(tmp_path: Path) -> None:
    output: dict[str, Any] = run_scenario_file(
        _scenario(tmp_path, run_id="run_ext_40"), kit_dir=KIT
    )

    assert output["run_id"] == "run_ext_40"
    assert {event["run_id"] for event in output["events"]} == {"run_ext_40"}
    assert output["executed_tools"] == ["consultar_ventas"]


def test_without_a_run_id_the_main_run_keeps_the_derived_one(tmp_path: Path) -> None:
    assert run_scenario_file(_scenario(tmp_path), kit_dir=KIT)["run_id"] == "run-conv_t02"


@pytest.mark.parametrize("bad", ["", "run/../x", "r" * 129, 7, None])
def test_an_invalid_run_id_is_an_unsupported_scenario(tmp_path: Path, bad: object) -> None:
    with pytest.raises(UnsupportedScenario, match="run_id"):
        run_scenario_file(_scenario(tmp_path, run_id=bad), kit_dir=KIT)
