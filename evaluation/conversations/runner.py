"""Scenarios times repetitions, each in a fresh world, classified as valid or invalid.

A run is VALID when the conversation was played to its end: its checks decide whether it
complied. It is INVALID when something outside the agent's behaviour got in the way (the provider
was unavailable or refused the credentials, the conversation hit its wall-clock ceiling or the
spending cap, the harness itself raised). An invalid run is never scored, neither as a failure of
the model nor as a pass; it goes to `errors.jsonl` with its failure class.

Repetitions run in REPETITION-MAJOR order (every scenario once, then every scenario again), so a
live run stopped by its spending cap still covers every case as evenly as it can. Each
repetition gets its own seed (`base_seed + rep`), used by the backoff jitter and by the rate-based
fault injector; the scripted modes are deterministic whatever the seed.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Literal

from evaluation.conversations.checks import CheckKind, CheckResult
from evaluation.conversations.drivers import drive, observe
from evaluation.conversations.models import (
    Budget,
    BudgetExhausted,
    ConversationTimeLimit,
    LiveLimits,
    NullModel,
    ObservedModel,
    ReactiveModel,
    Timer,
    UnknownCost,
)
from evaluation.conversations.observation import Observation, StepFact
from evaluation.conversations.pricing import cost
from evaluation.conversations.scenario import Scenario
from evaluation.conversations.world import Ground, World, build_real, prepare
from merchant_assistant.agent.ports import ChatModel
from merchant_assistant.shared.config import Settings

Mode = Literal["scripted", "null", "naive", "live"]
MODES: tuple[Mode, ...] = ("scripted", "null", "naive", "live")
DEFAULT_REPS = 3
DEFAULT_BUDGET_USD = Decimal("1.00")
LIVE_CONVERSATION_SECONDS = 180.0

# A provider failure says nothing about how the agent behaves: the run is not scored.
INFRA_FAILURES = {
    "model_unavailable": "provider_unavailable",
    "model_auth": "authentication",
    "model_not_found": "model_not_found",
    "model_rejected": "request_rejected",
}
INTERNAL_ERROR = "model_internal_error"
# After these, every later conversation would fail the same way: the whole run stops.
STOPS_THE_RUN = frozenset({"authentication", "model_not_found", "budget_exhausted", "unknown_cost"})

WorldBuilder = Callable[[Ground, ChatModel, str], World]


@dataclass(frozen=True)
class RunSettings:
    mode: Mode
    reps: int = DEFAULT_REPS
    base_seed: int = 0
    only: tuple[str, ...] = ()
    budget_usd: Decimal = DEFAULT_BUDGET_USD
    conversation_seconds: float = LIVE_CONVERSATION_SECONDS

    def __post_init__(self) -> None:
        if self.reps < 1:
            raise ValueError("reps must be >= 1")
        if not self.budget_usd > 0:
            raise ValueError("the budget must be above 0")
        if not self.conversation_seconds > 0:
            raise ValueError("the conversation ceiling must be above 0")


@dataclass(frozen=True)
class LiveSetup:
    """The real model, built once by the composition root, and what it was asked to be."""

    model: ChatModel
    requested_model: str
    base: Settings
    timer: Timer = time.perf_counter


@dataclass(frozen=True)
class RunResult:
    scenario_id: str
    rep: int
    seed: int
    valid: bool
    failure_class: str | None = None
    failure_detail: str | None = None
    checks: tuple[CheckResult, ...] = ()
    observation: Observation | None = None
    search_mode: str | None = None
    search_reason: str | None = None
    corpus_sha256: str | None = None
    cost_usd: Decimal | None = None

    @property
    def scored(self) -> tuple[CheckResult, ...]:
        return tuple(check for check in self.checks if check.kind is not CheckKind.EVIDENCE)

    @property
    def passed(self) -> bool:
        return self.valid and all(check.passed for check in self.scored)

    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.scored if not check.passed)


@dataclass(frozen=True)
class Evaluation:
    settings: RunSettings
    scenarios: tuple[Scenario, ...]
    results: tuple[RunResult, ...]
    not_run: tuple[tuple[str, int], ...] = ()
    stop_reason: str | None = None
    requested_model: str | None = None
    spent_usd: Decimal | None = None
    unknown_cost_calls: int = 0

    @property
    def partial(self) -> bool:
        return self.stop_reason is not None or bool(self.not_run)

    def results_of(self, scenario_id: str) -> tuple[RunResult, ...]:
        return tuple(r for r in self.results if r.scenario_id == scenario_id)


def select(scenarios: Sequence[Scenario], only: Sequence[str]) -> tuple[Scenario, ...]:
    if not only:
        return tuple(scenarios)
    known = {scenario.id for scenario in scenarios}
    unknown = sorted(set(only) - known)
    if unknown:
        raise ValueError(f"unknown scenario id(s): {', '.join(unknown)}")
    return tuple(scenario for scenario in scenarios if scenario.id in set(only))


def run_evaluation(
    scenarios: Sequence[Scenario],
    settings: RunSettings,
    *,
    live: LiveSetup | None = None,
    builders: dict[str, WorldBuilder] | None = None,
) -> Evaluation:
    if (settings.mode == "live") != (live is not None):
        raise ValueError("the live mode, and only it, needs a LiveSetup")
    chosen = select(scenarios, settings.only)
    budget = Budget(settings.budget_usd) if live is not None else None
    builder = (builders or {}).get(settings.mode, build_real)
    results: list[RunResult] = []
    not_run: list[tuple[str, int]] = []
    stop: str | None = None
    for rep in range(settings.reps):
        for scenario in chosen:
            if stop is not None:
                not_run.append((scenario.id, rep))
                continue
            result = run_once(scenario, settings, rep, live=live, budget=budget, builder=builder)
            results.append(result)
            if result.failure_class in STOPS_THE_RUN:
                stop = result.failure_class
    order = {scenario.id: index for index, scenario in enumerate(chosen)}
    results.sort(key=lambda r: (order[r.scenario_id], r.rep))
    return Evaluation(
        settings=settings,
        scenarios=chosen,
        results=tuple(results),
        not_run=tuple(sorted(not_run, key=lambda item: (order[item[0]], item[1]))),
        stop_reason=stop,
        requested_model=None if live is None else live.requested_model,
        spent_usd=None if budget is None else budget.spent_usd,
        unknown_cost_calls=0 if budget is None else budget.unknown_calls,
    )


def run_once(
    scenario: Scenario,
    settings: RunSettings,
    rep: int,
    *,
    live: LiveSetup | None = None,
    budget: Budget | None = None,
    builder: WorldBuilder = build_real,
) -> RunResult:
    """One conversation in its own temporary directory, judged by the scenario's checks."""
    seed = settings.base_seed + rep
    conversation_id = f"eval-{scenario.id.lower()}-r{rep}"
    with tempfile.TemporaryDirectory(prefix="eval-conv-") as directory:
        ground = prepare(
            scenario.world, Path(directory), seed=seed, base=None if live is None else live.base
        )
        model = _observed(scenario, settings, live, budget)
        try:
            world = builder(ground, model, conversation_id)
            driven = drive(scenario.steps, world.agent, world, stop=_stop_rule(settings.mode))
            observation = observe(
                driven,
                world.agent.snapshot(),
                world.read_rows(),
                model_calls=tuple(model.calls),
                system_prompts=tuple(model.prompts),
                public_texts=ground.public_texts,
            )
        except BudgetExhausted as limit:
            return _invalid(scenario, rep, seed, "budget_exhausted", str(limit))
        except ConversationTimeLimit as limit:
            return _invalid(scenario, rep, seed, "timeout", str(limit))
        except UnknownCost as unknown:
            return _invalid(scenario, rep, seed, "unknown_cost", str(unknown))
        except Exception as error:  # any other harness failure: invalid, never scored
            return _invalid(scenario, rep, seed, "harness_exception", type(error).__name__)
    base = RunResult(
        scenario.id,
        rep,
        seed,
        valid=True,
        observation=observation,
        search_mode=world.search_mode,
        search_reason=world.search_reason,
        corpus_sha256=ground.corpus_sha256,
        cost_usd=_spent(observation) if live is not None else None,
    )
    if driven.stopped is not None:
        detail = f"stopped after step {len(driven.steps) - 1}"
        return replace(base, valid=False, failure_class=driven.stopped, failure_detail=detail)
    return replace(base, checks=tuple(check.run(observation) for check in scenario.checks))


