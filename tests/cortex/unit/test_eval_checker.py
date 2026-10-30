"""The checker must be able to fail: every rule is shown a wrong output and has to catch it."""

import ast
import copy
from pathlib import Path
from typing import Any

import pytest
from evaluation.harness.checker import (
    EXPECTED_RULES,
    Check,
    check_refusal,
    check_scenario,
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
    import json

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
