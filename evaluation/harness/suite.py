"""Runs every scenario through the real entry point and checks it against the real state.

    python -m evaluation.harness [--only NAME ...] [--jobs N] [--date YYYY-MM-DD] [--out DIR]

One subprocess per scenario, through `evaluation_adapter.py` exactly as IT runs it, each in a
world of its own (kept so the checker can open its SQLite files). The checker never imports the
harness. PASS and FAIL come from its checks, never from the adapter's say-so.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from evaluation.harness.checker import Check, check_refusal, check_scenario
from evaluation.harness.variants import Case, Scenario, build_variants

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "evaluation_adapter.py"
RESULTS = ROOT / "evaluation" / "results" / "harness"
TIMEOUT_SECONDS = 120
GIT = shutil.which("git") or "git"


@dataclass(frozen=True, slots=True)
class Outcome:
    name: str
    kind: str
    description: str
    passed: bool
    checks: tuple[Check, ...]

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.ok)


def load_kit_cases() -> list[Case]:
    cases = []
    for path in sorted((ROOT / "examples").glob("T*.json")):
        scenario: Scenario = json.loads(path.read_text(encoding="utf-8"))
        cases.append(Case(str(scenario["id"]), "kit", str(scenario["description"]), scenario))
    return cases


def all_cases() -> list[Case]:
    kit = load_kit_cases()
    return [*kit, *build_variants({case.name: case.scenario for case in kit})]


def run_case(case: Case, workspace: Path) -> Outcome:
    folder = workspace / case.name
    folder.mkdir(parents=True)
    scenario_file, output_file, world = (
        folder / "scenario.json",
        folder / "out.json",
        folder / "world",
    )
    scenario_file.write_text(json.dumps(case.scenario), encoding="utf-8")
    try:
        done = subprocess.run(  # noqa: S603 - our own interpreter and adapter
            [
                sys.executable,
                str(ADAPTER),
                "--scenario",
                str(scenario_file),
                "--output",
                str(output_file),
                "--workdir",
                str(world),
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Outcome(
            case.name,
            case.kind,
            case.description,
            False,
            (Check("run", False, f"no answer after {TIMEOUT_SECONDS} s"),),
        )
    if case.expect_exit != 0 or done.returncode != 0:
        checks = check_refusal(done.returncode, case.expect_exit, done.stderr, output_file.exists())
    else:
        try:
            output = json.loads(output_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            checks = [Check("run", False, "the adapter wrote no valid JSON output")]
        else:
            checks = check_scenario(case.scenario, output, world)
    return Outcome(case.name, case.kind, case.description, all(c.ok for c in checks), tuple(checks))


def run_cases(cases: Sequence[Case], jobs: int) -> list[Outcome]:
    with (
        tempfile.TemporaryDirectory(prefix="cortex-eval-") as workspace,
        ThreadPoolExecutor(jobs) as pool,
    ):
        return list(pool.map(lambda case: run_case(case, Path(workspace)), cases))


# --------------------------------------------------------------------------- provenance and report


def _git(*args: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed git subcommands
            [GIT, *args], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def code_digest() -> str:
    """A hash of what is being evaluated: the harness, the adapter, the kit and this suite."""
    files = sorted(
        [
            *(ROOT / "src" / "cortex").rglob("*.py"),
            *(ROOT / "evaluation" / "harness").glob("*.py"),
            ROOT / "evaluation_adapter.py",
            ROOT / "kit" / "MANIFEST.sha256",
        ]
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def provenance(date: str) -> dict[str, object]:
    return {
        "date": date,
        "commit": _git("rev-parse", "HEAD") or "unknown",
        "uncommitted_changes": bool(_git("status", "--porcelain")),
        "code_sha256": code_digest(),
        "python": sys.version.split()[0],
        "command": "python -m evaluation.harness",
    }


def render_report(outcomes: Sequence[Outcome], info: dict[str, object]) -> str:
    passed = sum(outcome.passed for outcome in outcomes)
    lines = [
        f"# Evaluation of the harness, {info['date']}",
        "",
        f"**{passed}/{len(outcomes)} PASS** "
        f"({sum(o.passed for o in outcomes if o.kind == 'kit')}/"
        f"{sum(1 for o in outcomes if o.kind == 'kit')} of the kit, "
        f"{sum(o.passed for o in outcomes if o.kind == 'variant')}/"
        f"{sum(1 for o in outcomes if o.kind == 'variant')} of our variants).",
        "",
        f"- commit: `{info['commit']}`"
        + (" (with uncommitted changes)" if info["uncommitted_changes"] else ""),
        f"- code sha256: `{info['code_sha256']}`",
        f"- python: {info['python']}",
        f"- command: `{info['command']}`",
        "",
        "| scenario | kind | result | checks | description |",
        "|---|---|---|---|---|",
    ]
    for outcome in outcomes:
        result = "PASS" if outcome.passed else "FAIL"
        lines.append(
            f"| {outcome.name} | {outcome.kind} | {result} | {len(outcome.checks)} | "
            f"{outcome.description} |"
        )
    failed = [outcome for outcome in outcomes if not outcome.passed]
    if failed:
        lines += ["", "## Failures", ""]
        for outcome in failed:
            lines.append(
                f"- **{outcome.name}**: "
                + "; ".join(f"{check.name} ({check.detail})" for check in outcome.failures)
            )
    lines.append("")
    return "\n".join(lines)


def write_results(outcomes: Sequence[Outcome], info: dict[str, object], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    document = {
        **info,
        "passed": sum(outcome.passed for outcome in outcomes),
        "total": len(outcomes),
        "scenarios": [
            {
                "name": o.name,
                "kind": o.kind,
                "description": o.description,
                "passed": o.passed,
                "checks": len(o.checks),
                "failures": [asdict(check) for check in o.failures],
            }
            for o in outcomes
        ],
    }
    (folder / "results.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (folder / "report.md").write_text(render_report(outcomes, info), encoding="utf-8")
