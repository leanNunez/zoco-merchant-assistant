"""The checker must be able to fail: every rule is shown a wrong output and has to catch it."""

import ast
import copy
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from evaluation.harness.checker import (
    ALLOWED_FINISH_REASONS,
    EXPECTED_RULES,
    FILE_RULES,
    Check,
    RealState,
    check_refusal,
    check_scenario,
    read_allowed_finish_reasons,
    read_company_contacts,
    read_default_max_steps,
)

CHECKER = Path(__file__).resolve().parents[3] / "evaluation" / "harness" / "checker.py"
STAMP = "2026-10-28T12:00:00Z"

GOOD: dict[str, Any] = {
    "run_id": "run-1",
    "tenant_id": "comercio_a",
    "conversation_id": "conv_1",
    "finish_reason": "COMPLETED",
    "finish_detail": "answered",
    "step_count": 1,
    "events": [{"seq": 1, "type": "final_answer", "timestamp": STAMP}],
    "effects": {"claims_created": 0, "customer_messages_sent": 0, "ai_replies_delivered": 1},
    "state": {"conversation_mode": "AUTO"},
    "executed_tools": [],
    "rejected_tools": [],
    "external_calls_to_rejected_tool": 0,
    "cross_tenant_data_exposed": False,
    "cross_tenant_effects": 0,
    "delivered_ai_messages_after_takeover": None,
    "tool_effects_after_takeover": None,
    "customer_messages_sent": 0,
    "ai_replies_delivered": 1,
}


def _failed(output: dict[str, Any], expected: dict[str, Any] | None = None) -> set[str]:
    checks = check_scenario({"expected": expected or {}}, output, None)
    return {check.name for check in checks if not check.ok}


def _with(**changes: Any) -> dict[str, Any]:
    output = copy.deepcopy(GOOD)
    output.update(changes)
    return output


def test_a_correct_output_passes_everything() -> None:
    assert _failed(GOOD, {"finish_reason": "COMPLETED", "executed_tools": []}) == set()


@pytest.mark.parametrize("field", ["run_id", "tenant_id", "finish_reason", "step_count", "events"])
def test_a_missing_contract_field_fails(field: str) -> None:
    output = copy.deepcopy(GOOD)
    del output[field]

    assert "contract.fields" in _failed(output)


def test_a_finish_reason_outside_the_five_fails() -> None:
    assert "contract.finish_reason" in _failed(_with(finish_reason="DONE"))


def test_an_event_without_a_timestamp_fails() -> None:
    assert "contract.events" in _failed(_with(events=[{"type": "final_answer"}]))


def test_an_expected_value_that_differs_fails() -> None:
    assert "expected.executed_tools" in _failed(GOOD, {"executed_tools": ["consultar_ventas"]})
    assert "expected.finish_reason" in _failed(GOOD, {"finish_reason": "BLOCKED"})


def test_an_expected_key_nobody_checks_fails_loudly() -> None:
    failed = check_scenario({"expected": {"mystery": 1}}, GOOD, None)

    assert [c.name for c in failed if not c.ok] == ["expected.mystery"]
    assert "no rule" in next(c.detail for c in failed if not c.ok)


def test_every_key_the_kit_uses_has_a_rule() -> None:
    kit = json.loads((Path(CHECKER).parents[2] / "kit" / "scenarios.json").read_text())
    used = {key for scenario in kit["scenarios"] for key in scenario["expected"]}

    assert used <= set(EXPECTED_RULES)


