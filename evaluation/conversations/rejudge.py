"""Judge SAVED transcripts again with the current checks, without calling any model.

`python -m evaluation.conversations.rejudge evaluation/results/conversations/live-v1/results.json`

A change to a check (ADR-0008) is only justified if it can be shown on the evidence that made
it necessary. This rebuilds each saved `Observation` from `results.json`, runs the CURRENT checks
of the catalog on it, and prints, check by check, how often it passed before and now, plus the
runs that comply under each version of the evaluator. The model's answers are fixed, so any
difference is the evaluator's, not the model's.

Only what a saved trajectory keeps can be rebuilt: the system prompts are stored as a hash, so
the prompt-leak lock reads "no prompt recorded" here and trivially passes.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluation.conversations.catalog import SCENARIOS
from evaluation.conversations.checks import CheckKind
from evaluation.conversations.observation import (
    DraftFact,
    HistoryEntry,
    InboxEntry,
    LogEntry,
    ModelCall,
    Observation,
    RowFact,
    StepFact,
    ToolFact,
    Visible,
)

Json = Mapping[str, Any]


def rebuild(trajectory: Json) -> Observation:
    """The `Observation` a run saved in `results.json` (see `report._trajectory`)."""

    def each(cls: Any, key: str) -> tuple[Any, ...]:
        return tuple(cls(**item) for item in trajectory[key])

    steps = tuple(
        StepFact(
            **{
                **step,
                "visible": tuple(Visible(**v) for v in step["visible"]),
                "drafts": tuple(step["drafts"]),
                "events": tuple(step["events"]),
                "violations": tuple(step["violations"]),
                "flags": tuple(step["flags"]),
            }
        )
        for step in trajectory["steps"]
    )
    history = tuple(
        HistoryEntry(
            h["role"],
            h["author"],
            h["content"],
            tuple((c[0], c[1], c[2]) for c in h["tool_calls"]),
            h["tool_call_id"],
        )
        for h in trajectory["history"]
    )
    return Observation(
        steps=steps,
        tools=each(ToolFact, "tools"),
        rows=each(RowFact, "rows"),
        history=history,
        control_log=each(LogEntry, "control_log"),
        inbox=each(InboxEntry, "inbox"),
        drafts=each(DraftFact, "drafts"),
        final_state=trajectory["final_state"],
        model_calls=each(ModelCall, "model_calls"),
        public_texts=tuple(trajectory["public_texts"]),
    )


@dataclass(frozen=True, slots=True)
class CheckChange:
    scenario: str
    check: str
    before: tuple[int, int]  # passes, runs that had the check
    after: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Rejudgment:
    complying_before: int
    complying_after: int
    runs: int
    changes: tuple[CheckChange, ...]


def rejudge(document: Json) -> Rejudgment:
    by_id = {scenario.id: scenario for scenario in SCENARIOS}
    before: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    after: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    complying_before = complying_after = runs = 0
    for run in document["runs"]:
        if not run["valid"] or run["trajectory"] is None:
            continue
        runs += 1
        scenario = by_id[run["scenario"]]
        observation = rebuild(run["trajectory"])
        results = [c.run(observation) for c in scenario.checks if c.applies(observation)]
        complying_before += bool(run["passed"])
        complying_after += all(r.passed for r in results if r.kind is not CheckKind.EVIDENCE)
        for check in run["checks"]:
            tally = before[(run["scenario"], check["name"])]
            tally[0] += bool(check["passed"])
            tally[1] += 1
        for result in results:
            tally = after[(run["scenario"], result.name)]
            tally[0] += result.passed
            tally[1] += 1
    changes = tuple(
        CheckChange(scenario, name, _pair(before.get(key)), _pair(after.get(key)))
        for key in sorted(before.keys() | after.keys())
        for scenario, name in [key]
        if before.get(key) != after.get(key)
    )
    return Rejudgment(complying_before, complying_after, runs, changes)


def _pair(tally: Sequence[int] | None) -> tuple[int, int]:
    return (0, 0) if tally is None else (tally[0], tally[1])


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Uso: python -m evaluation.conversations.rejudge <results.json>", file=sys.stderr)
        return 2
    result = rejudge(json.loads(Path(args[0]).read_text(encoding="utf-8")))
    print(
        f"Corridas que cumplen: {result.complying_before}/{result.runs} con el evaluador de "
        f"entonces, {result.complying_after}/{result.runs} con el actual."
    )
    for change in result.changes:
        print(
            f"{change.scenario:4} {change.check}: {change.before[0]}/{change.before[1]} -> "
            f"{change.after[0]}/{change.after[1]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
