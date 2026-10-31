"""Writes the two sample traces of the delivery: one success and one failure (brief section 9).

    python -m cortex.kit_adapter.samples [OUTPUT_DIRECTORY]

Both run through the same entry point as any scenario, on the fixed clock, so the files are
reproducible and a test keeps them current. They are anonymous by construction: a trace carries
no message text, only codes, names, counts and a keyed pseudonym of who acted (the demo's
default key, so the files are reproducible).

- success: T07, a read that times out once, is retried and answers.
- failure: a confirmed write whose outcome stays unknown after reconciliation (`FAILED`),
  the case the harness exists to handle without filing a second claim.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from cortex.kit_adapter.orchestrator import DEFAULT_NOW, run_scenario
from cortex.kit_adapter.scenario import load_scenario
from cortex.tools.adapters.faults import FaultKind
from merchant_assistant.agent.domain.tools import CREAR_RECLAMO

ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kit"
EXAMPLES = ROOT / "examples"
DEFAULT_OUTPUT = ROOT / "evaluation" / "results" / "harness" / "traces"

SUCCESS_FILE = "success_T07_read_retried.jsonl"
FAILURE_FILE = "failure_T09_write_outcome_unknown.jsonl"


def _example(name: str) -> dict[str, object]:
    data = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"{name} is not a scenario")
    return data


def sample_scenarios() -> Mapping[str, dict[str, object]]:
    failure = _example("T09")
    script = failure["model_script"]
    if not isinstance(script, list):
        raise TypeError("T09 has no model_script")
    failure["model_script"] = [script[0], script[-1]]  # one request, then the answer
    # The write times out twice: the answer is lost, the lookup finds nothing, and the one more
    # attempt times out as well. Nothing can be said about the claim, so nothing is repeated.
    failure["faults"] = {CREAR_RECLAMO: [FaultKind.TIMEOUT.value, FaultKind.TIMEOUT.value]}
    return {SUCCESS_FILE: _example("T07"), FAILURE_FILE: failure}


def render(scenario: dict[str, object]) -> str:
    with tempfile.TemporaryDirectory(prefix="cortex-sample-") as workdir:
        output = run_scenario(
            load_scenario(scenario), kit_dir=KIT, workdir=Path(workdir), now=DEFAULT_NOW
        )
    events = output["events"]
    if not isinstance(events, list):
        raise TypeError("the output has no events")
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    target = Path(args[0]) if args else DEFAULT_OUTPUT
    target.mkdir(parents=True, exist_ok=True)
    for name, scenario in sample_scenarios().items():
        (target / name).write_text(render(scenario), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
