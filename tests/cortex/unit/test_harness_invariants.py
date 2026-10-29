"""Rules of the harness checked on its source code, so they cannot erode silently."""

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[3] / "src" / "cortex"
MODULES = sorted(SOURCE.rglob("*.py"))
_MUTABLE_LITERALS = (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)
_MUTABLE_FACTORIES = {"list", "dict", "set", "defaultdict", "deque", "OrderedDict", "Counter"}


def _module_level_values(tree: ast.Module) -> list[tuple[str, ast.expr]]:
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            values += [(name, node.value) for name in names]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                values.append((node.target.id, node.value))
    return values


def _is_mutable(value: ast.expr) -> bool:
    if isinstance(value, _MUTABLE_LITERALS):
        return True
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _MUTABLE_FACTORIES
    )


@pytest.mark.parametrize("module", MODULES, ids=lambda p: str(p.relative_to(SOURCE)))
def test_no_module_holds_mutable_global_state(module: Path) -> None:
    tree = ast.parse(module.read_text(encoding="utf-8"))

    mutable = [name for name, value in _module_level_values(tree) if _is_mutable(value)]

    assert not mutable, f"module-level mutable state shared by every run: {mutable}"


def test_the_kit_adapter_never_reads_the_expected_results() -> None:
    """`expected` may only appear in the set of keys dropped when a scenario is loaded."""
    for module in sorted((SOURCE / "kit_adapter").glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        dropped = {
            id(constant)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "DROPPED_KEYS" for t in node.targets)
            for constant in ast.walk(node)
        }
        uses = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and node.value == "expected"
            and id(node) not in dropped
        ]

        assert not uses, f"{module.name} mentions 'expected' at lines {uses}"
