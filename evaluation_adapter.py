"""The evaluation contract of the kit (kit/CONTRATO_EVALUACION.md).

    python evaluation_adapter.py --scenario examples/T02.json --output resultado.json

Runs one scenario in a fresh, isolated world and writes the contract's JSON output. Exit code 2
means the scenario is malformed or uses something the harness does not support; it never
produces a silent PASS. With --workdir the world (its SQLite files) is kept there, so the
outcome can be checked against the real state and not only against this output.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from cortex.kit_adapter.orchestrator import DEFAULT_NOW, run_scenario_file
from cortex.kit_adapter.scenario import UnsupportedScenario

ROOT = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workdir", type=Path, help="keep the scenario's world in this directory")
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=DEFAULT_NOW,
        help="the instant the harness treats as now (ISO 8601 with offset)",
    )
    args = parser.parse_args(argv)
    if args.now.utcoffset() is None:
        parser.error("--now needs a timezone offset, for example 2026-10-28T12:00:00+00:00")

    try:
        output = run_scenario_file(
            args.scenario, kit_dir=ROOT / "kit", now=args.now, workdir=args.workdir
        )
    except UnsupportedScenario as unsupported:
        sys.stderr.write(f"unsupported scenario: {unsupported}\n")
        return 2
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
