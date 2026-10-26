"""Only the executor can register a complaint, and only the gate can confirm one: checked on
the source, not by convention."""

import ast
from pathlib import Path

import merchant_assistant
import merchant_assistant.agent as agent_package

AGENT = Path(agent_package.__file__).parent
EXECUTOR = AGENT / "application" / "executor.py"
PORTS = AGENT / "ports.py"  # where the port is declared


def registry_uses(path: Path) -> list[str]:
    """Every import or mention of `ComplaintRegistry` and every `.register(...)` call."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            found += [
                f"import {alias.name}" for alias in node.names if alias.name == "ComplaintRegistry"
            ]
        elif isinstance(node, ast.Name) and node.id == "ComplaintRegistry":
            found.append("name ComplaintRegistry")
        elif isinstance(node, ast.Attribute) and node.attr == "ComplaintRegistry":
            found.append("attribute ComplaintRegistry")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "register"
        ):
            found.append(f"call .register at line {node.lineno}")
    return found


def test_no_module_of_the_agent_but_the_executor_can_reach_the_complaint_registry() -> None:
    offenders = {
        str(path.relative_to(AGENT)): uses
        for path in sorted(AGENT.rglob("*.py"))
        if path not in (EXECUTOR, PORTS) and (uses := registry_uses(path))
    }

    assert offenders == {}


def test_the_scan_does_see_the_executor_registering() -> None:
    # Without this, a scanner that finds nothing anywhere would pass the test above.
    uses = registry_uses(EXECUTOR)

    assert "import ComplaintRegistry" in uses
    assert any(use.startswith("call .register") for use in uses)


def test_the_executor_registers_in_exactly_one_place() -> None:
    calls = [use for use in registry_uses(EXECUTOR) if use.startswith("call .register")]

    assert len(calls) == 1


# --------------------------------------------------------------------------- the gate's proof

PACKAGE = Path(merchant_assistant.__file__).parent
PENDING = AGENT / "domain" / "pending.py"  # the gate: the only module that issues a Confirmed
FORGE = """
from merchant_assistant.agent.domain.pending import _GATE_PROOF, Confirmed as Yes
from merchant_assistant.agent.domain import pending

def forge(action):
    Yes(action, _proof=_GATE_PROOF)
    pending.Confirmed(action, _proof=getattr(pending, "_GATE_PROOF"))
"""


def proof_uses(source: str) -> list[str]:
    """Every mention of `_GATE_PROOF` (also as a string) and every call building a `Confirmed`.

    Matching `case Confirmed():` or annotating with `Confirmed` is reading one, not building it.
    """
    tree = ast.parse(source)
    builders = {"Confirmed"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "Confirmed" and alias.asname
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found += [f"import {a.name}" for a in node.names if a.name == "_GATE_PROOF"]
        elif isinstance(node, ast.Name) and node.id == "_GATE_PROOF":
            found.append(f"name _GATE_PROOF at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr == "_GATE_PROOF":
            found.append(f"attribute _GATE_PROOF at line {node.lineno}")
        elif isinstance(node, ast.Constant) and node.value == "_GATE_PROOF":
            found.append(f"string _GATE_PROOF at line {node.lineno}")
        elif isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Name) and node.func.id in builders)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "Confirmed")
        ):
            found.append(f"call Confirmed at line {node.lineno}")
    return found


def test_no_module_but_the_gate_can_build_a_confirmed() -> None:
    offenders = {
        str(path.relative_to(PACKAGE)): uses
        for path in sorted(PACKAGE.rglob("*.py"))
        if path != PENDING and (uses := proof_uses(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


def test_the_scan_does_see_a_forged_confirmed() -> None:
    # Without this, a scanner that finds nothing anywhere would pass the test above.
    uses = proof_uses(FORGE)

    assert "import _GATE_PROOF" in uses
    assert sum(use.startswith("call Confirmed") for use in uses) == 2  # the alias and `pending.`
    assert any(use.startswith("string _GATE_PROOF") for use in uses)


def test_the_scan_does_see_the_gate_issuing_its_confirmed() -> None:
    uses = proof_uses(PENDING.read_text(encoding="utf-8"))

    assert any(use.startswith("call Confirmed") for use in uses)
    assert any(use.startswith("name _GATE_PROOF") for use in uses)
