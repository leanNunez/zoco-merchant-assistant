"""Rates, Wilson intervals, pass^k and costs, against values computed by hand."""

from decimal import Decimal

import pytest
from evaluation.conversations.checks import CheckKind, CheckResult, complaint_rows, lock, utility
from evaluation.conversations.metrics import (
    Rate,
    case_metrics,
    cost_metrics,
    group,
    summarize,
    wilson,
)
from evaluation.conversations.observation import ModelCall, Observation
from evaluation.conversations.runner import Evaluation, RunResult, RunSettings
from evaluation.conversations.scenario import Category, Say, Scenario
from evaluation.conversations.script import Policy, View

from merchant_assistant.agent.domain.messages import ModelReply

LOCK = lock(complaint_rows(0))
USEFUL = utility(complaint_rows(1))


def silent() -> Policy:
    def policy(view: View) -> ModelReply:
        return view.say("hola")

    return policy


def scenario(case_id: str = "S01", category: Category = Category.SUCCESS) -> Scenario:
    return Scenario(
        id=case_id,
        title="t",
        category=category,
        steps=(Say("a"), Say("b")),
        expected_behaviour="e",
        tools_expected=(),
        tools_forbidden=(),
        checks=(LOCK, USEFUL),
        model_script=silent,
        script_note="n",
    )


def run(rep: int, *, lock_ok: bool = True, useful: bool = True, case: str = "S01") -> RunResult:
    checks = (
        CheckResult(LOCK.name, CheckKind.LOCK, lock_ok, ""),
        CheckResult(USEFUL.name, CheckKind.UTILITY, useful, ""),
        CheckResult("evidence", CheckKind.EVIDENCE, False, ""),
    )
    return RunResult(case, rep, rep, valid=True, checks=checks, observation=Observation())


def invalid(rep: int, case: str = "S01") -> RunResult:
    return RunResult(case, rep, rep, valid=False, failure_class="provider_unavailable")


def test_wilson_matches_hand_computed_intervals() -> None:
    # 3/3: centre (1 + z²/6) / (1 + z²/3) = 0.71925, margin 0.28075 -> [0.43849, 1.0]
    three = wilson(3, 3)
    assert three is not None
    assert (round(three.low, 4), three.high) == (0.4385, 1.0)
    half = wilson(5, 10)
    assert half is not None
    assert (round(half.low, 4), round(half.high, 4)) == (0.2366, 0.7634)
    assert wilson(0, 0) is None
    zero = wilson(0, 3)
    assert zero is not None
    assert (zero.low, round(zero.high, 4)) == (0.0, 0.5615)


def test_a_case_rates_valid_runs_only_and_flags_thin_evidence() -> None:
    metrics = case_metrics(scenario(), [run(0), run(1, useful=False), invalid(2)])

    assert (metrics.runs, metrics.valid) == (3, 2)
    assert (metrics.compliance.passes, metrics.compliance.total) == (1, 2)
    assert (metrics.lock_runs.passes, metrics.utility_runs.passes) == (2, 1)
    assert [(t.name, t.kind, t.failures) for t in metrics.failed] == [
        (USEFUL.name, CheckKind.UTILITY, 1)
    ]
    assert (metrics.evidence.passes, metrics.evidence.total) == (0, 2)  # reported, never scored
    assert not metrics.insufficient
    assert not metrics.always
    thin = case_metrics(scenario(), [run(0), invalid(1), invalid(2)])
    assert thin.insufficient
    assert thin.always


def test_pass_k_counts_cases_that_always_complied() -> None:
    always = case_metrics(scenario("S01"), [run(0), run(1)])
    once = case_metrics(scenario("S02"), [run(0, case="S02"), run(1, useful=False, case="S02")])
    nothing = case_metrics(scenario("S03"), [invalid(0, "S03")])

    total = group("total", [always, once, nothing])

    assert (total.pass_k.passes, total.pass_k.total) == (1, 2)  # the case without runs is out
    assert (total.compliance.passes, total.compliance.total) == (3, 4)
    assert (total.lock_checks.passes, total.lock_checks.total) == (4, 4)


def test_rates_add_up_and_say_nothing_without_runs() -> None:
    assert Rate(1, 2) + Rate(2, 3) == Rate(3, 5)
    assert Rate(0, 0).value is None
    assert Rate(0, 0).interval is None


def test_costs_and_latencies_come_from_the_recorded_calls() -> None:
    calls = (
        ModelCall("gpt-4o-mini", 1000, 100, 0.5),
        ModelCall("gpt-4o-mini", 2000, 200, 1.5),
    )
    observed = RunResult(
        "S01",
        0,
        0,
        valid=True,
        observation=Observation(model_calls=calls),
        cost_usd=Decimal("0.001"),
    )
    unknown = RunResult("S02", 0, 0, valid=True, observation=Observation(), cost_usd=None)

    cost = cost_metrics([observed])
    assert (cost.model_calls, cost.prompt_tokens, cost.completion_tokens) == (2, 3000, 300)
    assert cost.spent_usd == Decimal("0.001")
    assert cost.mean_conversation_seconds == pytest.approx(2.0)
    assert cost.mean_final_request_seconds == pytest.approx(1.5)
    assert cost_metrics([observed, unknown]).spent_usd is None  # unknown is never zero


def test_summarize_groups_by_category_in_a_fixed_order() -> None:
    cases = (scenario("S01", Category.POLICY_ATTACK), scenario("S02", Category.SUCCESS))
    evaluation = Evaluation(
        settings=RunSettings("scripted", reps=1),
        scenarios=cases,
        results=(run(0), run(0, case="S02"), invalid(1, "S02")),
    )

    summary = summarize(evaluation)

    assert [g.label for g in summary.categories] == ["success", "policy_attack"]
    assert summary.invalid == 1
    assert [c.scenario_id for c in summary.cases] == ["S01", "S02"]
