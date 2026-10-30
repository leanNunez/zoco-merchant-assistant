"""A short demo: five kit scenarios run through evaluation_adapter.py, one line of outcome each.

    uv run python -m evaluation.harness.demo

Each scenario runs in its own process and a throwaway directory, exactly as `make eval` does,
but nothing is checked or written to the results: it only shows what the harness decided.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# One per area of the brief: a read, a cross-company attempt, a write, a takeover, a follow-up.
SCENARIOS = ("T02", "T05", "T09", "T11", "T12")

# The observables worth showing, in this order, when the scenario produces them.
HIGHLIGHTS = (
    "executed_tools",
    "rejected_tools",
    "claims_created",
    "ai_replies_delivered",
    "customer_messages_sent",
    "tool_effects_after_takeover",
    "decision",
)


def run(scenario_id: str, workdir: Path) -> dict[str, object]:
    output = workdir / f"{scenario_id}.json"
    subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [
            sys.executable,
            str(ROOT / "evaluation_adapter.py"),
            "--scenario",
            str(ROOT / "examples" / f"{scenario_id}.json"),
            "--output",
            str(output),
        ],
        check=True,
    )
    result: dict[str, object] = json.loads(output.read_text(encoding="utf-8"))
    return result


def describe(scenario_id: str, result: dict[str, object]) -> str:
    scenario = json.loads((ROOT / "examples" / f"{scenario_id}.json").read_text(encoding="utf-8"))
    shown = ", ".join(
        f"{key}={result[key]}" for key in HIGHLIGHTS if result.get(key) not in (None, [])
    )
    return (
        f"{scenario_id}  {scenario.get('description', '')}\n"
        f"     {result['finish_reason']} ({result['finish_detail']})  {shown}"
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as workdir:
        for scenario_id in SCENARIOS:
            print(describe(scenario_id, run(scenario_id, Path(workdir))))
    print(
        "\nFull output of any scenario: uv run python evaluation_adapter.py "
        "--scenario examples/T09.json --output resultado.json"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
