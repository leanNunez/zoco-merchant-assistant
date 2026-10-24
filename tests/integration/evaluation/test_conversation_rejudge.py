"""Saved transcripts can be judged again offline, and rebuilding one loses nothing a check reads."""

import json
from pathlib import Path
from typing import Any

import pytest
from evaluation.conversations.catalog import SCENARIOS
from evaluation.conversations.metrics import summarize
from evaluation.conversations.rejudge import main, rebuild, rejudge
from evaluation.conversations.report import provenance, to_json
from evaluation.conversations.runner import RunSettings, run_evaluation

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def saved() -> dict[str, Any]:
    """What `results.json` of a scripted run holds, read back from its text."""
    evaluation = run_evaluation(SCENARIOS, RunSettings("scripted", reps=1))
    document: dict[str, Any] = json.loads(
        to_json(evaluation, summarize(evaluation), provenance(evaluation), None)
    )
    return document


def test_a_rebuilt_observation_gives_every_check_the_verdict_it_had(saved: dict[str, Any]) -> None:
    by_id = {scenario.id: scenario for scenario in SCENARIOS}

    for run in saved["runs"]:
        observation = rebuild(run["trajectory"])
        checks = [c for c in by_id[run["scenario"]].checks if c.applies(observation)]
        again = {c.name: c.passed for c in (check.run(observation) for check in checks)}
        before = {c["name"]: c["passed"] for c in run["checks"]}
        # The prompt itself is not saved (only its hash), so the leak lock reads "no prompt".
        again.pop("no_system_prompt_leak", None)
        before.pop("no_system_prompt_leak", None)
        assert again == before, run["scenario"]


def test_rejudging_the_current_scripted_results_changes_nothing(saved: dict[str, Any]) -> None:
    result = rejudge(saved)

    assert result.complying_before == result.complying_after == result.runs == len(SCENARIOS)
    assert result.changes == ()


def test_the_command_prints_before_and_after(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], saved: dict[str, Any]
) -> None:
    path = tmp_path / "results.json"
    path.write_text(json.dumps(saved), encoding="utf-8")

    assert main([str(path)]) == 0
    assert "Corridas que cumplen: 34/34 con el evaluador de entonces, 34/34 con el actual." in (
        capsys.readouterr().out
    )
    assert main([]) == 2
