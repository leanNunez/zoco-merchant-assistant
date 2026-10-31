"""The evaluation contract of the kit (kit/CONTRATO_EVALUACION.md).

    python evaluation_adapter.py --scenario examples/T02.json --output resultado.json

Runs one scenario in a fresh, isolated world and writes the contract's JSON output. Exit code 2
means the scenario is malformed or uses something the harness does not support; it never
produces a silent PASS. With --workdir the world (its SQLite files) is kept there, so the
outcome can be checked against the real state and not only against this output.

The one environment variable, read here and nowhere else, is CORTEX_TRACE_KEY: the key of the
principal pseudonym in the traces. Without it the demo's public default is used, which keeps the
outputs reproducible.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRACE_KEY_VARIABLE = "CORTEX_TRACE_KEY"


def _use_the_project_interpreter() -> None:
    """Run under the project's virtual environment when started with another Python.

    The contract's command is a bare `python evaluation_adapter.py ...`, which may be a Python
    without the harness installed. If `make setup` created `.venv`, the same command is re-run
    with its interpreter; otherwise the adapter says what to do instead of failing on an import.
    `sys.prefix` (not the binary) tells the environments apart: the venv's python is usually a
    symlink to the system one, and comparing it would never re-run or loop forever.
    """
    if importlib.util.find_spec("cortex") is not None:
        return
    venv = ROOT / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.exists() and Path(sys.prefix).resolve() != venv.resolve():
        os.execv(python, [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])  # noqa: S606
    sys.stderr.write("the harness is not installed: run `make setup` first, or use `uv run`\n")
    raise SystemExit(2)


_use_the_project_interpreter()

from cortex.bootstrap import DEFAULT_TRACE_KEY  # noqa: E402 - only after the interpreter check
from cortex.config import ConfigError  # noqa: E402
from cortex.kit_adapter.orchestrator import DEFAULT_NOW, run_scenario_file  # noqa: E402
from cortex.kit_adapter.scenario import UnsupportedScenario  # noqa: E402


def _is_occupied(path: Path) -> bool:
    """Something is already there: a file, or a directory with anything in it."""
    return path.exists() and (not path.is_dir() or any(path.iterdir()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--workdir",
        type=Path,
        help="keep the scenario's world in this directory (it must be empty or not exist yet)",
    )
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=DEFAULT_NOW,
        help="the instant the harness treats as now (ISO 8601 with offset)",
    )
    args = parser.parse_args(argv)
    if args.now.utcoffset() is None:
        parser.error("--now needs a timezone offset, for example 2026-10-28T12:00:00+00:00")

    if args.workdir is not None and _is_occupied(args.workdir):
        # A world left by another scenario would be reused silently, and its claims counted.
        parser.error("--workdir must be empty or not exist yet")

    trace_key = os.environ.get(TRACE_KEY_VARIABLE, DEFAULT_TRACE_KEY)
    if not trace_key.strip():
        parser.error(f"{TRACE_KEY_VARIABLE} must not be empty or blank (unset it for the default)")

    try:
        output = run_scenario_file(
            args.scenario,
            kit_dir=ROOT / "kit",
            now=args.now,
            workdir=args.workdir,
            trace_key=trace_key,
        )
    except UnsupportedScenario as unsupported:
        sys.stderr.write(f"unsupported scenario: {unsupported}\n")
        return 2
    except ConfigError as invalid:
        # A kit that loosens a policy or breaks a limit: refused, never obeyed.
        sys.stderr.write(f"invalid harness configuration: {invalid.detail}\n")
        return 2
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
