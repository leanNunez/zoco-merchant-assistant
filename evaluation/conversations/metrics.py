"""Compliance per case, per category and overall, with honest uncertainty.

A RUN complies when every scored check passed (LOCK and UTILITY; EVIDENCE is never scored). The
rate of a case is passes over VALID runs: an invalid run is not in the denominator, and a case
with fewer than two valid runs is flagged as insufficient evidence. LOCK and UTILITY are also
rated apart, per run ("every LOCK check of the run passed") and per check.

`pass^k` is the share of cases whose every valid repetition complied: the probability that a
case works every time, not on average. The Wilson 95% interval is reported for the rates; with
three repetitions per case the intervals are wide, and the report says so.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from evaluation.conversations.checks import CheckKind
from evaluation.conversations.runner import Evaluation, RunResult
from evaluation.conversations.scenario import Category, Scenario

Z95 = 1.959963984540054
MIN_VALID = 2


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float


def wilson(passes: int, total: int) -> Interval | None:
    """The Wilson score interval at 95%, or None when there is nothing to estimate."""
    if total == 0:
        return None
    p = passes / total
    z2 = Z95 * Z95
    centre = (p + z2 / (2 * total)) / (1 + z2 / total)
    margin = Z95 * math.sqrt(p * (1 - p) / total + z2 / (4 * total * total)) / (1 + z2 / total)
    # At 0 and at n the bound is exactly 0 or 1; the subtraction would leave float noise.
    low = 0.0 if passes == 0 else max(0.0, centre - margin)
    high = 1.0 if passes == total else min(1.0, centre + margin)
    return Interval(low, high)


@dataclass(frozen=True, slots=True)
class Rate:
    passes: int
    total: int

    @property
    def value(self) -> float | None:
        return None if self.total == 0 else self.passes / self.total

    @property
    def interval(self) -> Interval | None:
        return wilson(self.passes, self.total)

    def __add__(self, other: Rate) -> Rate:
        return Rate(self.passes + other.passes, self.total + other.total)


@dataclass(frozen=True, slots=True)
class CheckTally:
    name: str
    kind: CheckKind
    failures: int  # in how many valid runs it failed


@dataclass(frozen=True, slots=True)
class CaseMetrics:
    scenario_id: str
    title: str
    category: Category
    split: str
    runs: int
    valid: int
    compliance: Rate  # runs where every scored check passed
    lock_runs: Rate
    utility_runs: Rate
    lock_checks: Rate
    utility_checks: Rate
    failed: tuple[CheckTally, ...]
    evidence: Rate  # evidence checks that held, over all evidence checks of valid runs
    known_limitation: bool

    @property
    def insufficient(self) -> bool:
        return self.valid < MIN_VALID

    @property
    def always(self) -> bool:
        """Every valid repetition complied (and there was at least one)."""
        return self.valid > 0 and self.compliance.passes == self.valid


@dataclass(frozen=True, slots=True)
class GroupMetrics:
    label: str
    cases: int
    compliance: Rate
    lock_runs: Rate
    utility_runs: Rate
    lock_checks: Rate
    utility_checks: Rate
    pass_k: Rate  # cases whose every valid repetition complied, over cases with a valid run


@dataclass(frozen=True, slots=True)
class CostMetrics:
    conversations: int
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    spent_usd: Decimal | None  # None: some call has no known price
    mean_conversation_seconds: float | None
    mean_final_request_seconds: float | None


@dataclass(frozen=True, slots=True)
class Summary:
    cases: tuple[CaseMetrics, ...]
    categories: tuple[GroupMetrics, ...]
    splits: tuple[GroupMetrics, ...]  # only when the run holds more than one split
    overall: GroupMetrics
    cost: CostMetrics
    invalid: int


def summarize(evaluation: Evaluation) -> Summary:
    cases = tuple(
        case_metrics(scenario, evaluation.results_of(scenario.id))
        for scenario in evaluation.scenarios
    )
    categories = tuple(
        group(str(category), [c for c in cases if c.category is category])
        for category in Category
        if any(c.category is category for c in cases)
    )
    present = [name for name in ("dev", "heldout") if any(c.split == name for c in cases)]
    splits = tuple(group(name, [c for c in cases if c.split == name]) for name in present)
    return Summary(
        cases=cases,
        categories=categories,
        splits=splits if len(splits) > 1 else (),
        overall=group("total", list(cases)),
        cost=cost_metrics(evaluation.results),
        invalid=sum(1 for result in evaluation.results if not result.valid),
    )


def case_metrics(scenario: Scenario, results: Sequence[RunResult]) -> CaseMetrics:
    valid = [result for result in results if result.valid]
    tallies: Counter[tuple[str, CheckKind]] = Counter()
    lock_checks = utility_checks = evidence = Rate(0, 0)
    lock_runs = utility_runs = 0
    for result in valid:
        locks = [c for c in result.checks if c.kind is CheckKind.LOCK]
        utilities = [c for c in result.checks if c.kind is CheckKind.UTILITY]
        evidences = [c for c in result.checks if c.kind is CheckKind.EVIDENCE]
        lock_checks += Rate(sum(c.passed for c in locks), len(locks))
        utility_checks += Rate(sum(c.passed for c in utilities), len(utilities))
        evidence += Rate(sum(c.passed for c in evidences), len(evidences))
        lock_runs += all(c.passed for c in locks)
        utility_runs += all(c.passed for c in utilities)
        tallies.update((c.name, c.kind) for c in result.scored if not c.passed)
    order = {check.name: index for index, check in enumerate(scenario.checks)}
    failed = tuple(
        CheckTally(name, kind, count)
        for (name, kind), count in sorted(tallies.items(), key=lambda item: order[item[0][0]])
    )
    return CaseMetrics(
        scenario_id=scenario.id,
        title=scenario.title,
        category=scenario.category,
        split=scenario.split,
        runs=len(results),
        valid=len(valid),
        compliance=Rate(sum(r.passed for r in valid), len(valid)),
        lock_runs=Rate(lock_runs, len(valid)),
        utility_runs=Rate(utility_runs, len(valid)),
        lock_checks=lock_checks,
        utility_checks=utility_checks,
        failed=failed,
        evidence=evidence,
        known_limitation=scenario.known_limitation is not None,
    )


def group(label: str, cases: Sequence[CaseMetrics]) -> GroupMetrics:
    zero = Rate(0, 0)
    with_runs = [case for case in cases if case.valid > 0]
    return GroupMetrics(
        label=label,
        cases=len(cases),
        compliance=sum((c.compliance for c in cases), zero),
        lock_runs=sum((c.lock_runs for c in cases), zero),
        utility_runs=sum((c.utility_runs for c in cases), zero),
        lock_checks=sum((c.lock_checks for c in cases), zero),
        utility_checks=sum((c.utility_checks for c in cases), zero),
        pass_k=Rate(sum(c.always for c in with_runs), len(with_runs)),
    )


def cost_metrics(results: Sequence[RunResult]) -> CostMetrics:
    observed = [r.observation for r in results if r.observation is not None]
    calls = [call for obs in observed for call in obs.model_calls]
    durations: list[float] = []
    finals: list[float] = []
    for obs in observed:
        latencies = [c.latency_seconds for c in obs.model_calls if c.latency_seconds is not None]
        if latencies:
            durations.append(sum(latencies))
            finals.append(latencies[-1])
    costs = [r.cost_usd for r in results if r.observation is not None]
    spent: Decimal | None = None
    if costs and all(c is not None for c in costs):
        spent = sum((c for c in costs if c is not None), Decimal(0))
    return CostMetrics(
        conversations=len(observed),
        model_calls=len(calls),
        prompt_tokens=sum(c.prompt_tokens or 0 for c in calls),
        completion_tokens=sum(c.completion_tokens or 0 for c in calls),
        spent_usd=spent,
        mean_conversation_seconds=_mean(durations),
        mean_final_request_seconds=_mean(finals),
    )


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None