def _observed(
    scenario: Scenario, settings: RunSettings, live: LiveSetup | None, budget: Budget | None
) -> ObservedModel:
    if live is not None and budget is not None:
        limits = LiveLimits(budget, settings.conversation_seconds, live.timer)
        return ObservedModel(live.model, timer=live.timer, limits=limits)
    inner: ChatModel = (
        NullModel() if settings.mode == "null" else ReactiveModel(scenario.model_script())
    )
    return ObservedModel(inner)


def _stop_rule(mode: Mode) -> Callable[[StepFact], str | None]:
    def rule(fact: StepFact) -> str | None:
        if fact.model_error == INTERNAL_ERROR:
            # Offline, the model is our own script: an exception in it is a harness bug.
            return "adapter_exception" if mode == "live" else "harness_exception"
        return INFRA_FAILURES.get(fact.model_error or "")

    return rule


def _spent(observation: Observation) -> Decimal | None:
    total = Decimal(0)
    for call in observation.model_calls:
        price = cost(call.served_model, call.prompt_tokens, call.completion_tokens)
        if price is None:
            return None
        total += price
    return total


def _invalid(scenario: Scenario, rep: int, seed: int, failure: str, detail: str) -> RunResult:
    return RunResult(
        scenario.id, rep, seed, valid=False, failure_class=failure, failure_detail=detail[:160]
    )
