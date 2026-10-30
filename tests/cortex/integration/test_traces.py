"""Traces: complete, stored, free of secrets, and reproducible (brief section 7)."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from cortex.kit_adapter import samples
from cortex.kit_adapter.orchestrator import DEFAULT_NOW, run_scenario
from cortex.kit_adapter.scenario import load_scenario
from cortex.memory.tenant_store import TenantStore

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
EXAMPLES, KIT = ROOT / "examples", ROOT / "kit"
SAMPLES = ROOT / "evaluation" / "results" / "harness" / "traces"

CARD, EMAIL = "4111 1111 1111 1111", "juana.perez@example.com"
API_KEY, PHONE = "sk-live-AbCdEf1234567890XyZq", "+54 9 11 5555-1234"
CUIT, DNI = "20-12345678-9", "30123456"
CANARIES = (
    CARD,
    "4111",
    EMAIL,
    "juana.perez",
    API_KEY,
    "AbCdEf1234567890",
    PHONE,
    "5555-1234",
    CUIT,
    "12345678",
    DNI,
)
SECRET_TEXT = (
    f"Mi tarjeta {CARD}, mi mail {EMAIL}, la clave {API_KEY}, tel {PHONE}, CUIT {CUIT}, DNI {DNI}"
)


def _example(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def _run(scenario: dict[str, Any], workdir: Path) -> dict[str, Any]:
    output: dict[str, Any] = run_scenario(
        load_scenario(scenario), kit_dir=KIT, workdir=workdir, now=DEFAULT_NOW
    )
    return output


def _canary_scenario() -> dict[str, Any]:
    scenario = _example("T09")
    scenario["preconditions"] = {}
    scenario["message"] = SECRET_TEXT
    scenario["model_script"] = [
        {"tool": f"tarjeta{CARD.replace(' ', '')}", "args": {}},  # a tool name carrying a number
        {
            "tool": "crear_reclamo",
            "args": {
                "motivo": "demora",
                "descripcion": SECRET_TEXT,
                "operation_id": f"op_{API_KEY}",
            },
        },
        {"final": SECRET_TEXT},
    ]
    return scenario


def _everything_stored_as_trace(workdir: Path) -> str:
    with sqlite3.connect(workdir / "cortex.db") as db:
        traces = db.execute("SELECT * FROM trace_events").fetchall()
        results = db.execute("SELECT result FROM runs").fetchall()
    return json.dumps([traces, results], ensure_ascii=False)


# --------------------------------------------------------------------------- what a trace holds


def test_a_trace_has_the_ids_the_times_the_tools_and_the_final_reason(tmp_path: Path) -> None:
    events = _run(_example("T07"), tmp_path)["events"]
    base = {
        "seq",
        "type",
        "run_id",
        "tenant_id",
        "conversation_id",
        "step",
        "timestamp",
        "elapsed_ms",
    }

    assert all(base <= set(event) for event in events)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert {event["tenant_id"] for event in events} == {"comercio_a"}
    kinds = [event["type"] for event in events]
    assert kinds[0] == "run_started"
    assert {"tool_requested", "tool_retry", "tool_executed"} <= set(kinds)
    finished = events[-1]
    assert finished["type"] == "run_finished"
    assert (finished["finish_reason"], finished["steps"], finished["retries"]) == (
        "COMPLETED",
        2,
        1,
    )
    assert finished["tools_executed"] == 1


def test_the_trace_is_stored_as_it_happens_and_matches_what_the_run_returned(
    tmp_path: Path,
) -> None:
    output = _run(_example("T07"), tmp_path)
    stored = json.loads(_everything_stored_as_trace(tmp_path))[0]

    assert [row[2] for row in stored] == [event["seq"] for event in output["events"]]
    assert [row[4] for row in stored] == [event["type"] for event in output["events"]]


def test_a_failed_run_leaves_its_whole_story_behind(tmp_path: Path) -> None:
    scenario = _example("T09")
    scenario["model_script"] = [scenario["model_script"][0], scenario["model_script"][-1]]
    scenario["faults"] = {"crear_reclamo": ["TIMEOUT", "TIMEOUT"]}

    events = _run(scenario, tmp_path)["events"]

    assert [e["type"] for e in events[3:]] == [
        "tool_requested",
        "tool_error",
        "write_lookup",
        "tool_error",
        "write_uncertain",
        "run_finished",
    ]
    assert events[-1]["finish_reason"] == "FAILED"
    assert events[-1]["duration_ms"] == 6000


def test_a_trace_that_cannot_be_written_never_changes_what_the_run_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(self: TenantStore, event: object) -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr("cortex.memory.traces.TraceStore.write", broken)
    with caplog.at_level(logging.WARNING):
        output = _run(_example("T07"), tmp_path)

    assert (output["finish_reason"], output["executed_tools"]) == (
        "COMPLETED",
        ["consultar_ventas"],
    )
    assert "tracing continues in memory" in caplog.text


# --------------------------------------------------------------------------- no secrets


def test_no_secret_reaches_a_trace_a_result_a_log_or_the_operators_card(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        output = _run(_canary_scenario(), tmp_path)

    everything = (
        json.dumps(output, ensure_ascii=False) + _everything_stored_as_trace(tmp_path) + caplog.text
    )
    leaked = [canary for canary in CANARIES if canary in everything]
    assert not leaked, f"leaked: {leaked}"
    assert (
        output["finish_detail"] == "awaiting_confirmation"
    )  # the run really went through the write


def test_what_is_masked_is_still_enough_to_follow_what_happened(tmp_path: Path) -> None:
    events = _run(_canary_scenario(), tmp_path)["events"]
    kinds = [event["type"] for event in events]

    assert "tool_rejected" in kinds
    assert "tool_confirmation_required" in kinds
    required = next(e for e in events if e["type"] == "tool_confirmation_required")
    assert required["operation_id"].startswith("op_")  # the prefix survives, the key does not
    assert API_KEY not in required["operation_id"]


def test_the_operators_card_masks_the_customers_numbers(tmp_path: Path) -> None:
    card = _run(_canary_scenario(), tmp_path)["state"]["continuity_card"]

    assert card["last_query"] is not None
    assert "[redacted]" in card["last_query"]
    assert not [c for c in CANARIES if c in json.dumps(card, ensure_ascii=False)]


# --------------------------------------------------------------------------- samples


@pytest.mark.parametrize("name", [samples.SUCCESS_FILE, samples.FAILURE_FILE])
def test_the_committed_sample_traces_are_current(name: str) -> None:
    expected = samples.render(samples.sample_scenarios()[name])

    assert (SAMPLES / name).read_text(encoding="utf-8") == expected


def test_the_sample_traces_tell_a_success_and_a_failure() -> None:
    success = [
        json.loads(line) for line in (SAMPLES / samples.SUCCESS_FILE).read_text().splitlines()
    ]
    failure = [
        json.loads(line) for line in (SAMPLES / samples.FAILURE_FILE).read_text().splitlines()
    ]

    assert success[-1]["finish_reason"] == "COMPLETED"
    assert failure[-1]["finish_reason"] == "FAILED"
    for event in (*success, *failure):
        assert "operador_a" not in json.dumps(event)  # the principal appears only as a hash