@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"external_calls_to_rejected_tool": 1}, "rule.rejection_never_reaches_the_tool"),
        ({"cross_tenant_effects": 1}, "rule.no_company_crossing"),
        ({"cross_tenant_data_exposed": True}, "rule.no_company_crossing"),
        ({"delivered_ai_messages_after_takeover": 1}, "rule.nothing_after_a_takeover"),
        ({"tool_effects_after_takeover": 2}, "rule.nothing_after_a_takeover"),
        ({"finish_detail": "auth_invalid"}, "rule.a_denial_has_a_rejection_event"),
        (
            {"events": [{"type": "tool_error", "timestamp": STAMP}]},
            "rule.failures_say_why",
        ),
        (
            {
                "events": [
                    {"type": "final_answer", "timestamp": STAMP, "note": "sk-live-AbCdEf123456"}
                ]
            },
            "rule.no_secrets_in_the_output",
        ),
        (
            {"events": [{"type": "x", "timestamp": STAMP, "mail": "a.b@example.com"}]},
            "rule.no_secrets_in_the_output",
        ),
    ],
)
def test_each_rule_of_the_contract_catches_its_violation(
    changes: dict[str, Any], rule: str
) -> None:
    assert rule in _failed(_with(**changes))


def test_a_denial_with_its_rejection_event_passes() -> None:
    output = _with(
        finish_detail="scope_violation",
        events=[
            {"seq": 1, "type": "tool_rejected", "timestamp": STAMP, "reason": "scope_violation"}
        ],
    )

    assert _failed(output) == set()


REFUSED_CHAT_EVENT = [{"type": "chat_event_rejected", "reason": "auth_invalid"}]


def test_a_refused_chat_event_without_a_tool_rejected_is_caught() -> None:
    output = _with(chat_events=REFUSED_CHAT_EVENT)

    assert "rule.a_refused_chat_event_has_a_rejection_event" in _failed(output)


def test_a_refused_chat_event_with_its_tool_rejected_passes() -> None:
    rejected = {"seq": 2, "type": "tool_rejected", "timestamp": STAMP, "reason": "auth_invalid"}
    output = _with(chat_events=REFUSED_CHAT_EVENT, events=[*GOOD["events"], rejected])

    assert _failed(output) == set()


def test_a_refusal_needs_the_right_exit_code_a_reason_and_no_output() -> None:
    assert all(c.ok for c in check_refusal(2, 2, "unsupported scenario", output_exists=False))
    names = {c.name for c in check_refusal(0, 2, "", output_exists=True) if not c.ok}

    assert names == {"refusal.exit_code", "refusal.says_why", "refusal.no_output"}


def test_the_checker_does_not_depend_on_the_harness() -> None:
    """It must judge from the files and the output alone, never reuse the code under test."""
    tree = ast.parse(CHECKER.read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[0]
        if isinstance(node, ast.ImportFrom)
        else alias.name.split(".")[0]
        for node in ast.walk(tree)
        for alias in (node.names if isinstance(node, ast.Import | ast.ImportFrom) else [])
        if isinstance(node, ast.Import | ast.ImportFrom)
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not imported & {"cortex", "merchant_assistant"}


def test_a_check_is_a_plain_value() -> None:
    assert Check("a", True) == Check("a", True, "")


def _world_with_messages(folder: Path, stored: int) -> Path:
    with closing(sqlite3.connect(folder / "cortex.db")) as db:
        db.execute("CREATE TABLE messages (tenant_id TEXT, conversation_id TEXT)")
        db.executemany("INSERT INTO messages VALUES (?, ?)", [("comercio_a", "conv_1")] * stored)
        db.execute("INSERT INTO messages VALUES ('comercio_b', 'conv_1')")  # another company's
        db.commit()
    return folder


def test_messages_stored_is_read_from_the_files_of_that_company(tmp_path: Path) -> None:
    rule = FILE_RULES["messages_stored"]
    output = {"tenant_id": "comercio_a", "conversation_id": "conv_1"}

    assert rule(2, output, RealState(_world_with_messages(tmp_path, 2))).ok
    assert not rule(0, output, RealState(tmp_path)).ok


def test_messages_stored_needs_the_kept_world() -> None:
    check = FILE_RULES["messages_stored"](0, GOOD, RealState(None))

    assert not check.ok
    assert "no world" in check.detail or "none" in check.detail


# --------------------------------------------------------------------------- finish reasons


def test_the_allowed_finish_reasons_are_the_ones_in_the_kits_config() -> None:
    config = json.loads((CHECKER.parents[2] / "kit" / "harness_config.json").read_text())

    assert set(config["allowed_finish_reasons"]) == ALLOWED_FINISH_REASONS


def test_the_allowed_finish_reasons_are_read_from_the_file_given(tmp_path: Path) -> None:
    config = tmp_path / "harness_config.json"
    config.write_text(json.dumps({"allowed_finish_reasons": ["DONE", "OOPS"]}), encoding="utf-8")

    assert read_allowed_finish_reasons(config) == frozenset({"DONE", "OOPS"})


@pytest.mark.parametrize("content", ['{"allowed_finish_reasons": "DONE"}', "{}", "[1]", "nope"])
def test_a_config_without_a_list_of_reasons_is_refused_loudly(tmp_path: Path, content: str) -> None:
    config = tmp_path / "harness_config.json"
    config.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="allowed_finish_reasons"):
        read_allowed_finish_reasons(config)


