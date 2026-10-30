"""The evaluation suite end to end, and the checker against the real files of a real run."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from evaluation.harness import suite
from evaluation.harness.checker import EXPECTED_RULES, FILE_RULES, check_scenario
from evaluation.harness.variants import build_variants

from cortex.kit_adapter.orchestrator import run_scenario_file

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit"


def _scenario(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (ROOT / "examples" / f"{name}.json").read_text(encoding="utf-8")
    )
    return data


def _failed(scenario: dict[str, Any], output: dict[str, Any], world: Path) -> set[str]:
    return {c.name for c in check_scenario(scenario, output, world) if not c.ok}


def _real_run(tmp_path: Path, name: str) -> tuple[dict[str, Any], dict[str, Any], Path]:
    scenario = _scenario(name)
    world = tmp_path / "world"
    output = run_scenario_file(ROOT / "examples" / f"{name}.json", kit_dir=KIT, workdir=world)
    return scenario, output, world


# --------------------------------------------------------------------------- the cases


def test_there_are_13_kit_scenarios_and_unique_variants() -> None:
    cases = suite.all_cases()
    names = [case.name for case in cases]

    assert [c.name for c in cases if c.kind == "kit"] == [f"T{n:02d}" for n in range(1, 14)]
    assert len(names) == len(set(names))
    assert len([c for c in cases if c.kind == "variant"]) >= 30


def test_every_variant_asserts_only_what_the_checker_knows_how_to_check() -> None:
    kit = {c.name: c.scenario for c in suite.load_kit_cases()}

    for case in build_variants(kit):
        assert set(case.scenario["expected"]) <= set(EXPECTED_RULES) | set(FILE_RULES), case.name


def test_a_variant_never_changes_the_kit_it_derives_from() -> None:
    kit = {c.name: c.scenario for c in suite.load_kit_cases()}
    before = json.dumps(kit, sort_keys=True)

    build_variants(kit)

    assert json.dumps(kit, sort_keys=True) == before


# --------------------------------------------------------------------------- the suite runs


def test_a_subset_runs_through_the_real_entry_point_and_passes(tmp_path: Path) -> None:
    wanted = {"T01", "T09", "T11", "V17_principal_of_another_company", "V32_unknown_precondition"}
    cases = [case for case in suite.all_cases() if case.name in wanted]

    outcomes = suite.run_cases(cases, jobs=2)

    assert {o.name for o in outcomes} == wanted
    assert all(o.passed for o in outcomes), [(o.name, o.failures) for o in outcomes if not o.passed]


def test_the_results_are_written_dated_with_their_provenance(tmp_path: Path) -> None:
    cases = [c for c in suite.all_cases() if c.name in {"T01", "T02"}]
    outcomes = suite.run_cases(cases, jobs=2)
    info = suite.provenance("2026-10-29")

    suite.write_results(outcomes, info, tmp_path)
    document = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert (document["passed"], document["total"], document["date"]) == (2, 2, "2026-10-29")
    assert {"commit", "code_sha256", "python", "uncommitted_changes"} <= set(document)
    assert "2/2 PASS" in report
    assert "| T01 | kit | PASS |" in report


def test_the_code_digest_changes_when_the_code_changes(tmp_path: Path) -> None:
    assert suite.code_digest() == suite.code_digest()
    assert len(suite.code_digest()) == 64


# -------------------------------------------------------- a claim in the text decides nothing


def test_an_administrator_claim_in_the_message_changes_neither_permissions_nor_scope(
    tmp_path: Path,
) -> None:
    case = next(c for c in suite.all_cases() if c.name == "V54_admin_claim_only_in_the_message")
    assert "comercio_b" in case.scenario["message"]
    assert "tenant_id" not in json.dumps(case.scenario["model_script"])  # the claim is only text
    scenario_file, world = tmp_path / "scenario.json", tmp_path / "world"
    scenario_file.write_text(json.dumps(case.scenario), encoding="utf-8")

    output = run_scenario_file(scenario_file, kit_dir=KIT, workdir=world)

    assert _failed(case.scenario, output, world) == set()
    assert (output["finish_reason"], output["executed_tools"]) == (
        "COMPLETED",
        ["consultar_ventas"],
    )
    events: list[dict[str, Any]] = output["events"]  # type: ignore[assignment]
    executed = next(e for e in events if e["type"] == "tool_executed")
    assert executed["data_tenant"] == "comercio_a" == output["tenant_id"]
    with sqlite3.connect(world / "cortex.db") as db:
        tool_messages = db.execute("SELECT tenant_id, content FROM messages WHERE role = 'tool'")
        [(owner, content)] = tool_messages.fetchall()
    data = json.loads(content)["data"]
    # comercio_a's own day (15 sales, 300000), never comercio_b's (8 sales, 160000).
    assert owner == "comercio_a"
    assert (data["cantidad"], data["bruto"], data["neto"]) == (15, 300000, 300000)


# ---------------------------------------------------------------- the checker vs the files


def test_a_real_run_passes_every_check_against_its_files(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T09")

    assert _failed(scenario, output, world) == set()


def test_an_adapter_that_reports_a_message_that_was_not_sent_is_caught(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T01")
    output["ai_replies_delivered"] = 0
    output["customer_messages_sent"] = 1

    failed = _failed(scenario, output, world)

    assert {"files.ai_replies_delivered", "files.customer_messages_sent"} <= failed


def _send_from_the_follow_up(world: Path, kind: str, reply_to: int | None) -> None:
    """A row the adapter did not report: run-conv_t12 sent the customer a message."""
    with sqlite3.connect(world / "cortex.db") as db:
        db.execute(
            "INSERT INTO outbox (tenant_id, conversation_id, run_id, content, status, kind, "
            "reply_to, control_version, created_at) "
            "VALUES ('comercio_a', 'conv_t12', 'run-conv_t12', 'OK', 'SENT', ?, ?, 0, 'now')",
            (kind, reply_to),
        )


@pytest.mark.parametrize(
    ("kind", "reply_to"),
    # A "reply" to a message that is not an inbound message of the same run is not a reply.
    [("proactive", None), ("reply", 999)],
    ids=["proactive", "reply_to_nothing_of_the_run"],
)
def test_a_follow_up_message_the_output_hides_is_caught(
    tmp_path: Path, kind: str, reply_to: int | None
) -> None:
    scenario, output, world = _real_run(tmp_path, "T12")
    assert _failed(scenario, output, world) == set()
    _send_from_the_follow_up(world, kind, reply_to)

    failed = _failed(scenario, output, world)

    assert "files.customer_messages_sent" in failed


def test_an_extra_claim_in_the_files_is_caught_even_if_the_output_says_one(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T09")
    with sqlite3.connect(world / "claims" / "comercio_a.db") as db:
        db.execute(
            "INSERT INTO complaints "
            "(idempotency_key, conversation_id, reason, description, created_at)"
            " VALUES ('another-key', 'conv_t09', 'motivo', 'una descripcion larga', 'now')"
        )

    failed = _failed(scenario, output, world)

    assert {"files.claims_created", "files.unique_claims_created"} <= failed


def test_a_duplicate_claim_row_is_caught(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T09")
    with sqlite3.connect(world / "claims" / "comercio_a.db") as db:
        db.execute("ALTER TABLE complaints RENAME TO complaints_old")
        db.execute(
            "CREATE TABLE complaints (seq INTEGER, idempotency_key TEXT, conversation_id TEXT,"
            " reason TEXT, description TEXT, created_at TEXT)"
        )
        db.execute("INSERT INTO complaints SELECT * FROM complaints_old")
        db.execute("INSERT INTO complaints SELECT * FROM complaints_old")

    assert "files.one_row_per_claim" in _failed(scenario, output, world)


def test_a_delivery_after_the_takeover_that_the_output_hides_is_caught(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T11")
    with sqlite3.connect(world / "cortex.db") as db:
        db.execute(
            "INSERT INTO journal (tenant_id, conversation_id, kind, reference, created_at)"
            " VALUES ('comercio_a', 'conv_t11', 'delivery', 'run-x', 'now')"
        )

    failed = _failed(scenario, output, world)

    assert "files.delivered_ai_messages_after_takeover" in failed


def test_a_conversation_whose_state_differs_from_the_report_is_caught(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T11")
    output["state"]["conversation_mode"] = "AUTO"  # the files say HUMAN

    assert "files.conversation_mode" in _failed(scenario, output, world)


def test_a_trace_that_differs_from_the_stored_one_is_caught(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T02")
    output["events"] = output["events"][:-1]

    assert "files.trace_matches" in _failed(scenario, output, world)


# ------------------------------------------- the observables the checker recomputes from the files


@pytest.mark.parametrize(
    ("name", "key", "lie"),
    [
        ("T02", "executed_tools", ["crear_reclamo"]),
        ("T02", "executed_tools", []),
        ("T03", "rejected_tools", []),
        ("T07", "retry_count", 0),
        ("T02", "step_count", 1),
        ("T08", "max_steps", 99),
        ("T12", "decision", "CONTACTAR"),
        ("T12", "decision_reasons", ["NOTHING_OWED"]),
        ("T02", "decision", "CONTACTAR"),
        ("T10", "pending_action_retained", False),
        ("T10", "state_after_restart", "AUTO"),
        ("T02", "state_after_restart", "AUTO"),
        ("T06", "contexts_separated", False),
        ("T02", "contexts_separated", True),
    ],
    ids=lambda value: str(value),
)
def test_an_adapter_that_reports_an_observable_the_files_contradict_is_caught(
    tmp_path: Path, name: str, key: str, lie: object
) -> None:
    scenario, output, world = _real_run(tmp_path, name)
    assert _failed(scenario, output, world) == set()  # the honest output passes
    assert output[key] != lie
    output[key] = lie

    assert f"files.{key}" in _failed(scenario, output, world)


def _edit(world: Path, sql: str, *params: object) -> None:
    with sqlite3.connect(world / "cortex.db") as db:
        db.execute(sql, params)


def test_a_pending_action_the_files_no_longer_hold_is_caught(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T10")
    _edit(world, "UPDATE pending_actions SET status = 'CANCELLED'")

    assert "files.pending_action_retained" in _failed(scenario, output, world)


def test_a_decision_that_the_files_do_not_hold_is_caught(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T12")
    _edit(world, "UPDATE followup_decisions SET decision = 'CANCELAR'")

    assert "files.decision" in _failed(scenario, output, world)


def test_a_decision_row_in_a_run_that_reports_none_is_caught(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T12")
    output["decision"], output["decision_reasons"] = None, []

    assert {"files.decision", "files.decision_reasons"} <= _failed(scenario, output, world)


def test_an_interleaved_message_stored_under_another_conversation_is_caught(
    tmp_path: Path,
) -> None:
    scenario, output, world = _real_run(tmp_path, "T06")
    assert output["contexts_separated"] is True
    _edit(
        world,
        "INSERT INTO conversations (tenant_id, conversation_id, created_at) "
        "VALUES ('comercio_b', 'conv_b_inter', 'now') ON CONFLICT DO NOTHING",
    )
    _edit(
        world,
        "INSERT INTO messages (tenant_id, conversation_id, run_id, role, content, created_at) "
        "VALUES ('comercio_b', 'conv_b_inter', 'run-x', 'user', 'Soy Alex del comercio A', 'now')",
    )

    assert "files.contexts_separated" in _failed(scenario, output, world)


def test_a_contact_of_one_company_stored_under_another_is_caught(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T06")
    _edit(
        world,
        "INSERT INTO contacts (tenant_id, contact_id, name) VALUES ('comercio_a', 'b_01', 'Alex')",
    )

    assert "files.contexts_separated" in _failed(scenario, output, world)


FAILURE_RULE = "contract.a_failure_says_why_with_a_tool_event"


def test_a_scenario_with_faults_whose_output_hides_the_tool_error_is_caught(
    tmp_path: Path,
) -> None:
    scenario, output, world = _real_run(tmp_path, "T07")
    assert FAILURE_RULE not in _failed(scenario, output, world)
    output["events"] = [e for e in output["events"] if e["type"] != "tool_error"]

    assert FAILURE_RULE in _failed(scenario, output, world)


@pytest.mark.parametrize("finish_reason", ["BLOCKED", "FAILED"])
def test_a_blocked_or_failed_output_without_a_tool_event_is_caught(
    tmp_path: Path, finish_reason: str
) -> None:
    scenario, output, world = _real_run(tmp_path, "T01")
    output["finish_reason"] = finish_reason

    assert FAILURE_RULE in _failed(scenario, output, world)


def test_a_tool_event_without_a_reason_does_not_satisfy_the_rule(tmp_path: Path) -> None:
    scenario, output, world = _real_run(tmp_path, "T01")
    output["finish_reason"] = "FAILED"
    output["events"].insert(-1, {"seq": 99, "type": "tool_error", "timestamp": "now", "reason": ""})

    assert FAILURE_RULE in _failed(scenario, output, world)
