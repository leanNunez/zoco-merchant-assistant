"""The saved files: one section per case in the required order, sorted JSON, no prompt text."""

import json

import pytest
from evaluation.conversations.catalog import SCENARIOS
from evaluation.conversations.metrics import summarize
from evaluation.conversations.report import (
    errors_jsonl,
    provenance,
    reference_prompt,
    to_json,
    to_markdown,
)
from evaluation.conversations.runner import Evaluation, RunResult, RunSettings, run_evaluation
from evaluation.conversations.validation import validate

pytestmark = pytest.mark.integration

CASES = [s for s in SCENARIOS if s.id in {"S04", "S18", "X1"}]


@pytest.fixture(scope="module")
def scripted() -> Evaluation:
    return run_evaluation(CASES, RunSettings("scripted", reps=2))


def render(evaluation: Evaluation, *, with_validation: bool = False) -> tuple[str, str]:
    summary, source = summarize(evaluation), provenance(evaluation)
    validation = None
    if with_validation:

        def again(mode: str) -> Evaluation:
            return run_evaluation(CASES, RunSettings(mode, reps=2))  # type: ignore[arg-type]

        validation = validate(evaluation, again("null"), again("naive"))
    return (
        to_markdown(evaluation, summary, source, validation),
        to_json(evaluation, summary, source, validation),
    )


def test_each_case_section_follows_the_required_order(scripted: Evaluation) -> None:
    markdown, _ = render(scripted)
    section = markdown[markdown.index("### S04") : markdown.index("### S18")]
    markers = [
        "**Reclamo completo",
        "**Mensajes del usuario y pasos**",
        "**Comportamiento esperado.**",
        "**Herramientas que debe usar:**",
        "**Resultado observado.**",
        "**Cumplimiento: 2/2 (100%)**",
    ]
    positions = [section.index(marker) for marker in markers]
    assert positions == sorted(positions)
    assert "Idéntico en las 2 repeticiones válidas" in section
    assert "_(solo si el último mensaje pide confirmar)_" in section
    assert markdown.index("## Qué NO mide esta evaluación") > markdown.index("### X1")
    assert "no se usa un LLM como juez" in markdown


def test_the_validation_section_is_written_only_when_given(scripted: Evaluation) -> None:
    plain, _ = render(scripted)
    validated, document = render(scripted, with_validation=True)
    assert "## Validación del evaluador" not in plain
    assert "## Validación del evaluador" in validated
    assert "Las tres validaciones se cumplen en todos los casos" in validated
    assert json.loads(document)["validation"]["holds"] is True


def test_the_json_is_sorted_complete_and_never_copies_the_prompt(scripted: Evaluation) -> None:
    _, document = render(scripted)
    parsed = json.loads(document)

    assert document == json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert [(r["scenario"], r["rep"]) for r in parsed["runs"]] == [
        ("S04", 0), ("S04", 1), ("S18", 0), ("S18", 1), ("X1", 0), ("X1", 1),
    ]  # fmt: skip
    trajectory = parsed["runs"][0]["trajectory"]
    assert {"steps", "tools", "rows", "history", "control_log", "checks"} <= {
        *trajectory,
        *parsed["runs"][0],
    }
    assert len(trajectory["system_prompt_sha256"]) == 1
    first_rule = reference_prompt().splitlines()[0]
    assert first_rule not in document
    # 6/6: centre (1 + z²/12) / (1 + z²/6) = 0.80483, margin 0.19517 -> [0.609666, 1.0]
    assert parsed["summary"]["overall"]["compliance"]["wilson95"] == [0.609666, 1.0]


def test_invalid_and_unrun_conversations_go_to_errors_and_mark_the_report_partial() -> None:
    evaluation = Evaluation(
        settings=RunSettings("scripted", reps=2),
        scenarios=tuple(CASES[:1]),
        results=(RunResult("S04", 0, 0, valid=False, failure_class="timeout", failure_detail="x"),),
        not_run=(("S04", 1),),
        stop_reason="budget_exhausted",
    )

    lines = [json.loads(line) for line in errors_jsonl(evaluation).splitlines()]
    markdown, _ = render(evaluation)

    assert [(line["failure_class"], line["rep"]) for line in lines] == [
        ("timeout", 0),
        ("not_run", 1),
    ]
    assert "**Resultados PARCIALES**" in markdown
    assert "Corridas inválidas (no puntúan): 1 (timeout)." in markdown
