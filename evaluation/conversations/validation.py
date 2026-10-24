"""Is the evaluator trustworthy? Three expectations, checked case by case.

1. `scripted` (intended or hostile scripts, the REAL agent): every check passes, except the
   checks a scenario declares as a known limitation of the agent, which fail exactly as declared.
2. `null` (a model that does nothing): every LOCK check passes and at least one UTILITY check
   fails, so "no answer" is never scored as a correct answer.
3. `naive` (the unsafe agent, same scripts): in every attack and state crossing at least one
   LOCK check fails, so the locks do detect a violation when there is one.

The same expectations run in CI (`tests/integration/evaluation/test_conversation_validation.py`);
this module puts them in the scripted report.
"""

from __future__ import annotations

from dataclasses import dataclass

from evaluation.conversations.checks import CheckKind
from evaluation.conversations.runner import Evaluation, RunResult
from evaluation.conversations.scenario import Category, Scenario

ADVERSARIAL = frozenset({Category.POLICY_ATTACK, Category.STATE_CROSSING})


@dataclass(frozen=True, slots=True)
class ModeTotals:
    mode: str
    lock_passed: int
    lock_total: int
    utility_passed: int
    utility_total: int


@dataclass(frozen=True, slots=True)
class ValidationRow:
    scenario_id: str
    category: Category
    scripted_as_expected: bool
    null_locks_hold: bool
    null_utility_fails: bool
    naive_lock_fails: bool | None  # None: not an attack or crossing, so not expected to fail
    naive_lock_failures: int

    @property
    def holds(self) -> bool:
        naive = self.naive_lock_fails is not False
        return (
            self.scripted_as_expected and self.null_locks_hold and self.null_utility_fails and naive
        )


@dataclass(frozen=True, slots=True)
class Validation:
    rows: tuple[ValidationRow, ...]
    totals: tuple[ModeTotals, ...]

    @property
    def holds(self) -> bool:
        return all(row.holds for row in self.rows)


def validate(scripted: Evaluation, null: Evaluation, naive: Evaluation) -> Validation:
    rows = tuple(
        _row(scenario, scripted.results_of(scenario.id), null.results_of(scenario.id),
             naive.results_of(scenario.id))
        for scenario in scripted.scenarios
    )  # fmt: skip
    totals = tuple(_totals(name, run) for name, run in (("scripted", scripted), ("null", null),
                   ("naive", naive)))  # fmt: skip
    return Validation(rows, totals)


def _row(
    scenario: Scenario,
    scripted: tuple[RunResult, ...],
    null: tuple[RunResult, ...],
    naive: tuple[RunResult, ...],
) -> ValidationRow:
    expected = scenario.expected_failures
    adversarial = scenario.category in ADVERSARIAL
    naive_failures = [_failing(r, CheckKind.LOCK) for r in naive if r.valid]
    return ValidationRow(
        scenario_id=scenario.id,
        category=scenario.category,
        scripted_as_expected=bool(scripted)
        and all(r.valid and r.failed_checks() == expected for r in scripted),
        null_locks_hold=bool(null)
        and all(r.valid and not _failing(r, CheckKind.LOCK) for r in null),
        null_utility_fails=bool(null) and all(_failing(r, CheckKind.UTILITY) for r in null),
        naive_lock_fails=(bool(naive_failures) and all(naive_failures)) if adversarial else None,
        naive_lock_failures=min((len(f) for f in naive_failures), default=0),
    )


def _failing(result: RunResult, kind: CheckKind) -> list[str]:
    return [c.name for c in result.checks if c.kind is kind and not c.passed]


def _totals(mode: str, evaluation: Evaluation) -> ModeTotals:
    checks = [c for r in evaluation.results if r.valid for c in r.checks]
    locks = [c for c in checks if c.kind is CheckKind.LOCK]
    utilities = [c for c in checks if c.kind is CheckKind.UTILITY]
    return ModeTotals(
        mode,
        sum(c.passed for c in locks),
        len(locks),
        sum(c.passed for c in utilities),
        len(utilities),
    )
