"""The evaluator validated against itself, on every scenario of the catalog.

1. The scripted models with the REAL agent pass every check, except the checks a scenario
   declares as a known limitation of the agent, which must fail exactly as declared.
2. The null model (answers a fixed sentence, never calls a tool) passes every LOCK check and
   fails at least one UTILITY check per scenario: "no answer" is never scored as compliance.
3. The unsafe reference agent fails at least one LOCK check in every attack and every state
   crossing: the locks detect a violation when there is one.
"""

import pytest
from evaluation.conversations.catalog import SCENARIOS
from evaluation.conversations.checks import CheckKind
from evaluation.conversations.runner import Evaluation, RunResult, RunSettings, run_evaluation
from evaluation.conversations.scenario import Category, Say, Scenario

pytestmark = pytest.mark.integration

ADVERSARIAL = frozenset({Category.POLICY_ATTACK, Category.STATE_CROSSING})
IDS = [scenario.id for scenario in SCENARIOS]


@pytest.fixture(scope="module")
def runs() -> dict[str, Evaluation]:
    return {
        mode: run_evaluation(SCENARIOS, RunSettings(mode, reps=1))
        for mode in ("scripted", "null", "naive")
    }


def only(evaluation: Evaluation, scenario: Scenario) -> RunResult:
    (result,) = evaluation.results_of(scenario.id)
    assert result.valid, (scenario.id, result.failure_class, result.failure_detail)
    return result


def failing(result: RunResult, kind: CheckKind) -> list[str]:
    return [c.name for c in result.checks if c.kind is kind and not c.passed]


def test_the_catalog_covers_what_part_4_asks_for() -> None:
    assert len(SCENARIOS) >= 15
    assert len(set(IDS)) == len(IDS)
    by_category = {
        category: [s for s in SCENARIOS if s.category is category] for category in Category
    }
    assert all(by_category.values())
    assert len(by_category[Category.STATE_CROSSING]) >= 3
    assert all(len(s.user_messages) >= 2 for s in SCENARIOS)
    poisoned = [s for s in SCENARIOS if s.world.corpus_patch is not None]
    assert [s.category for s in poisoned] == [Category.POLICY_ATTACK]  # rule 7, indirect
    assert all(isinstance(step, Say) or step.tag for s in SCENARIOS for step in s.steps)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_1_the_scripted_models_pass_every_check_but_the_declared_limitations(
    runs: dict[str, Evaluation], scenario: Scenario
) -> None:
    result = only(runs["scripted"], scenario)
    assert result.failed_checks() == scenario.expected_failures
    assert failing(result, CheckKind.EVIDENCE) == []  # every attack really happened


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_2_the_null_model_breaks_no_lock_and_is_never_useful(
    runs: dict[str, Evaluation], scenario: Scenario
) -> None:
    result = only(runs["null"], scenario)
    assert failing(result, CheckKind.LOCK) == []
    assert failing(result, CheckKind.UTILITY) != []


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS if s.category in ADVERSARIAL], ids=lambda s: s.id
)
def test_3_the_unsafe_agent_is_caught_by_the_locks(
    runs: dict[str, Evaluation], scenario: Scenario
) -> None:
    assert failing(only(runs["naive"], scenario), CheckKind.LOCK) != []


def test_the_known_limitation_is_safe_even_while_it_is_useless(runs: dict[str, Evaluation]) -> None:
    (limited,) = [s for s in SCENARIOS if s.known_limitation]
    result = only(runs["scripted"], limited)
    assert failing(result, CheckKind.LOCK) == []
    assert result.observation is not None
    assert result.observation.final_state == "SUGGEST"
