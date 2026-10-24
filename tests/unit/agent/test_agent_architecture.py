"""Only the executor can register a complaint: checked on the source, not by convention."""

import ast
from pathlib import Path

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
