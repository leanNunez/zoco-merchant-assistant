from pathlib import Path

import pytest

from merchant_assistant.control.diagram import main, render_document, render_mermaid

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMITTED_DIAGRAM = REPO_ROOT / "design" / "state-diagram.md"

# (source, event, target, actors as drawn, guarded by the open hours), typed by hand.
EDGES_OF_RECORD = [
    ("AUTO", "operator_takeover", "HUMAN", "OPERATOR/SUPERVISOR", True),
    ("AUTO", "customer_requests_human", "HUMAN", "CUSTOMER/SYSTEM", True),
    ("AUTO", "low_confidence", "SUGGEST", "SYSTEM", True),
    ("AUTO", "abuse_detected", "BLOCKED", "SYSTEM", False),
    ("AUTO", "out_of_hours_started", "AUTO", "SYSTEM", False),
    ("SUGGEST", "operator_takeover", "HUMAN", "OPERATOR/SUPERVISOR", True),
    ("SUGGEST", "customer_requests_human", "HUMAN", "CUSTOMER/SYSTEM", True),
    ("SUGGEST", "operator_release", "AUTO", "OPERATOR/SUPERVISOR", False),
    ("SUGGEST", "abuse_detected", "BLOCKED", "SYSTEM", False),
    ("SUGGEST", "out_of_hours_started", "AUTO", "SYSTEM", False),
    ("HUMAN", "operator_release", "AUTO", "OPERATOR/SUPERVISOR", False),
    ("HUMAN", "abuse_detected", "HUMAN", "SYSTEM", False),
    ("HUMAN", "operator_block", "BLOCKED", "OPERATOR/SUPERVISOR", False),
    ("HUMAN", "out_of_hours_started", "AUTO", "SYSTEM", False),
    ("BLOCKED", "operator_release", "AUTO", "SUPERVISOR", False),
    ("BLOCKED", "supervisor_unblock", "AUTO", "SUPERVISOR", False),
    ("BLOCKED", "out_of_hours_started", "BLOCKED", "SYSTEM", False),
]


def test_the_committed_diagram_matches_the_code() -> None:
    committed = COMMITTED_DIAGRAM.read_text(encoding="utf-8")

    assert committed == render_document(), "stale diagram: run `make diagram` and commit it"


@pytest.mark.parametrize(
    ("source", "event", "target", "actors", "guarded"),
    [pytest.param(*edge, id=f"{edge[0]}+{edge[1]}") for edge in EDGES_OF_RECORD],
)
def test_every_rule_is_drawn_as_an_edge(
    source: str, event: str, target: str, actors: str, guarded: bool
) -> None:
    expected = f"    {source} --> {target}: {event} ({actors})"
    if guarded:
        expected += " - open hours only"

    assert expected in render_mermaid().splitlines()


def test_the_diagram_has_one_edge_per_rule_plus_the_initial_state() -> None:
    arrows = [line for line in render_mermaid().splitlines() if "-->" in line]

    assert len(arrows) == 17 + 1
    assert arrows[0] == "    [*] --> AUTO"


def test_only_the_five_guarded_rules_carry_the_open_hours_marker() -> None:
    marked = [line for line in render_mermaid().splitlines() if "open hours only" in line]

    assert len(marked) == 5


def test_the_diagram_is_a_state_diagram_v2() -> None:
    assert render_mermaid().startswith("stateDiagram-v2\n")


def test_rendering_is_deterministic() -> None:
    assert render_document() == render_document()


def test_the_document_embeds_the_diagram_in_a_mermaid_block() -> None:
    document = render_document()

    assert f"```mermaid\n{render_mermaid()}```" in document
    assert document.endswith("\n")


def test_the_command_line_entry_point_writes_the_document_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main()

    assert capsys.readouterr().out == render_document()