def test_the_default_step_limit_and_the_contacts_are_read_from_the_kit(tmp_path: Path) -> None:
    config, companies = tmp_path / "config.json", tmp_path / "companies.json"
    config.write_text(json.dumps({"defaults": {"max_steps": 4}}), encoding="utf-8")
    companies.write_text(
        json.dumps({"contacts": [{"tenant_id": "a", "contact_id": "a_1", "name": "Alex"}]}),
        encoding="utf-8",
    )

    assert read_default_max_steps(config) == 4
    assert read_company_contacts(companies) == {"a": frozenset({("a_1", "Alex")})}


@pytest.mark.parametrize(
    "content", ['{"defaults": {"max_steps": true}}', '{"defaults": {}}', "nope"]
)
def test_a_kit_without_a_step_limit_is_refused_loudly(tmp_path: Path, content: str) -> None:
    config = tmp_path / "config.json"
    config.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="max_steps"):
        read_default_max_steps(config)


@pytest.mark.parametrize("content", ['{"contacts": [{"tenant_id": "a"}]}', "{}", "nope"])
def test_a_kit_without_contacts_is_refused_loudly(tmp_path: Path, content: str) -> None:
    companies = tmp_path / "companies.json"
    companies.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="contacts"):
        read_company_contacts(companies)


# --------------------------------------------------------------------------- secrets


def _leaks(text: str) -> bool:
    output = _with(events=[{"seq": 1, "type": "final_answer", "timestamp": STAMP, "note": text}])
    return "rule.no_secrets_in_the_output" in _failed(output)


@pytest.mark.parametrize(
    "canary",
    [
        "sk-live-AbCdEf1234567890",
        "pk_test_AbCdEf1234567890",
        "ghp_AbCdEf1234567890xyz",
        "xoxb-AbCdEf1234567890",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc",
        "authorization: bearer abcdefgh12345678",
        "juana.perez@example.com",
        "a+b@sub.example.co",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "4111.1111.1111.1111",
        "4111111111111111",
        "tarjeta4111111111111111",
        "CUIT 20-12345678-9",
        "30123456789",
        "3f2a9c4e8b1d7a6f5e0c9b8a7d6e5f4a3b2c1d0e",
    ],
)
def test_a_canary_of_each_kind_is_caught(canary: str) -> None:
    assert _leaks(canary)


@pytest.mark.parametrize(
    "harmless",
    [
        "1234567",
        "12345678",
        "total 1234567 y neto 12345678",
        "$ 1.234.567",
        "ventas 123456 y 654321",
        "2026-10-15",
        "op_claim_001",
        "consultar_ventas",
        "run-conv_t09",
    ],
)
def test_amounts_and_codes_are_not_taken_for_secrets(harmless: str) -> None:
    assert not _leaks(harmless)


def test_the_long_names_of_the_contracts_own_keys_are_not_taken_for_tokens() -> None:
    # delivered_ai_messages_after_takeover is 36 characters of a token's alphabet.
    assert "delivered_ai_messages_after_takeover" in GOOD
    assert _failed(GOOD) == set()
